import logging
import sqlite3

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
    row = {
        'business_name': business_name,
        'industry': 'MedSpa/Esthetics (Med Spa)',
        'phone': '(310) 555-0100',
        'email': 'sales@example.com',
        'website': 'https://glow.example.com',
        'address': '1 Main St, Beverly Hills, CA 90210, USA',
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
