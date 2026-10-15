import sqlite3

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

import medspa_scraper
from init_db import init_db
from medspa_scraper import (
    FatalApiError,
    KEYWORDS,
    check_api_response,
    get_cached_place_details,
    get_place_details,
    run_medspa_scraper,
    save_to_sqlite,
)

DB_PATH = 'data/leads.db'

SEARCH_URL = 'https://maps.googleapis.com/maps/api/place/textsearch/json'
DETAILS_URL = 'https://maps.googleapis.com/maps/api/place/details/json'


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


def place(name, **overrides):
    payload = {
        'name': name,
        'place_id': f'place-{name}',
        'formatted_address': 'Search address',
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
        'source': 'Google_Places',
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def no_email_lookup(monkeypatch):
    """Keep run_medspa_scraper from touching real websites."""
    emails = {}

    async def fake_fetch(session, website_url):
        return emails.get(website_url, '')

    monkeypatch.setattr(medspa_scraper, 'fetch_email_from_website', fake_fetch)
    return emails


def stub_api(monkeypatch, search_payload, details_payload=None):
    """Route each endpoint to a canned payload and record the requests made."""
    calls = []

    def fake_get(url, params=None, timeout=None):
        calls.append((url, params))
        if url == SEARCH_URL:
            return FakeResponse(search_payload)
        return FakeResponse(details_payload if details_payload is not None else {'status': 'OK', 'result': {}})

    monkeypatch.setattr(medspa_scraper.requests, 'get', fake_get)
    return calls


class TestCheckApiResponse:
    def test_returns_payload_for_ok(self):
        payload = {'status': 'OK', 'results': []}
        assert check_api_response(FakeResponse(payload), 'ctx') == payload

    def test_treats_zero_results_as_success(self, capsys):
        payload = {'status': 'ZERO_RESULTS', 'results': []}
        assert check_api_response(FakeResponse(payload), 'ctx') == payload
        assert capsys.readouterr().out == ''

    def test_reports_non_200_without_parsing_body(self, capsys):
        assert check_api_response(FakeResponse({}, status_code=503), 'ctx') is None
        assert 'ctx failed: HTTP 503' in capsys.readouterr().out

    def test_reports_recoverable_status_without_raising(self, capsys):
        payload = {'status': 'INVALID_REQUEST', 'error_message': 'Missing the query parameter.'}
        assert check_api_response(FakeResponse(payload), 'ctx') is None
        out = capsys.readouterr().out
        assert 'INVALID_REQUEST' in out
        assert 'Missing the query parameter.' in out

    def test_defaults_missing_status_to_unknown_error(self, capsys):
        assert check_api_response(FakeResponse({'results': []}), 'ctx') is None
        out = capsys.readouterr().out
        assert 'UNKNOWN_ERROR' in out
        assert 'no error_message returned' in out

    @pytest.mark.parametrize('status', ['REQUEST_DENIED', 'OVER_QUERY_LIMIT'])
    def test_raises_on_account_level_failure(self, status):
        payload = {'status': status, 'error_message': 'nope'}
        with pytest.raises(FatalApiError, match=status):
            check_api_response(FakeResponse(payload), 'ctx')


class TestGetPlaceDetails:
    def test_returns_result_payload(self, monkeypatch):
        details = {'formatted_phone_number': '(310) 555-0100', 'website': 'https://glow.example.com'}
        monkeypatch.setattr(
            medspa_scraper.requests,
            'get',
            lambda url, params=None, timeout=None: FakeResponse({'status': 'OK', 'result': details}),
        )
        assert get_place_details('test-key', 'place-1') == details

    def test_returns_empty_dict_on_recoverable_error(self, monkeypatch, capsys):
        monkeypatch.setattr(
            medspa_scraper.requests,
            'get',
            lambda url, params=None, timeout=None: FakeResponse({'status': 'NOT_FOUND'}),
        )
        assert get_place_details('test-key', 'place-1') == {}
        assert 'NOT_FOUND' in capsys.readouterr().out

    def test_propagates_fatal_error(self, monkeypatch):
        monkeypatch.setattr(
            medspa_scraper.requests,
            'get',
            lambda url, params=None, timeout=None: FakeResponse(
                {'status': 'REQUEST_DENIED', 'error_message': 'The provided API key is invalid.'}
            ),
        )
        with pytest.raises(FatalApiError):
            get_place_details('bad-key', 'place-1')

    def test_reports_transport_error_and_returns_empty_dict(self, monkeypatch, capsys):
        def boom(url, params=None, timeout=None):
            raise RuntimeError('connection reset')

        monkeypatch.setattr(medspa_scraper.requests, 'get', boom)
        assert get_place_details('test-key', 'place-1') == {}
        assert 'Place Details request failed for place-1' in capsys.readouterr().out


class TestGetCachedPlaceDetails:
    def test_calls_the_api_once_per_place_id(self, monkeypatch):
        calls = []

        def fake_get(url, params=None, timeout=None):
            calls.append(params['place_id'])
            return FakeResponse({'status': 'OK', 'result': {'website': 'https://glow.example.com'}})

        monkeypatch.setattr(medspa_scraper.requests, 'get', fake_get)
        cache = {}

        first = get_cached_place_details('test-key', 'place-1', cache)
        second = get_cached_place_details('test-key', 'place-1', cache)

        assert first == second == {'website': 'https://glow.example.com'}
        assert calls == ['place-1']

    def test_keeps_separate_entries_per_place_id(self, monkeypatch):
        calls = []

        def fake_get(url, params=None, timeout=None):
            calls.append(params['place_id'])
            return FakeResponse({'status': 'OK', 'result': {'website': f"https://{params['place_id']}.example.com"}})

        monkeypatch.setattr(medspa_scraper.requests, 'get', fake_get)
        cache = {}

        assert get_cached_place_details('test-key', 'place-1', cache)['website'] == 'https://place-1.example.com'
        assert get_cached_place_details('test-key', 'place-2', cache)['website'] == 'https://place-2.example.com'
        assert calls == ['place-1', 'place-2']

    def test_caches_empty_results_so_failures_are_not_retried(self, monkeypatch):
        calls = []

        def fake_get(url, params=None, timeout=None):
            calls.append(params['place_id'])
            return FakeResponse({'status': 'NOT_FOUND'})

        monkeypatch.setattr(medspa_scraper.requests, 'get', fake_get)
        cache = {}

        assert get_cached_place_details('test-key', 'place-1', cache) == {}
        assert get_cached_place_details('test-key', 'place-1', cache) == {}
        assert calls == ['place-1']

    def test_fatal_error_is_not_cached(self, monkeypatch):
        monkeypatch.setattr(
            medspa_scraper.requests,
            'get',
            lambda url, params=None, timeout=None: FakeResponse(
                {'status': 'REQUEST_DENIED', 'error_message': 'invalid key'}
            ),
        )
        cache = {}
        with pytest.raises(FatalApiError):
            get_cached_place_details('bad-key', 'place-1', cache)
        assert cache == {}


class TestRunMedspaScraper:
    async def test_prefers_details_over_search_payload(self, monkeypatch, no_email_lookup):
        no_email_lookup['https://glow.example.com'] = 'hello@glow.example.com'
        stub_api(
            monkeypatch,
            {'status': 'OK', 'results': [place('Glow Med Spa')]},
            {
                'status': 'OK',
                'result': {
                    'formatted_address': 'Details address',
                    'formatted_phone_number': '(310) 555-0100',
                    'website': 'https://glow.example.com',
                },
            },
        )
        leads = await run_medspa_scraper('test-key', zip_code='90210', state='CA')
        assert leads[0]['address'] == 'Details address'
        assert leads[0]['phone'] == '(310) 555-0100'
        assert leads[0]['email'] == 'hello@glow.example.com'

    async def test_falls_back_to_search_address_when_details_are_empty(self, monkeypatch, no_email_lookup):
        stub_api(monkeypatch, {'status': 'OK', 'results': [place('Glow Med Spa')]})
        leads = await run_medspa_scraper('test-key')
        assert leads[0]['address'] == 'Search address'
        assert leads[0]['phone'] == ''
        assert leads[0]['website'] == ''
        assert leads[0]['email'] == ''

    async def test_skips_permanently_closed_businesses(self, monkeypatch, no_email_lookup):
        stub_api(
            monkeypatch,
            {
                'status': 'OK',
                'results': [place('Open Spa'), place('Gone Spa', business_status='CLOSED_PERMANENTLY')],
            },
        )
        leads = await run_medspa_scraper('test-key')
        assert {item['business_name'] for item in leads} == {'Open Spa'}

    async def test_searches_every_keyword(self, monkeypatch, no_email_lookup):
        calls = stub_api(monkeypatch, {'status': 'OK', 'results': []})
        await run_medspa_scraper('test-key', zip_code='90210', state='CA')
        queries = [params['query'] for url, params in calls if url == SEARCH_URL]
        assert queries == [f'{kw} in 90210, CA' for kw in KEYWORDS]

    async def test_business_matching_every_keyword_is_billed_once(self, monkeypatch, no_email_lookup, capsys):
        # Google returns the same business for several keywords, which used to mean paying per match
        calls = stub_api(
            monkeypatch,
            {'status': 'OK', 'results': [place('Glow Med Spa')]},
            {'status': 'OK', 'result': {'formatted_phone_number': '(310) 555-0100'}},
        )
        leads = await run_medspa_scraper('test-key')

        details_calls = [params['place_id'] for url, params in calls if url == DETAILS_URL]
        assert details_calls == ['place-Glow Med Spa']
        # Every keyword still produces its own labelled lead, now from the cached details
        assert len(leads) == len(KEYWORDS)
        assert all(item['phone'] == '(310) 555-0100' for item in leads)
        assert f'1 unique businesses, {len(KEYWORDS) - 1} duplicate lookups avoided' in capsys.readouterr().out

    async def test_distinct_businesses_are_each_looked_up(self, monkeypatch, no_email_lookup, capsys):
        def fake_get(url, params=None, timeout=None):
            if url == SEARCH_URL:
                name = params['query'].split(' in ')[0]
                return FakeResponse({'status': 'OK', 'results': [place(name)]})
            return FakeResponse({'status': 'OK', 'result': {}})

        monkeypatch.setattr(medspa_scraper.requests, 'get', fake_get)
        await run_medspa_scraper('test-key')
        assert f'{len(KEYWORDS)} unique businesses, 0 duplicate lookups avoided' in capsys.readouterr().out

    async def test_rejected_key_aborts_after_first_request(self, monkeypatch, no_email_lookup, capsys):
        calls = stub_api(
            monkeypatch,
            {'status': 'REQUEST_DENIED', 'error_message': 'The provided API key is invalid.'},
        )
        leads = await run_medspa_scraper('bad-key')

        out = capsys.readouterr().out
        assert leads == []
        assert 'REQUEST_DENIED' in out
        assert 'Aborting search' in out
        # The remaining keywords are skipped rather than repeating the same failure
        assert len(calls) == 1

    async def test_details_quota_failure_aborts_the_run(self, monkeypatch, no_email_lookup, capsys):
        calls = stub_api(
            monkeypatch,
            {'status': 'OK', 'results': [place('Glow Med Spa')]},
            {'status': 'OVER_QUERY_LIMIT', 'error_message': 'Daily quota exceeded.'},
        )
        leads = await run_medspa_scraper('test-key')

        assert 'OVER_QUERY_LIMIT' in capsys.readouterr().out
        assert leads == []
        # One search plus one details call, then the run stops
        assert len(calls) == 2

    async def test_recoverable_search_error_keeps_other_keywords_running(self, monkeypatch, no_email_lookup, capsys):
        def fake_get(url, params=None, timeout=None):
            if url == SEARCH_URL and params['query'].startswith('Med Spa'):
                return FakeResponse({'status': 'INVALID_REQUEST', 'error_message': 'bad query'})
            if url == SEARCH_URL:
                return FakeResponse({'status': 'OK', 'results': [place('Glow Med Spa')]})
            return FakeResponse({'status': 'OK', 'result': {}})

        monkeypatch.setattr(medspa_scraper.requests, 'get', fake_get)
        leads = await run_medspa_scraper('test-key')

        assert 'INVALID_REQUEST' in capsys.readouterr().out
        assert len(leads) == len(KEYWORDS) - 1


class TestSaveToSqlite:
    @pytest.fixture(autouse=True)
    def database(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        init_db()

    def test_reports_when_there_is_nothing_to_save(self, capsys):
        save_to_sqlite([])
        assert 'No valid leads were scraped' in capsys.readouterr().out

    def test_persists_all_lead_columns(self):
        save_to_sqlite([lead()])
        with sqlite3.connect(DB_PATH) as conn:
            row = conn.execute(
                'SELECT business_name, industry, phone, website, email, state, zip_code, address, source FROM leads'
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
        )

    def test_reports_inserted_count_not_attempted_count(self, capsys):
        save_to_sqlite([lead(), lead(industry='MedSpa/Esthetics (Facial Spa)')])

        out = capsys.readouterr().out
        assert 'Saved 1 new leads' in out
        assert 'Fetched 2, skipped 1 already in the database' in out

        with sqlite3.connect(DB_PATH) as conn:
            assert conn.execute('SELECT COUNT(*) FROM leads').fetchone()[0] == 1

    def test_reports_honest_field_coverage(self, capsys):
        save_to_sqlite(
            [
                lead(),
                lead(business_name='Bare Spa', phone='', email='', address=''),
            ]
        )
        assert 'Coverage of fetched leads: 1 with address, 1 with phone, 1 with email' in capsys.readouterr().out


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
        app.router.add_get('/not-found', not_found)

        server = TestServer(app)
        await server.start_server()
        yield server
        await server.close()

    @pytest.fixture
    async def session(self):
        async with aiohttp.ClientSession() as client_session:
            yield client_session

    @pytest.mark.parametrize('website_url', [None, '', 123, []])
    async def test_returns_empty_string_for_invalid_input(self, website_url):
        assert await medspa_scraper.fetch_email_from_website(None, website_url) == ''

    async def test_extracts_email_from_html(self, session, website):
        url = str(website.make_url('/with-email'))
        assert await medspa_scraper.fetch_email_from_website(session, url) == 'sales@example.com'

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
