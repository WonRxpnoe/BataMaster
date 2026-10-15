import asyncio
import sqlite3

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

import medspa_scraper
from init_db import DB_PATH, init_db
from medspa_scraper import (
    FatalApiError,
    KEYWORDS,
    MAX_CONCURRENT_REQUESTS,
    check_api_response,
    extract_zip_code,
    load_place_details,
    run_medspa_scraper,
    save_to_sqlite,
)


def place(name, **overrides):
    payload = {
        'name': name,
        'place_id': f'place-{name}',
        'formatted_address': '1 Main St, Beverly Hills, CA 90210, USA',
        'business_status': 'OPERATIONAL',
    }
    payload.update(overrides)
    return payload


def lead(**overrides):
    payload = {
        'business_name': 'Glow Med Spa',
        'industry': 'MedSpa/Esthetics (Med Spa)',
        'phone': '(310) 555-0100',
        'website': 'https://glow.example.com',
        'email': 'hello@glow.example.com',
        'state': 'CA',
        'zip_code': '90210',
        'address': '1 Main St',
        'place_id': 'place-Glow Med Spa',
        'source': 'Google_Places',
    }
    payload.update(overrides)
    return payload


class FakeGoogle:
    """A local stand-in for the two Places endpoints, exercised over real HTTP."""

    def __init__(self):
        self.search_payload = {'status': 'OK', 'results': []}
        self.search_status = 200
        self.details_payload = {'status': 'OK', 'result': {}}
        self.details_status = 200
        self.details_by_place_id = {}
        self.details_delay = 0
        self.search_queries = []
        self.details_requests = []
        self.in_flight = 0
        self.peak_in_flight = 0

    async def handle_search(self, request):
        self.search_queries.append(request.query.get('query'))
        return web.json_response(self.search_payload, status=self.search_status)

    async def handle_details(self, request):
        place_id = request.query.get('place_id')
        self.details_requests.append(place_id)

        self.in_flight += 1
        self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
        try:
            if self.details_delay:
                await asyncio.sleep(self.details_delay)
            payload = self.details_by_place_id.get(place_id, self.details_payload)
            return web.json_response(payload, status=self.details_status)
        finally:
            self.in_flight -= 1


@pytest.fixture
async def google(monkeypatch):
    fake = FakeGoogle()
    app = web.Application()
    app.router.add_get('/textsearch', fake.handle_search)
    app.router.add_get('/details', fake.handle_details)

    server = TestServer(app)
    await server.start_server()
    monkeypatch.setattr(medspa_scraper, 'SEARCH_URL', str(server.make_url('/textsearch')))
    monkeypatch.setattr(medspa_scraper, 'DETAILS_URL', str(server.make_url('/details')))

    yield fake
    await server.close()


@pytest.fixture
def no_email_lookup(monkeypatch):
    """Keep run_medspa_scraper from touching real websites."""
    emails = {}

    async def fake_fetch(session, website_url):
        return emails.get(website_url, '')

    monkeypatch.setattr(medspa_scraper, 'fetch_email_from_website', fake_fetch)
    return emails


@pytest.fixture
async def session():
    async with aiohttp.ClientSession() as client_session:
        yield client_session


class TestCheckApiResponse:
    def test_returns_payload_for_ok(self):
        payload = {'status': 'OK', 'results': []}
        assert check_api_response(200, payload, 'ctx') == payload

    def test_treats_zero_results_as_success(self, capsys):
        payload = {'status': 'ZERO_RESULTS', 'results': []}
        assert check_api_response(200, payload, 'ctx') == payload
        assert capsys.readouterr().out == ''

    def test_reports_non_200(self, capsys):
        assert check_api_response(503, {}, 'ctx') is None
        assert 'ctx failed: HTTP 503' in capsys.readouterr().out

    def test_reports_recoverable_status_without_raising(self, capsys):
        payload = {'status': 'INVALID_REQUEST', 'error_message': 'Missing the query parameter.'}
        assert check_api_response(200, payload, 'ctx') is None
        out = capsys.readouterr().out
        assert 'INVALID_REQUEST' in out
        assert 'Missing the query parameter.' in out

    def test_defaults_missing_status_to_unknown_error(self, capsys):
        assert check_api_response(200, {'results': []}, 'ctx') is None
        out = capsys.readouterr().out
        assert 'UNKNOWN_ERROR' in out
        assert 'no error_message returned' in out

    @pytest.mark.parametrize('status', ['REQUEST_DENIED', 'OVER_QUERY_LIMIT'])
    def test_raises_on_account_level_failure(self, status):
        with pytest.raises(FatalApiError, match=status):
            check_api_response(200, {'status': status, 'error_message': 'nope'}, 'ctx')


