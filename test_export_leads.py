import logging
import sqlite3
import zlib

import pandas as pd
import pytest

from export_leads import DEFAULT_OUTPUT, EXPORT_COLUMNS, build_query, export_leads, main, parse_args
from init_db import DB_PATH, init_db


@pytest.fixture(autouse=True)
def capture_logs(caplog):
    caplog.set_level(logging.DEBUG, logger='export_leads')
    return caplog


@pytest.fixture(autouse=True)
def workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    init_db()
    return tmp_path


def insert(business_name, status='valid', website_live=1, **overrides):
    # Every fake business gets its own street number. Listings are only ever compared
    # within one address, so sharing one would make the exporter rightly read these as
    # repeat listings of a single shop and merge them.
    street_number = zlib.crc32(business_name.encode()) % 9000 + 100
    row = {
        'business_name': business_name,
        'industry': 'MedSpa/Esthetics (Med Spa)',
        'phone': '(310) 555-0100',
        'email': 'sales@example.com',
        'website': 'https://glow.example.com',
        'address': f'{street_number} Main St, Beverly Hills, CA 90210, USA',
        'zip_code': '90210',
        'state': 'CA',
        'status': status,
        'website_live': website_live,
        'source': 'Google_Places',
        'place_id': f'place-{business_name}',
    }
    row.update(overrides)
    columns = ', '.join(row)
    placeholders = ', '.join('?' for _ in row)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(f'INSERT INTO leads ({columns}) VALUES ({placeholders})', list(row.values()))


class TestBuildQuery:
    def test_filters_by_status(self):
        query, params = build_query(status='valid', live_site_only=False)
        assert 'WHERE status = ?' in query
        assert params == ['valid']

    def test_filters_by_live_site(self):
        query, params = build_query(status=None, live_site_only=True)
        assert 'WHERE website_live = 1' in query
        assert params == []

    def test_combines_both_filters(self):
        query, params = build_query(status='valid', live_site_only=True)
        assert 'status = ? AND website_live = 1' in query
        assert params == ['valid']

    def test_no_filters_selects_everything(self):
        query, params = build_query(status=None, live_site_only=False)
        assert 'WHERE' not in query
        assert params == []

    def test_always_orders_by_name(self):
        query, _ = build_query(status=None, live_site_only=False)
        assert query.endswith('ORDER BY business_name')


class TestExportLeads:
    def test_writes_the_expected_columns(self, workdir):
        insert('Glow Med Spa')

        assert export_leads() == 1

        frame = pd.read_csv(workdir / DEFAULT_OUTPUT)
        assert list(frame.columns) == EXPORT_COLUMNS
        assert frame.loc[0, 'business_name'] == 'Glow Med Spa'
        assert frame.loc[0, 'email'] == 'sales@example.com'

    def test_exports_only_valid_leads_by_default(self, workdir):
        insert('Good Spa', status='valid')
        insert('Rejected Spa', status='invalid')
        insert('Unchecked Spa', status='new')

        assert export_leads() == 1

        frame = pd.read_csv(workdir / DEFAULT_OUTPUT)
        assert frame['business_name'].tolist() == ['Good Spa']

    def test_status_none_exports_every_lead(self, workdir):
        insert('Good Spa', status='valid')
        insert('Rejected Spa', status='invalid')

        assert export_leads(status=None) == 2

        frame = pd.read_csv(workdir / DEFAULT_OUTPUT)
        assert frame['business_name'].tolist() == ['Good Spa', 'Rejected Spa']

    def test_live_site_filter_drops_dead_websites(self, workdir):
        insert('Live Spa', website_live=1)
        insert('Dead Site Spa', website_live=0)
        insert('No Site Spa', website_live=None)

        assert export_leads(live_site_only=True) == 1

        frame = pd.read_csv(workdir / DEFAULT_OUTPUT)
        assert frame['business_name'].tolist() == ['Live Spa']

    def test_rows_are_sorted_by_business_name(self, workdir):
        insert('Zen Spa')
        insert('Alpha Spa')

        export_leads()

        frame = pd.read_csv(workdir / DEFAULT_OUTPUT)
        assert frame['business_name'].tolist() == ['Alpha Spa', 'Zen Spa']

    def test_writes_a_header_only_file_when_nothing_matches(self, workdir, capture_logs):
        assert export_leads() == 0

        frame = pd.read_csv(workdir / DEFAULT_OUTPUT)
        assert frame.empty
        assert list(frame.columns) == EXPORT_COLUMNS
        assert 'No leads matched the filters' in capture_logs.text

    def test_creates_a_missing_output_directory(self, workdir):
        insert('Glow Med Spa')

        export_leads(output_path='exports/nested/leads.csv')

        assert (workdir / 'exports/nested/leads.csv').is_file()

    def test_accepts_a_bare_filename(self, workdir):
        insert('Glow Med Spa')

        export_leads(output_path='leads.csv')

        assert (workdir / 'leads.csv').is_file()


