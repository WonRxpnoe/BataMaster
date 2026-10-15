import sqlite3

import pytest

import clean_leads
from clean_leads import BROWSER_USER_AGENT, check_email_mx, check_website_live, clean_and_validate_leads
from init_db import init_db

DB_PATH = 'data/leads.db'


class FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def stub_mx(monkeypatch, domains_with_mx):
    """Resolve MX records only for the listed domains, mimicking NXDOMAIN for the rest."""
    def fake_resolve(domain, record_type):
        if domain in domains_with_mx:
            return ['mx1.' + domain]
        raise Exception(f'no {record_type} record for {domain}')

    monkeypatch.setattr(clean_leads.dns.resolver, 'resolve', fake_resolve)


def stub_get(monkeypatch, status_by_url):
    """Answer requests from a lookup table and record every call made."""
    calls = []

    def fake_get(url, timeout=None, allow_redirects=None, headers=None, stream=None):
        calls.append({'url': url, 'headers': headers, 'stream': stream})
        if url not in status_by_url:
            raise Exception('connection refused')
        return FakeResponse(status_by_url[url])

    monkeypatch.setattr(clean_leads.requests, 'get', fake_get)
    return calls


class TestCheckEmailMx:
    @pytest.mark.parametrize('email', [None, '', 'not-an-email', 'missing@tld', 'spaces in@example.com'])
    def test_rejects_malformed_addresses_without_any_lookup(self, email, monkeypatch):
        def explode(domain, record_type):
            raise AssertionError('DNS should not be queried for a malformed address')

        monkeypatch.setattr(clean_leads.dns.resolver, 'resolve', explode)
        assert check_email_mx(email) is False

    def test_accepts_domain_with_mx_records(self, monkeypatch):
        stub_mx(monkeypatch, {'example.com'})
        assert check_email_mx('sales@example.com') is True

    def test_rejects_domain_without_mx_records(self, monkeypatch):
        stub_mx(monkeypatch, set())
        assert check_email_mx('sales@example.com') is False

    def test_rejects_when_mx_lookup_returns_nothing(self, monkeypatch):
        monkeypatch.setattr(clean_leads.dns.resolver, 'resolve', lambda domain, record_type: [])
        assert check_email_mx('sales@example.com') is False

    def test_anchored_regex_rejects_address_embedded_in_text(self, monkeypatch):
        stub_mx(monkeypatch, {'example.com'})
        assert check_email_mx('mailto:sales@example.com please') is False


class TestCheckWebsiteLive:
    @pytest.mark.parametrize('website', [None, ''])
    def test_rejects_empty_website(self, website):
        assert check_website_live(website) is False

    @pytest.mark.parametrize('status', [200, 202, 204, 301, 302, 307, 308, 399])
    def test_accepts_every_status_below_400(self, monkeypatch, status):
        stub_get(monkeypatch, {'https://glow.example.com': status})
        assert check_website_live('https://glow.example.com') is True

    @pytest.mark.parametrize('status', [400, 403, 404, 410, 500, 503])
    def test_rejects_error_statuses(self, monkeypatch, status):
        stub_get(monkeypatch, {'https://glow.example.com': status})
        assert check_website_live('https://glow.example.com') is False

    def test_sends_a_browser_user_agent(self, monkeypatch):
        # Without this header sites behind bot protection answer 403 and look dead
        calls = stub_get(monkeypatch, {'https://glow.example.com': 200})
        check_website_live('https://glow.example.com')
        assert calls[0]['headers']['User-Agent'] == BROWSER_USER_AGENT

    def test_streams_the_response_instead_of_downloading_the_page(self, monkeypatch):
        calls = stub_get(monkeypatch, {'https://glow.example.com': 200})
        check_website_live('https://glow.example.com')
        assert calls[0]['stream'] is True

    def test_prepends_https_to_a_bare_host(self, monkeypatch):
        calls = stub_get(monkeypatch, {'https://glow.example.com': 200})
        assert check_website_live('glow.example.com') is True
        assert calls[0]['url'] == 'https://glow.example.com'

    def test_rejects_unreachable_host(self, monkeypatch):
        stub_get(monkeypatch, {})
        assert check_website_live('https://glow.example.com') is False


