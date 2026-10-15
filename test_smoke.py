"""End to end checks that the three pipeline stages actually hand off to each other.

The per module suites cover each stage in isolation, so nothing there would notice
if the scraper wrote a column the cleaner never reads, or if the cleaner wrote a
status the exporter filters out. These tests run the real stages in sequence.
"""

import asyncio
import sqlite3

import pandas as pd
import pytest
from aiohttp.test_utils import TestServer

import clean_leads
import medspa_scraper
from clean_leads import clean_and_validate_leads
from export_leads import export_leads
from init_db import DB_PATH
from medspa_scraper import run_medspa_scraper, save_to_sqlite
from mock_places_server import create_app

CSV_PATH = 'data/leads.csv'


@pytest.fixture(autouse=True)
def fast_waits(monkeypatch):
    monkeypatch.setattr(medspa_scraper, 'PAGE_TOKEN_DELAY', 0)
    monkeypatch.setattr(medspa_scraper, 'RETRY_BACKOFF', 0)
    monkeypatch.setattr(medspa_scraper, 'MIN_SECONDS_BETWEEN_REQUESTS', 0)


@pytest.fixture
async def pipeline(tmp_path, monkeypatch):
    """Point the scraper at the local mock and run everything inside a temp directory."""
    monkeypatch.chdir(tmp_path)

    server = TestServer(create_app())
    await server.start_server()
    base = str(server.make_url('')).rstrip('/')
    monkeypatch.setattr(medspa_scraper, 'SEARCH_URL', f'{base}/textsearch/json')
    monkeypatch.setattr(medspa_scraper, 'DETAILS_URL', f'{base}/details/json')

    # The cleaner reaches the mock websites over plain HTTP, but DNS has to be faked.
    # Only .invalid domains are treated as unresolvable, so at least one lead is
    # genuinely rejected and the status filters have something to actually filter.
    def fake_resolve(domain, record_type):
        if domain.endswith('.invalid'):
            raise clean_leads.dns.resolver.NXDOMAIN(domain)
        return [f'mx.{domain}']

    monkeypatch.setattr(clean_leads.dns.resolver, 'resolve', fake_resolve)

    yield tmp_path
    await server.close()


def rows():
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute(
            'SELECT business_name, phone, email, website, zip_code, status, website_live '
            'FROM leads ORDER BY business_name'
        ).fetchall()


async def scrape(zip_codes, state='CA'):
    leads = await run_medspa_scraper('mock-key', zip_codes, state)
    return save_to_sqlite(leads)


async def clean(**kwargs):
    """Run the synchronous cleaner off the loop, so the mock server can still answer."""
    return await asyncio.to_thread(clean_and_validate_leads, **kwargs)