class TestRepeatListingsBecomeOneBusiness:
    """Google lists some businesses more than once, so one shop arrives several times."""

    SHARED = {'address': '1 Main St, Beverly Hills, CA 90210, USA'}

    def names(self, workdir):
        return pd.read_csv(workdir / DEFAULT_OUTPUT)['business_name'].tolist()

    def test_the_same_shop_listed_twice_appears_once(self, workdir):
        insert('Glow Med Spa', **self.SHARED)
        insert('Glow Med Spa', place_id='other-listing', **self.SHARED)

        assert export_leads() == 1
        assert self.names(workdir) == ['Glow Med Spa']

    def test_a_renamed_duplicate_is_still_one_shop(self, workdir):
        # Google punctuates its duplicate entries differently, so the name cannot decide
        insert('Quiet Esthetics', phone='', **self.SHARED)
        insert('Quiet Esthetics LLC', phone='', place_id='other-listing', **self.SHARED)

        assert export_leads() == 1

    def test_branches_of_a_chain_are_kept_apart(self, workdir):
        # One central phone number and one website, but two separate shops to contact
        insert('Radiance Skin Clinic', address='12 Rodeo Dr, Beverly Hills, CA 90210, USA')
        insert('Radiance Skin Clinic', place_id='second-branch',
               address='88 Sunset Blvd, Beverly Hills, CA 90210, USA')

        assert export_leads() == 2

    def test_a_neighbour_in_the_same_building_is_not_absorbed(self, workdir):
        insert('Glow Med Spa', **self.SHARED)
        insert('Serenity Facial Bar', phone='(310) 555-9999', email='hi@serenity.example',
               website='https://serenity.example', **self.SHARED)

        assert export_leads() == 2

    def test_the_surviving_row_keeps_every_contact_detail_found(self, workdir):
        # Each listing carries a different fragment, and the lead needs all of them
        insert('Glow Med Spa', email='', website='', **self.SHARED)
        insert('Glow Medspa', email='', place_id='listing-2', **self.SHARED)
        insert('Glow Med Spa Inc', phone='', place_id='listing-3', **self.SHARED)

        assert export_leads() == 1

        row = pd.read_csv(workdir / DEFAULT_OUTPUT).loc[0]
        assert row['phone'] == '(310) 555-0100'
        assert row['website'] == 'https://glow.example.com'
        assert row['email'] == 'sales@example.com'

    def test_the_merged_row_carries_the_most_favourable_verdict(self, workdir):
        # The rejected listing was judged before the other one supplied a phone number
        insert('Glow Med Spa', status='invalid', phone='', email='', **self.SHARED)
        insert('Glow Med Spa', status='valid', place_id='other-listing', **self.SHARED)

        assert export_leads(status=None) == 1

        row = pd.read_csv(workdir / DEFAULT_OUTPUT).loc[0]
        assert row['phone'] == '(310) 555-0100'
        assert row['status'] == 'valid'

    def test_a_site_that_answered_for_one_listing_counts_for_the_business(self, workdir):
        insert('Glow Med Spa', website_live=0, **self.SHARED)
        insert('Glow Med Spa', website_live=1, place_id='other-listing', **self.SHARED)

        export_leads()

        assert pd.read_csv(workdir / DEFAULT_OUTPUT).loc[0, 'website_live'] == 1

    def test_a_business_with_no_website_keeps_an_empty_liveness_flag(self, workdir):
        insert('Glow Med Spa', website='', website_live=None, **self.SHARED)
        insert('Glow Med Spa', website='', website_live=None, place_id='other-listing',
               **self.SHARED)

        export_leads()

        assert pd.read_csv(workdir / DEFAULT_OUTPUT).loc[0, 'website_live'] != 1

    def test_merging_is_reported(self, workdir, capture_logs):
        insert('Glow Med Spa', **self.SHARED)
        insert('Glow Med Spa', place_id='other-listing', **self.SHARED)

        export_leads()

        assert 'Merged 1 repeat listing' in capture_logs.text

    def test_nothing_is_reported_when_no_business_repeats(self, workdir, capture_logs):
        insert('Glow Med Spa')
        insert('Radiance Skin Clinic')

        assert export_leads() == 2
        assert 'Merged' not in capture_logs.text

    def test_an_empty_list_is_left_alone(self, workdir):
        assert export_leads() == 0


class TestCommandLine:
    def test_defaults(self):
        args = parse_args([])
        assert args.output == DEFAULT_OUTPUT
        assert args.status == 'valid'
        assert args.live_site_only is False

    def test_parses_the_options(self):
        args = parse_args(['--output', 'out.csv', '--status', 'all', '--live-site-only'])
        assert args.output == 'out.csv'
        assert args.status == 'all'
        assert args.live_site_only is True

    def test_rejects_an_unknown_status(self):
        with pytest.raises(SystemExit):
            parse_args(['--status', 'maybe'])

    def test_main_exports_all_statuses(self, workdir):
        insert('Good Spa', status='valid')
        insert('Rejected Spa', status='invalid')

        assert main(['--status', 'all', '--output', 'out.csv']) == 0

        frame = pd.read_csv(workdir / 'out.csv')
        assert len(frame) == 2