class TestExtractZipCode:
    @pytest.mark.parametrize(
        'address, expected',
        [
            ('1 Main St, Beverly Hills, CA 90210, USA', '90210'),
            ('1 Main St, Beverly Hills, CA 90210-1234, USA', '90210'),
            ('500 Industrial Pkwy, Reno, NV 89502', '89502'),
            # A five digit street number must not be mistaken for the ZIP
            ('90210 Wilshire Blvd, Los Angeles, CA 90024', '90024'),
        ],
    )
    def test_reads_the_zip_that_follows_the_state_code(self, address, expected):
        assert extract_zip_code(address, '00000') == expected

    @pytest.mark.parametrize('address', [None, '', 'Somewhere in France'])
    def test_falls_back_to_the_search_zip(self, address):
        assert extract_zip_code(address, 90210) == '90210'


class TestLoadPlaceDetails:
    async def test_calls_the_api_once_per_place_id(self, google, session):
        cache = {}
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

        avoided = await load_place_details(session, 'key', ['place-1', 'place-1'], cache, semaphore)

        assert google.details_requests == ['place-1']
        assert avoided == 1
        assert set(cache) == {'place-1'}

    async def test_skips_place_ids_already_cached(self, google, session):
        cache = {'place-1': {'website': 'https://cached.example.com'}}
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

        avoided = await load_place_details(session, 'key', ['place-1', 'place-2'], cache, semaphore)

        assert google.details_requests == ['place-2']
        assert avoided == 1
        assert cache['place-1'] == {'website': 'https://cached.example.com'}

    async def test_propagates_a_fatal_error(self, google, session):
        google.details_payload = {'status': 'REQUEST_DENIED', 'error_message': 'invalid key'}
        with pytest.raises(FatalApiError):
            await load_place_details(session, 'key', ['place-1'], {}, asyncio.Semaphore(2))

    async def test_recoverable_error_caches_an_empty_result(self, google, session):
        google.details_payload = {'status': 'NOT_FOUND'}
        cache = {}

        await load_place_details(session, 'key', ['place-1'], cache, asyncio.Semaphore(2))
        await load_place_details(session, 'key', ['place-1'], cache, asyncio.Semaphore(2))

        assert cache == {'place-1': {}}
        # The second pass is served from the cache rather than repeating a failing call
        assert google.details_requests == ['place-1']

    async def test_never_exceeds_the_concurrency_limit(self, google, session):
        google.details_delay = 0.02
        place_ids = [f'place-{i}' for i in range(MAX_CONCURRENT_REQUESTS * 3)]

        await load_place_details(session, 'key', place_ids, {}, asyncio.Semaphore(MAX_CONCURRENT_REQUESTS))

        assert len(google.details_requests) == len(place_ids)
        assert google.peak_in_flight <= MAX_CONCURRENT_REQUESTS
        # The requests really did overlap rather than running one at a time
        assert google.peak_in_flight > 1