class TestFullPipeline:
    async def test_a_lead_travels_from_the_api_into_the_csv(self, pipeline):
        await scrape(['90210'])
        await clean()
        exported = export_leads(output_path=CSV_PATH)

        frame = pd.read_csv(CSV_PATH).set_index('business_name')
        glow = frame.loc['Glow Med Spa 90210']

        # Name and address come from the search, phone and website from the details
        # call, and the email was scraped from the website the details call returned
        assert glow['phone'] == '(310) 555-1000'
        assert glow['email'] == 'hello@glow-spa.example'
        assert glow['zip_code'] == 90210
        assert glow['status'] == 'valid'
        assert glow['website_live'] == 1
        assert exported == len(frame)

    async def test_closed_businesses_never_reach_the_database(self, pipeline):
        await scrape(['90210'])
        names = [row[0] for row in rows()]
        assert 'Closed Aesthetics 90210' not in names

    async def test_pagination_brings_in_the_later_pages(self, pipeline):
        await scrape(['90210'])
        names = [row[0] for row in rows()]
        # Serenity sits on page two of the mock results
        assert 'Serenity Facial Bar 90210' in names

    async def test_a_business_without_a_website_still_becomes_a_valid_lead(self, pipeline):
        await scrape(['90210'])
        await clean()

        with sqlite3.connect(DB_PATH) as conn:
            row = conn.execute(
                'SELECT website, email, status, website_live FROM leads WHERE business_name = ?',
                ('Serenity Facial Bar 90210',),
            ).fetchone()

        # No site means no email and no liveness verdict, but the phone still works
        assert row == ('', '', 'valid', None)

    async def test_a_site_without_an_email_keeps_the_lead_on_its_phone(self, pipeline):
        await scrape(['90210'])
        await clean()

        with sqlite3.connect(DB_PATH) as conn:
            row = conn.execute(
                'SELECT email, status, website_live FROM leads WHERE business_name = ?',
                ('Radiance Skin Clinic 90210',),
            ).fetchone()

        assert row == ('', 'valid', 1)

    async def test_a_business_with_only_an_email_is_still_valid(self, pipeline):
        await scrape(['90210'])
        await clean()

        with sqlite3.connect(DB_PATH) as conn:
            row = conn.execute(
                'SELECT phone, email, status FROM leads WHERE business_name = ?',
                ('Quiet Esthetics 90210',),
            ).fetchone()

        assert row == ('', 'book@quiet.example', 'valid')

    async def test_rerunning_the_scrape_adds_nothing_new(self, pipeline):
        first = await scrape(['90210'])
        second = await scrape(['90210'])

        assert first > 0
        # Every business is already present, so place_id dedup rejects the second pass
        assert second == 0

    async def test_two_zip_codes_produce_separate_businesses(self, pipeline):
        await scrape(['90210', '90211'])

        zips = {row[4] for row in rows()}
        assert zips == {'90210', '90211'}

    async def test_skip_recent_stops_the_second_run(self, pipeline):
        await scrape(['90210'])

        leads = await run_medspa_scraper('mock-key', ['90210'], 'CA', skip_recent_days=7)

        assert leads == []

    async def test_export_filters_match_what_the_cleaner_wrote(self, pipeline):
        await scrape(['90210'])
        await clean()

        all_leads = export_leads(output_path='data/all.csv', status=None)
        valid_leads = export_leads(output_path='data/valid.csv', status='valid')
        live_leads = export_leads(output_path='data/live.csv', status='valid', live_site_only=True)

        # Bounce Aesthetics has no phone and an unresolvable email domain, so the
        # cleaner rejects it and the default export leaves it behind
        assert all_leads == valid_leads + 1
        # Serenity has no website at all, so it drops out of the live-site list
        assert live_leads == valid_leads - 1

    async def test_a_rejected_lead_is_kept_but_never_exported(self, pipeline):
        await scrape(['90210'])
        await clean()
        export_leads(output_path=CSV_PATH)

        with sqlite3.connect(DB_PATH) as conn:
            status = conn.execute(
                'SELECT status FROM leads WHERE business_name = ?',
                ('Bounce Aesthetics 90210',),
            ).fetchone()

        # Still on record, so the rejection can be reviewed rather than silently lost
        assert status == ('invalid',)
        assert 'Bounce Aesthetics 90210' not in set(pd.read_csv(CSV_PATH)['business_name'])

    async def test_website_live_exports_as_an_integer(self, pipeline):
        await scrape(['90210'])
        await clean()
        export_leads(output_path=CSV_PATH, status=None)

        # Read as text to see exactly what was written rather than what pandas infers
        written = pd.read_csv(CSV_PATH, dtype=str)['website_live'].dropna()

        # A NULL from a business with no website must not turn the flags into 1.0
        assert set(written) == {'1'}

    async def test_cleaning_twice_is_stable(self, pipeline):
        await scrape(['90210'])

        first = await clean()
        second = await clean()

        assert first == second

    async def test_only_new_has_nothing_left_after_a_full_pass(self, pipeline):
        await scrape(['90210'])
        await clean()

        assert await clean(only_new=True) == (0, 0)


class TestStageContracts:
    async def test_every_column_the_cleaner_reads_is_written_by_the_scraper(self, pipeline):
        await scrape(['90210'])

        with sqlite3.connect(DB_PATH) as conn:
            row = conn.execute(
                'SELECT id, business_name, phone, website, email FROM leads LIMIT 1'
            ).fetchone()

        assert row is not None
        assert row[1]

    async def test_every_column_the_exporter_reads_is_populated(self, pipeline):
        from export_leads import EXPORT_COLUMNS

        await scrape(['90210'])
        await clean()
        export_leads(output_path=CSV_PATH)

        frame = pd.read_csv(CSV_PATH)
        assert list(frame.columns) == EXPORT_COLUMNS
        for column in ['business_name', 'industry', 'address', 'zip_code', 'state', 'source', 'place_id']:
            assert frame[column].notna().all(), f'{column} came back empty'

    async def test_a_scrape_run_is_recorded_for_the_cleaner_to_build_on(self, pipeline):
        await scrape(['90210'])

        with sqlite3.connect(DB_PATH) as conn:
            row = conn.execute('SELECT zip_code, state, leads_found FROM scrape_runs').fetchone()

        assert row[0] == '90210'
        assert row[1] == 'CA'
        assert row[2] > 0