class TestCleanAndValidateLeads:
    @pytest.fixture(autouse=True)
    def database(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        init_db()

    def insert(self, **overrides):
        lead = {
            'business_name': 'Glow Med Spa',
            'phone': '(310) 555-0100',
            'website': 'https://glow.example.com',
            'email': 'sales@example.com',
            'zip_code': '90210',
        }
        lead.update(overrides)
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                'INSERT INTO leads (business_name, phone, website, email, zip_code) VALUES (?, ?, ?, ?, ?)',
                (lead['business_name'], lead['phone'], lead['website'], lead['email'], lead['zip_code']),
            )

    def fetch(self):
        with sqlite3.connect(DB_PATH) as conn:
            return conn.execute(
                'SELECT business_name, status, website_live FROM leads ORDER BY business_name'
            ).fetchall()

    def pass_everything(self, monkeypatch):
        stub_mx(monkeypatch, {'example.com'})
        stub_get(monkeypatch, {'https://glow.example.com': 200})

    def test_marks_a_fully_valid_lead(self, monkeypatch, capsys):
        self.insert()
        self.pass_everything(monkeypatch)

        clean_and_validate_leads()

        assert self.fetch() == [('Glow Med Spa', 'valid', 1)]
        out = capsys.readouterr().out
        assert '[VALID] Glow Med Spa' in out
        assert 'Marked 1 valid and 0 invalid, nothing was deleted' in out

    def test_never_deletes_rows(self, monkeypatch):
        self.insert(business_name='Unreachable Spa', phone='', email='', website='')
        stub_mx(monkeypatch, set())
        stub_get(monkeypatch, {})

        clean_and_validate_leads()

        assert self.fetch() == [('Unreachable Spa', 'invalid', None)]

    def test_phone_alone_keeps_the_lead(self, monkeypatch):
        self.insert(email='')
        stub_mx(monkeypatch, set())
        stub_get(monkeypatch, {'https://glow.example.com': 200})

        clean_and_validate_leads()

        assert self.fetch() == [('Glow Med Spa', 'valid', 1)]

    def test_email_alone_keeps_the_lead(self, monkeypatch):
        self.insert(phone='')
        self.pass_everything(monkeypatch)

        clean_and_validate_leads()

        assert self.fetch() == [('Glow Med Spa', 'valid', 1)]

    def test_a_dead_website_no_longer_rejects_a_contactable_lead(self, monkeypatch):
        self.insert()
        stub_mx(monkeypatch, {'example.com'})
        stub_get(monkeypatch, {'https://glow.example.com': 404})

        clean_and_validate_leads()

        # Still valid, but the dead site is recorded so it can be filtered later
        assert self.fetch() == [('Glow Med Spa', 'valid', 0)]

    def test_website_live_is_null_when_there_is_no_website(self, monkeypatch):
        self.insert(website='')
        self.pass_everything(monkeypatch)

        clean_and_validate_leads()

        assert self.fetch() == [('Glow Med Spa', 'valid', None)]

    @pytest.mark.parametrize('phone', ['', '555-0100', 'call us'])
    def test_marks_invalid_when_neither_phone_nor_email_works(self, monkeypatch, phone):
        self.insert(phone=phone, email='')
        stub_mx(monkeypatch, set())
        stub_get(monkeypatch, {'https://glow.example.com': 200})

        clean_and_validate_leads()

        assert self.fetch() == [('Glow Med Spa', 'invalid', 1)]

    def test_a_transient_failure_can_be_recovered_by_rerunning(self, monkeypatch):
        self.insert()
        stub_mx(monkeypatch, {'example.com'})
        stub_get(monkeypatch, {})

        clean_and_validate_leads()
        assert self.fetch() == [('Glow Med Spa', 'valid', 0)]

        # The row still exists, so a later run can correct the website verdict
        stub_get(monkeypatch, {'https://glow.example.com': 200})
        clean_and_validate_leads()
        assert self.fetch() == [('Glow Med Spa', 'valid', 1)]

    def test_reports_counts_for_a_mixed_batch(self, monkeypatch, capsys):
        self.insert(business_name='Good Spa')
        self.insert(business_name='Phone Only Spa', email='', website='')
        self.insert(business_name='Unreachable Spa', phone='123', email='', website='')
        self.pass_everything(monkeypatch)

        clean_and_validate_leads()

        assert self.fetch() == [
            ('Good Spa', 'valid', 1),
            ('Phone Only Spa', 'valid', None),
            ('Unreachable Spa', 'invalid', None),
        ]
        out = capsys.readouterr().out
        assert 'Marked 2 valid and 1 invalid, nothing was deleted' in out
        assert 'Of the valid leads, 1 also have a live website' in out

    def test_handles_an_empty_database(self, monkeypatch, capsys):
        self.pass_everything(monkeypatch)

        clean_and_validate_leads()

        out = capsys.readouterr().out
        assert 'Cleaning 0 raw leads' in out
        assert 'Marked 0 valid and 0 invalid, nothing was deleted' in out