class TestRunMedspaScraper:
    async def test_prefers_details_over_search_payload(self, google, no_email_lookup):
        no_email_lookup['https://glow.example.com'] = 'hello@glow.example.com'
        google.search_payload = {'status': 'OK', 'results': [place('Glow Med Spa')]}
        google.details_payload = {
            'status': 'OK',
            'result': {
                'formatted_address': '9 Rodeo Dr, Beverly Hills, CA 90212, USA',
                'formatted_phone_number': '(310) 555-0100',
                'website': 'https://glow.example.com',
            },
        }

        leads = await run_medspa_scraper('key', zip_code='90210', state='CA')

        assert leads[0]['address'] == '9 Rodeo Dr, Beverly Hills, CA 90212, USA'
        assert leads[0]['phone'] == '(310) 555-0100'
        assert leads[0]['email'] == 'hello@glow.example.com'

    async def test_stores_the_business_zip_not_the_search_zip(self, google, no_email_lookup):
        # A text search for 90210 also returns businesses in neighbouring zips
        google.search_payload = {
            'status': 'OK',
            'results': [place('Nearby Spa', formatted_address='9 Rodeo Dr, Beverly Hills, CA 90212, USA')],
        }

        leads = await run_medspa_scraper('key', zip_code='90210', state='CA')

        assert leads[0]['zip_code'] == '90212'

    async def test_keeps_the_place_id_on_the_lead(self, google, no_email_lookup):
        google.search_payload = {'status': 'OK', 'results': [place('Glow Med Spa')]}
        leads = await run_medspa_scraper('key')
        assert leads[0]['place_id'] == 'place-Glow Med Spa'

    async def test_falls_back_to_search_address_when_details_are_empty(self, google, no_email_lookup):
        google.search_payload = {'status': 'OK', 'results': [place('Glow Med Spa')]}
        leads = await run_medspa_scraper('key')
        assert leads[0]['address'] == '1 Main St, Beverly Hills, CA 90210, USA'
        assert leads[0]['phone'] == ''
        assert leads[0]['website'] == ''
        assert leads[0]['email'] == ''

    async def test_skips_permanently_closed_businesses(self, google, no_email_lookup):
        google.search_payload = {
            'status': 'OK',
            'results': [place('Open Spa'), place('Gone Spa', business_status='CLOSED_PERMANENTLY')],
        }

        leads = await run_medspa_scraper('key')

        assert {item['business_name'] for item in leads} == {'Open Spa'}
        # A closed business must not cost a details lookup either
        assert google.details_requests == ['place-Open Spa']

    async def test_searches_every_keyword(self, google, no_email_lookup):
        await run_medspa_scraper('key', zip_code='90210', state='CA')
        assert google.search_queries == [f'{kw} in 90210, CA' for kw in KEYWORDS]

    async def test_business_matching_every_keyword_is_billed_once(self, google, no_email_lookup, capsys):
        google.search_payload = {'status': 'OK', 'results': [place('Glow Med Spa')]}
        google.details_payload = {'status': 'OK', 'result': {'formatted_phone_number': '(310) 555-0100'}}

        leads = await run_medspa_scraper('key')

        assert google.details_requests == ['place-Glow Med Spa']
        assert len(leads) == len(KEYWORDS)
        assert all(item['phone'] == '(310) 555-0100' for item in leads)
        assert f'1 unique businesses, {len(KEYWORDS) - 1} duplicate lookups avoided' in capsys.readouterr().out

    async def test_rejected_key_aborts_and_carries_partial_results(self, google, no_email_lookup):
        google.search_payload = {'status': 'REQUEST_DENIED', 'error_message': 'The provided API key is invalid.'}

        with pytest.raises(FatalApiError) as excinfo:
            await run_medspa_scraper('bad-key')

        assert excinfo.value.partial_leads == []
        # The remaining keywords are skipped rather than repeating the same failure
        assert len(google.search_queries) == 1

    async def test_partial_results_survive_a_mid_run_quota_failure(self, google, no_email_lookup):
        google.search_payload = {'status': 'OK', 'results': [place('Glow Med Spa')]}
        google.details_by_place_id['place-Glow Med Spa'] = {'status': 'OK', 'result': {}}

        original = medspa_scraper.fetch_api_json
        seen = {'searches': 0}

        async def failing_after_first_keyword(session, url, params, context):
            if url == medspa_scraper.SEARCH_URL:
                seen['searches'] += 1
                if seen['searches'] == 2:
                    raise FatalApiError('OVER_QUERY_LIMIT - quota exhausted')
            return await original(session, url, params, context)

        medspa_scraper.fetch_api_json = failing_after_first_keyword
        try:
            with pytest.raises(FatalApiError) as excinfo:
                await run_medspa_scraper('key')
        finally:
            medspa_scraper.fetch_api_json = original

        # Everything gathered before the quota ran out is still available to save
        assert [item['business_name'] for item in excinfo.value.partial_leads] == ['Glow Med Spa']

    async def test_recoverable_search_error_keeps_other_keywords_running(self, google, no_email_lookup, capsys):
        google.search_payload = {'status': 'INVALID_REQUEST', 'error_message': 'bad query'}

        leads = await run_medspa_scraper('key')

        assert leads == []
        assert capsys.readouterr().out.count('INVALID_REQUEST') == len(KEYWORDS)
        assert len(google.search_queries) == len(KEYWORDS)

    async def test_non_200_search_response_is_reported(self, google, no_email_lookup, capsys):
        google.search_status = 503

        leads = await run_medspa_scraper('key')

        assert leads == []
        assert capsys.readouterr().out.count('HTTP 503') == len(KEYWORDS)


class TestSaveToSqlite:
    @pytest.fixture(autouse=True)
    def workdir(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)

    def test_creates_the_database_without_running_init_db_first(self, tmp_path):
        save_to_sqlite([lead()])

        assert (tmp_path / DB_PATH).is_file()
        with sqlite3.connect(DB_PATH) as conn:
            assert conn.execute('SELECT COUNT(*) FROM leads').fetchone()[0] == 1

    def test_does_nothing_for_empty_input(self, tmp_path, capsys):
        save_to_sqlite([])
        assert 'No valid leads were scraped' in capsys.readouterr().out
        assert not (tmp_path / DB_PATH).exists()

    def test_persists_all_lead_columns(self):
        save_to_sqlite([lead()])
        with sqlite3.connect(DB_PATH) as conn:
            row = conn.execute(
                'SELECT business_name, industry, phone, website, email, state, zip_code, address, source, place_id '
                'FROM leads'
            ).fetchone()
        assert row == (
            'Glow Med Spa',
            'MedSpa/Esthetics (Med Spa)',
            '(310) 555-0100',
            'https://glow.example.com',
            'hello@glow.example.com',
            'CA',
            '90210',
            '1 Main St',
            'Google_Places',
            'place-Glow Med Spa',
        )

    def test_same_place_id_is_only_stored_once(self, capsys):
        # The same business under two keywords, and now under two different zips
        save_to_sqlite([lead(), lead(industry='MedSpa/Esthetics (Facial Spa)', zip_code='90212')])

        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute('SELECT industry FROM leads').fetchall()
        assert rows == [('MedSpa/Esthetics (Med Spa)',)]
        assert 'Saved 1 new leads' in capsys.readouterr().out

    def test_distinct_businesses_are_all_stored(self):
        save_to_sqlite([lead(), lead(business_name='Other Spa', place_id='place-Other Spa')])
        with sqlite3.connect(DB_PATH) as conn:
            assert conn.execute('SELECT COUNT(*) FROM leads').fetchone()[0] == 2

    def test_reports_inserted_count_and_coverage(self, capsys):
        init_db()
        save_to_sqlite([lead(), lead(business_name='Bare Spa', place_id='p2', phone='', email='', address='')])

        out = capsys.readouterr().out
        assert 'Saved 2 new leads' in out
        assert 'Coverage of fetched leads: 1 with address, 1 with phone, 1 with email' in out


def html_response(body):
    async def handler(request):
        return web.Response(text=f'<html><body>{body}</body></html>', content_type='text/html')

    return handler


class TestFetchEmailFromWebsite:
    @pytest.fixture
    async def website(self):
        async def not_found(request):
            return web.Response(text='support@example.org', status=404, content_type='text/html')

        app = web.Application()
        app.router.add_get('/with-email', html_response('Contact: sales@example.com'))
        app.router.add_get('/without-email', html_response('No contact details here'))
        app.router.add_get('/image-only', html_response('<img src="logo@2x.png">'))
        app.router.add_get('/svg-only', html_response('<img src="icon@1x.svg">'))
        app.router.add_get('/jpeg-only', html_response('<img src="hero@2x.jpeg">'))
        app.router.add_get('/image-and-email', html_response('<img src="banner@3x.jpg">support@example.org'))
        app.router.add_get(
            '/many-emails',
            html_response('first@example.com then second@example.com then third@example.com'),
        )
        app.router.add_get('/not-found', not_found)

        server = TestServer(app)
        await server.start_server()
        yield server
        await server.close()

    @pytest.mark.parametrize('website_url', [None, '', 123, []])
    async def test_returns_empty_string_for_invalid_input(self, website_url):
        assert await medspa_scraper.fetch_email_from_website(None, website_url) == ''

    async def test_extracts_email_from_html(self, session, website):
        url = str(website.make_url('/with-email'))
        assert await medspa_scraper.fetch_email_from_website(session, url) == 'sales@example.com'

    async def test_returns_the_first_address_in_document_order(self, session, website):
        # Deduplicating through a set used to make this vary between runs
        url = str(website.make_url('/many-emails'))
        results = {await medspa_scraper.fetch_email_from_website(session, url) for _ in range(5)}
        assert results == {'first@example.com'}

    async def test_returns_empty_string_when_page_has_no_email(self, session, website):
        url = str(website.make_url('/without-email'))
        assert await medspa_scraper.fetch_email_from_website(session, url) == ''

    @pytest.mark.parametrize('path', ['/image-only', '/svg-only', '/jpeg-only'])
    async def test_ignores_image_filenames_that_look_like_emails(self, session, website, path):
        url = str(website.make_url(path))
        assert await medspa_scraper.fetch_email_from_website(session, url) == ''

    async def test_picks_real_email_over_image_filename(self, session, website):
        url = str(website.make_url('/image-and-email'))
        assert await medspa_scraper.fetch_email_from_website(session, url) == 'support@example.org'

    async def test_returns_empty_string_on_non_200_status(self, session, website):
        url = str(website.make_url('/not-found'))
        assert await medspa_scraper.fetch_email_from_website(session, url) == ''

    async def test_returns_empty_string_when_host_is_unreachable(self, session):
        assert await medspa_scraper.fetch_email_from_website(session, 'http://127.0.0.1:1/') == ''

    async def test_prepends_https_scheme_for_bare_host(self, session):
        # No scheme means the helper retries against https://, which cannot resolve here
        assert await medspa_scraper.fetch_email_from_website(session, 'host.invalid') == ''
