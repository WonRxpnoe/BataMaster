import asyncio
import sqlite3
import time

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
    MAX_PAGES_PER_KEYWORD,
    RateLimiter,
    check_api_response,
    extract_zip_code,
    load_place_details,
    main,
    parse_args,
    recently_scraped_zip_codes,
    record_scrape_run,
    run_medspa_scraper,
    save_to_sqlite,
)


@pytest.fixture
def single_keyword(monkeypatch):
    """Reduce the keyword list so page and request counts stay readable."""
    monkeypatch.setattr(medspa_scraper, 'KEYWORDS', ['Med Spa'])


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
        self.search_responses = []
        self.search_status = 200
        self.search_failures = 0
        self.details_payload = {'status': 'OK', 'result': {}}
        self.details_status = 200
        self.details_by_place_id = {}
        self.details_delay = 0
        self.search_queries = []
        self.search_tokens = []
        self.details_requests = []
        self.in_flight = 0
        self.peak_in_flight = 0

    async def handle_search(self, request):
        self.search_queries.append(request.query.get('query'))
        self.search_tokens.append(request.query.get('pagetoken'))

        if self.search_failures > 0:
            self.search_failures -= 1
            return web.json_response({}, status=500)

        payload = self.search_responses.pop(0) if self.search_responses else self.search_payload
        return web.json_response(payload, status=self.search_status)

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
async def google(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

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

    def test_treats_zero_results_as_success(self, capture_logs):
        payload = {'status': 'ZERO_RESULTS', 'results': []}
        assert check_api_response(200, payload, 'ctx') == payload
        assert capture_logs.text == ''

    def test_reports_client_error_without_retrying(self, capture_logs):
        assert check_api_response(404, {}, 'ctx') is None
        assert 'ctx failed: HTTP 404' in capture_logs.text

    def test_treats_server_error_as_retryable(self):
        with pytest.raises(medspa_scraper.RetryableApiError, match='HTTP 503'):
            check_api_response(503, {}, 'ctx')

    def test_reports_permanent_status_without_raising(self, capture_logs):
        payload = {'status': 'NOT_FOUND', 'error_message': 'gone'}
        assert check_api_response(200, payload, 'ctx') is None
        assert 'NOT_FOUND' in capture_logs.text

    @pytest.mark.parametrize('status', ['UNKNOWN_ERROR', 'INVALID_REQUEST'])
    def test_treats_transient_statuses_as_retryable(self, status):
        with pytest.raises(medspa_scraper.RetryableApiError, match=status):
            check_api_response(200, {'status': status}, 'ctx')

    @pytest.mark.parametrize('status', ['REQUEST_DENIED', 'OVER_QUERY_LIMIT'])
    def test_raises_on_account_level_failure(self, status):
        with pytest.raises(FatalApiError, match=status):
            check_api_response(200, {'status': status, 'error_message': 'nope'}, 'ctx')


class TestRetries:
    async def test_recovers_from_a_temporary_server_error(
        self, google, session, single_keyword, no_email_lookup
    ):
        google.search_failures = 2
        google.search_payload = {'status': 'OK', 'results': [place('Glow Med Spa')]}

        leads = await run_medspa_scraper('key', ['90210'])

        assert [item['business_name'] for item in leads] == ['Glow Med Spa']
        # Two failures plus the successful third attempt
        assert len(google.search_queries) == 3

    async def test_gives_up_after_the_attempt_limit(
        self, google, session, single_keyword, no_email_lookup, capture_logs
    ):
        google.search_failures = 99

        leads = await run_medspa_scraper('key', ['90210'])

        assert leads == []
        assert len(google.search_queries) == medspa_scraper.MAX_ATTEMPTS
        assert 'failed after 3 attempts' in capture_logs.text

    async def test_retries_when_the_host_cannot_be_reached(
        self, google, monkeypatch, single_keyword, no_email_lookup, capture_logs
    ):
        # The everyday failure in the field is a refused connection or a timeout,
        # which arrives as a plain exception rather than an API status
        monkeypatch.setattr(medspa_scraper, 'SEARCH_URL', 'http://127.0.0.1:1/textsearch')

        leads = await run_medspa_scraper('key', ['90210'])

        assert leads == []
        assert 'failed after 3 attempts' in capture_logs.text
        assert 'ClientConnectorError' in capture_logs.text

    async def test_an_unreachable_host_leaves_the_zip_open_for_a_retry(
        self, google, monkeypatch, single_keyword, no_email_lookup
    ):
        init_db()
        monkeypatch.setattr(medspa_scraper, 'SEARCH_URL', 'http://127.0.0.1:1/textsearch')

        await run_medspa_scraper('key', ['90210'], state='CA')

        with sqlite3.connect(DB_PATH) as conn:
            assert conn.execute('SELECT COUNT(*) FROM scrape_runs').fetchone()[0] == 0

    async def test_does_not_retry_a_fatal_error(self, google, single_keyword, no_email_lookup):
        google.search_payload = {'status': 'REQUEST_DENIED', 'error_message': 'invalid key'}

        with pytest.raises(FatalApiError):
            await run_medspa_scraper('bad-key', ['90210'])

        assert len(google.search_queries) == 1


class TestRateLimiter:
    async def test_spaces_requests_apart(self):
        limiter = RateLimiter(0.01)
        started = time.monotonic()
        for _ in range(5):
            await limiter.wait()
        # Four gaps of 10ms between five calls
        assert time.monotonic() - started >= 0.04

    async def test_zero_interval_does_not_wait(self):
        limiter = RateLimiter(0)
        started = time.monotonic()
        for _ in range(100):
            await limiter.wait()
        assert time.monotonic() - started < 0.1


class TestPagination:
    async def test_follows_next_page_token(self, google, single_keyword, no_email_lookup):
        google.search_responses = [
            {'status': 'OK', 'results': [place('First')], 'next_page_token': 'token-2'},
            {'status': 'OK', 'results': [place('Second')], 'next_page_token': 'token-3'},
            {'status': 'OK', 'results': [place('Third')]},
        ]

        leads = await run_medspa_scraper('key', ['90210'])

        assert [item['business_name'] for item in leads] == ['First', 'Second', 'Third']
        assert google.search_tokens == [None, 'token-2', 'token-3']

    async def test_stops_when_no_token_is_returned(self, google, single_keyword, no_email_lookup):
        google.search_payload = {'status': 'OK', 'results': [place('Only')]}

        leads = await run_medspa_scraper('key', ['90210'])

        assert len(leads) == 1
        assert len(google.search_queries) == 1

    async def test_never_exceeds_the_page_limit(self, google, single_keyword, no_email_lookup):
        # A payload that always offers another token would otherwise loop forever
        google.search_payload = {'status': 'OK', 'results': [place('Endless')], 'next_page_token': 'more'}

        leads = await run_medspa_scraper('key', ['90210'])

        assert len(google.search_queries) == MAX_PAGES_PER_KEYWORD
        assert len(leads) == MAX_PAGES_PER_KEYWORD


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
        avoided, incomplete = await load_place_details(
            session, 'key', ['place-1', 'place-1'], cache, asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
        )

        assert google.details_requests == ['place-1']
        assert (avoided, incomplete) == (1, 0)
        assert set(cache) == {'place-1'}

    async def test_skips_place_ids_already_cached(self, google, session):
        cache = {'place-1': {'website': 'https://cached.example.com'}}
        avoided, incomplete = await load_place_details(
            session, 'key', ['place-1', 'place-2'], cache, asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
        )

        assert google.details_requests == ['place-2']
        assert (avoided, incomplete) == (1, 0)
        assert cache['place-1'] == {'website': 'https://cached.example.com'}

    async def test_propagates_a_fatal_error(self, google, session):
        google.details_payload = {'status': 'REQUEST_DENIED', 'error_message': 'invalid key'}
        with pytest.raises(FatalApiError):
            await load_place_details(session, 'key', ['place-1'], {}, asyncio.Semaphore(2))

    async def test_permanent_error_caches_an_empty_result(self, google, session):
        google.details_payload = {'status': 'NOT_FOUND'}
        cache = {}

        _, incomplete = await load_place_details(session, 'key', ['place-1'], cache, asyncio.Semaphore(2))
        await load_place_details(session, 'key', ['place-1'], cache, asyncio.Semaphore(2))

        assert cache == {'place-1': {}}
        # The API gave a definitive answer, so this is not an unfinished request
        assert incomplete == 0
        # The second pass is served from the cache rather than repeating a failing call
        assert google.details_requests == ['place-1']

    async def test_an_unfinished_lookup_is_not_cached(self, google, session):
        google.details_status = 503
        cache = {}

        _, incomplete = await load_place_details(session, 'key', ['place-1'], cache, asyncio.Semaphore(2))

        # Caching this would cost the business its phone number for the entire run,
        # so the place id is left absent and a later page can ask again
        assert cache == {}
        assert incomplete == 1
        assert google.details_requests == ['place-1'] * medspa_scraper.MAX_ATTEMPTS

    async def test_an_unexpected_error_is_not_swallowed_as_an_unfinished_lookup(
        self, google, session, monkeypatch
    ):
        async def exploding_details(*args, **kwargs):
            raise KeyError('a bug, not a network problem')

        monkeypatch.setattr(medspa_scraper, 'get_place_details', exploding_details)

        # Counting this as merely incomplete would hide a real defect behind a retry
        with pytest.raises(KeyError):
            await load_place_details(session, 'key', ['place-1'], {}, asyncio.Semaphore(2))

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

        leads = await run_medspa_scraper('key', ['90210'], state='CA')

        assert leads[0]['address'] == '9 Rodeo Dr, Beverly Hills, CA 90212, USA'
        assert leads[0]['phone'] == '(310) 555-0100'
        assert leads[0]['email'] == 'hello@glow.example.com'

    async def test_stores_the_business_zip_not_the_search_zip(self, google, no_email_lookup):
        google.search_payload = {
            'status': 'OK',
            'results': [place('Nearby Spa', formatted_address='9 Rodeo Dr, Beverly Hills, CA 90212, USA')],
        }

        leads = await run_medspa_scraper('key', ['90210'], state='CA')

        assert leads[0]['zip_code'] == '90212'

    async def test_keeps_the_place_id_on_the_lead(self, google, no_email_lookup):
        google.search_payload = {'status': 'OK', 'results': [place('Glow Med Spa')]}
        leads = await run_medspa_scraper('key', ['90210'])
        assert leads[0]['place_id'] == 'place-Glow Med Spa'

    async def test_falls_back_to_search_address_when_details_are_empty(self, google, no_email_lookup):
        google.search_payload = {'status': 'OK', 'results': [place('Glow Med Spa')]}
        leads = await run_medspa_scraper('key', ['90210'])
        assert leads[0]['address'] == '1 Main St, Beverly Hills, CA 90210, USA'
        assert leads[0]['phone'] == ''
        assert leads[0]['website'] == ''
        assert leads[0]['email'] == ''

    async def test_skips_permanently_closed_businesses(self, google, no_email_lookup, single_keyword):
        google.search_payload = {
            'status': 'OK',
            'results': [place('Open Spa'), place('Gone Spa', business_status='CLOSED_PERMANENTLY')],
        }

        leads = await run_medspa_scraper('key', ['90210'])

        assert {item['business_name'] for item in leads} == {'Open Spa'}
        # A closed business must not cost a details lookup either
        assert google.details_requests == ['place-Open Spa']

    async def test_searches_every_keyword(self, google, no_email_lookup):
        await run_medspa_scraper('key', ['90210'], state='CA')
        assert google.search_queries == [f'{kw} in 90210, CA' for kw in KEYWORDS]

    async def test_business_matching_every_keyword_is_billed_once(
        self, google, no_email_lookup, capture_logs
    ):
        google.search_payload = {'status': 'OK', 'results': [place('Glow Med Spa')]}
        google.details_payload = {'status': 'OK', 'result': {'formatted_phone_number': '(310) 555-0100'}}

        leads = await run_medspa_scraper('key', ['90210'])

        assert google.details_requests == ['place-Glow Med Spa']
        assert len(leads) == len(KEYWORDS)
        assert all(item['phone'] == '(310) 555-0100' for item in leads)
        assert f'1 unique businesses, {len(KEYWORDS) - 1} duplicate lookups avoided' in capture_logs.text

    async def test_rejected_key_aborts_and_carries_partial_results(self, google, no_email_lookup):
        google.search_payload = {
            'status': 'REQUEST_DENIED', 'error_message': 'The provided API key is invalid.',
        }

        with pytest.raises(FatalApiError) as excinfo:
            await run_medspa_scraper('bad-key', ['90210'])

        assert excinfo.value.partial_leads == []
        # The remaining keywords are skipped rather than repeating the same failure
        assert len(google.search_queries) == 1

    async def test_permanent_search_error_keeps_other_keywords_running(
        self, google, no_email_lookup, capture_logs
    ):
        google.search_payload = {'status': 'NOT_FOUND', 'error_message': 'nothing here'}

        leads = await run_medspa_scraper('key', ['90210'])

        assert leads == []
        assert capture_logs.text.count('NOT_FOUND') == len(KEYWORDS)
        assert len(google.search_queries) == len(KEYWORDS)


class TestCostGuards:
    async def test_searching_one_keyword_costs_one_search(self, google, no_email_lookup, capture_logs):
        google.search_payload = {'status': 'OK', 'results': [place('Glow Med Spa')]}

        await run_medspa_scraper('key', ['90210'], keywords=['Med Spa'])

        assert google.search_queries == ['Med Spa in 90210, CA']
        assert 'Billable requests: 1 Text Search, 1 Place Details' in capture_logs.text

    async def test_max_pages_stops_the_scraper_following_the_token(
        self, google, no_email_lookup, single_keyword
    ):
        # The payload always offers another page, so only the limit can end this
        google.search_payload = {'status': 'OK', 'results': [place('Endless')], 'next_page_token': 'more'}

        await run_medspa_scraper('key', ['90210'], max_pages=1)

        assert len(google.search_queries) == 1

    async def test_a_narrowed_run_is_not_recorded_as_a_finished_zip(self, google, no_email_lookup):
        init_db()
        google.search_payload = {'status': 'OK', 'results': [place('Glow Med Spa')]}

        await run_medspa_scraper('key', ['90210'], 'CA', keywords=['Med Spa'], max_pages=1)

        # A cheap probe covers a fraction of the area, so the full run must still happen
        assert recently_scraped_zip_codes(['90210'], 'CA', 7) == set()

    @pytest.mark.parametrize(
        'kwargs',
        [
            {'keywords': ['Med Spa']},
            {'max_pages': 1},
            {'keywords': ['Med Spa', 'Facial Spa'], 'max_pages': 2},
        ],
    )
    async def test_either_cap_on_its_own_leaves_the_zip_open(
        self, google, no_email_lookup, kwargs
    ):
        init_db()
        google.search_payload = {'status': 'OK', 'results': [place('Glow Med Spa')]}

        await run_medspa_scraper('key', ['90210'], 'CA', **kwargs)

        assert recently_scraped_zip_codes(['90210'], 'CA', 7) == set()

    async def test_asking_for_every_keyword_still_counts_as_a_full_run(
        self, google, no_email_lookup
    ):
        init_db()
        google.search_payload = {'status': 'OK', 'results': [place('Glow Med Spa')]}

        # Spelling out the defaults is not a narrowing
        await run_medspa_scraper(
            'key', ['90210'], 'CA', keywords=list(KEYWORDS), max_pages=MAX_PAGES_PER_KEYWORD
        )

        assert recently_scraped_zip_codes(['90210'], 'CA', 7) == {'90210'}

    async def test_the_billable_tally_counts_unique_businesses_not_results(
        self, google, no_email_lookup, capture_logs
    ):
        # One business surfacing under all five keywords is five searches but one lookup
        google.search_payload = {'status': 'OK', 'results': [place('Glow Med Spa')]}

        await run_medspa_scraper('key', ['90210'])

        expected = f'Billable requests: {len(KEYWORDS)} Text Search, 1 Place Details'
        assert expected in capture_logs.text


class TestMultipleZipCodes:
    async def test_scrapes_every_requested_zip_code(self, google, no_email_lookup, single_keyword):
        google.search_payload = {'status': 'OK', 'results': [place('Glow Med Spa')]}

        await run_medspa_scraper('key', ['90210', '90211'], state='CA')

        assert google.search_queries == ['Med Spa in 90210, CA', 'Med Spa in 90211, CA']

    async def test_shares_the_details_cache_between_zip_codes(self, google, no_email_lookup, single_keyword):
        # Neighbouring zip searches routinely surface the same business
        google.search_payload = {'status': 'OK', 'results': [place('Glow Med Spa')]}

        leads = await run_medspa_scraper('key', ['90210', '90211'])

        assert google.details_requests == ['place-Glow Med Spa']
        assert len(leads) == 2

    async def test_accepts_numeric_zip_codes(self, google, no_email_lookup, single_keyword):
        await run_medspa_scraper('key', [90210], state='CA')
        assert google.search_queries == ['Med Spa in 90210, CA']

    async def test_records_a_run_for_each_completed_zip_code(self, google, no_email_lookup, single_keyword):
        google.search_payload = {'status': 'OK', 'results': [place('Glow Med Spa')]}

        await run_medspa_scraper('key', ['90210', '90211'], state='CA')

        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute(
                'SELECT zip_code, state, leads_found FROM scrape_runs ORDER BY zip_code'
            ).fetchall()
        assert rows == [('90210', 'CA', 1), ('90211', 'CA', 1)]

    async def test_an_unanswered_zip_is_not_recorded_as_scraped(
        self, google, no_email_lookup, single_keyword, capture_logs
    ):
        init_db()
        # Every attempt fails, so this zip produced nothing because it broke
        google.search_failures = 99

        await run_medspa_scraper('key', ['90210'], state='CA')

        with sqlite3.connect(DB_PATH) as conn:
            assert conn.execute('SELECT COUNT(*) FROM scrape_runs').fetchone()[0] == 0
        assert 'was not recorded as scraped' in capture_logs.text

    async def test_a_zip_that_failed_is_retried_rather_than_skipped(
        self, google, no_email_lookup, single_keyword
    ):
        init_db()
        google.search_failures = 99
        await run_medspa_scraper('key', ['90210'], state='CA')

        # The whole point of not recording it: the next run must pick it up again
        assert recently_scraped_zip_codes(['90210'], 'CA', 7) == set()

    async def test_a_genuinely_empty_zip_is_still_recorded(self, google, no_email_lookup, single_keyword):
        init_db()
        google.search_payload = {'status': 'ZERO_RESULTS', 'results': []}

        await run_medspa_scraper('key', ['90210'], state='CA')

        # Nothing failed, so an area with no med spas counts as finished
        with sqlite3.connect(DB_PATH) as conn:
            assert conn.execute('SELECT zip_code, leads_found FROM scrape_runs').fetchall() == [('90210', 0)]

    async def test_an_unfinished_details_lookup_also_blocks_the_record(
        self, google, no_email_lookup, single_keyword
    ):
        init_db()
        # The search works, but the details call behind it never completes
        google.search_payload = {'status': 'OK', 'results': [place('Glow Med Spa')]}
        google.details_status = 503

        leads = await run_medspa_scraper('key', ['90210'], state='CA')

        # The lead is still returned, just without a phone number worth trusting
        assert len(leads) == 1
        with sqlite3.connect(DB_PATH) as conn:
            assert conn.execute('SELECT COUNT(*) FROM scrape_runs').fetchone()[0] == 0

    async def test_a_fatal_error_leaves_the_unfinished_zip_unrecorded(
        self, google, no_email_lookup, single_keyword
    ):
        init_db()
        google.search_payload = {'status': 'REQUEST_DENIED', 'error_message': 'invalid key'}

        with pytest.raises(FatalApiError):
            await run_medspa_scraper('bad-key', ['90210'])

        with sqlite3.connect(DB_PATH) as conn:
            assert conn.execute('SELECT COUNT(*) FROM scrape_runs').fetchone()[0] == 0


class TestIncrementalRuns:
    @pytest.fixture(autouse=True)
    def workdir(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        init_db()

    def test_reports_nothing_when_the_window_is_disabled(self):
        record_scrape_run('90210', 'CA', 3)
        assert recently_scraped_zip_codes(['90210'], 'CA', 0) == set()

    def test_finds_a_zip_scraped_inside_the_window(self):
        record_scrape_run('90210', 'CA', 3)
        assert recently_scraped_zip_codes(['90210', '90211'], 'CA', 7) == {'90210'}

    def test_ignores_a_zip_scraped_before_the_window(self):
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "INSERT INTO scrape_runs (zip_code, state, scraped_at, leads_found) "
                "VALUES ('90210', 'CA', '2020-01-01T00:00:00+00:00', 5)"
            )
        assert recently_scraped_zip_codes(['90210'], 'CA', 7) == set()

    def test_separates_identical_zips_in_different_states(self):
        record_scrape_run('90210', 'CA', 3)
        assert recently_scraped_zip_codes(['90210'], 'NV', 7) == set()


class TestSkipRecent:
    async def test_skips_a_recently_scraped_zip(self, google, no_email_lookup, single_keyword, capture_logs):
        init_db()
        record_scrape_run('90210', 'CA', 4)
        google.search_payload = {'status': 'OK', 'results': [place('Glow Med Spa')]}

        leads = await run_medspa_scraper('key', ['90210', '90211'], state='CA', skip_recent_days=7)

        assert google.search_queries == ['Med Spa in 90211, CA']
        assert len(leads) == 1
        assert 'Skipping 90210' in capture_logs.text

    async def test_scrapes_everything_when_the_window_is_zero(self, google, no_email_lookup, single_keyword):
        init_db()
        record_scrape_run('90210', 'CA', 4)

        await run_medspa_scraper('key', ['90210'], state='CA', skip_recent_days=0)

        assert google.search_queries == ['Med Spa in 90210, CA']


class TestSaveToSqlite:
    @pytest.fixture(autouse=True)
    def workdir(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)

    def test_creates_the_database_without_running_init_db_first(self, tmp_path):
        assert save_to_sqlite([lead()]) == 1

        assert (tmp_path / DB_PATH).is_file()
        with sqlite3.connect(DB_PATH) as conn:
            assert conn.execute('SELECT COUNT(*) FROM leads').fetchone()[0] == 1

    def test_does_nothing_for_empty_input(self, tmp_path, capture_logs):
        assert save_to_sqlite([]) == 0
        assert 'No valid leads were scraped' in capture_logs.text
        assert not (tmp_path / DB_PATH).exists()

    def test_persists_all_lead_columns(self):
        save_to_sqlite([lead()])
        with sqlite3.connect(DB_PATH) as conn:
            row = conn.execute(
                'SELECT business_name, industry, phone, website, email, '
                'state, zip_code, address, source, place_id FROM leads'
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

    def test_same_place_id_is_only_stored_once(self, capture_logs):
        # The same business under two keywords, and now under two different zips
        assert save_to_sqlite([lead(), lead(industry='MedSpa/Esthetics (Facial Spa)', zip_code='90212')]) == 1

        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute('SELECT industry FROM leads').fetchall()
        assert rows == [('MedSpa/Esthetics (Med Spa)',)]
        assert 'Saved 1 new leads' in capture_logs.text

    def test_distinct_businesses_are_all_stored(self):
        save_to_sqlite([lead(), lead(business_name='Other Spa', place_id='place-Other Spa')])
        with sqlite3.connect(DB_PATH) as conn:
            assert conn.execute('SELECT COUNT(*) FROM leads').fetchone()[0] == 2

    def test_reports_inserted_count_and_coverage(self, capture_logs):
        save_to_sqlite([
            lead(),
            lead(business_name='Bare Spa', place_id='p2', phone='', email='', address=''),
        ])

        text = capture_logs.text
        assert 'Saved 2 new leads' in text
        assert 'Coverage of fetched leads: 1 with address, 1 with phone, 1 with email' in text

    def test_repeats_within_one_batch_are_not_called_pre_existing(self, capture_logs):
        # Five results, one business. Nothing was on record before this call.
        save_to_sqlite([lead(industry=f'MedSpa/Esthetics ({keyword})') for keyword in KEYWORDS])

        expected = f'Fetched {len(KEYWORDS)} results: 1 new businesses, 0 gained a missing field'
        assert expected in capture_logs.text

    def test_two_branches_sharing_a_name_and_zip_are_both_stored(self):
        # The pair the old schema used to merge into one
        save_to_sqlite([
            lead(business_name='SkinLab', place_id='place-rodeo', address='100 Rodeo Dr'),
            lead(business_name='SkinLab', place_id='place-wilshire', address='900 Wilshire Blvd'),
        ])

        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute('SELECT address FROM leads ORDER BY address').fetchall()
        assert rows == [('100 Rodeo Dr',), ('900 Wilshire Blvd',)]


class TestRescrapingAnExistingBusiness:
    @pytest.fixture(autouse=True)
    def workdir(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)

    def stored(self):
        with sqlite3.connect(DB_PATH) as conn:
            return conn.execute('SELECT phone, email, website, address FROM leads').fetchone()

    def test_a_blank_field_is_filled_in(self, capture_logs):
        save_to_sqlite([lead(email='', phone='')])

        assert save_to_sqlite([lead(email='found@later.example', phone='(310) 555-0100')]) == 0

        phone, email, _, _ = self.stored()
        assert (phone, email) == ('(310) 555-0100', 'found@later.example')
        assert '1 gained a missing field' in capture_logs.text

    def test_a_field_that_already_has_a_value_is_never_overwritten(self):
        save_to_sqlite([lead(phone='(310) 555-0100')])

        save_to_sqlite([lead(phone='(999) 999-9999')])

        assert self.stored()[0] == '(310) 555-0100'

    def test_a_blank_in_the_new_scrape_does_not_erase_what_is_stored(self):
        save_to_sqlite([lead(email='hello@glow.example.com')])

        save_to_sqlite([lead(email='')])

        assert self.stored()[1] == 'hello@glow.example.com'

    def test_nothing_is_reported_when_there_is_nothing_to_add(self, capture_logs):
        save_to_sqlite([lead()])
        capture_logs.clear()

        assert save_to_sqlite([lead()]) == 0
        assert '0 gained a missing field' in capture_logs.text

    def test_a_result_without_a_place_id_is_still_deduplicated(self):
        """Google occasionally omits the id. Without a fallback those rows pile up.

        The old name and zip constraint used to catch this, so dropping it has to be
        paired with a match the insert path can fall back on.
        """
        for _ in range(3):
            save_to_sqlite([lead(place_id=None)])

        with sqlite3.connect(DB_PATH) as conn:
            assert conn.execute('SELECT COUNT(*) FROM leads').fetchone()[0] == 1

    def test_the_fallback_does_not_merge_two_branches(self):
        # Both have ids, so the weaker name and zip match must never come into play
        save_to_sqlite([
            lead(business_name='SkinLab', place_id='place-rodeo'),
            lead(business_name='SkinLab', place_id='place-wilshire'),
        ])

        with sqlite3.connect(DB_PATH) as conn:
            assert conn.execute('SELECT COUNT(*) FROM leads').fetchone()[0] == 2

    def test_a_result_without_an_id_does_not_absorb_one_that_has_an_id(self):
        save_to_sqlite([lead(place_id='place-real')])

        save_to_sqlite([lead(place_id=None)])

        # The identified row is left alone, so the anonymous one becomes its own lead
        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute('SELECT place_id FROM leads ORDER BY id').fetchall()
        assert rows == [('place-real',), (None,)]

    def test_the_row_is_matched_by_place_id_not_by_name(self):
        save_to_sqlite([lead(phone='')])

        # Google renamed the business, but it is the same place id
        save_to_sqlite([lead(business_name='Glow Med Spa and Wellness', phone='(310) 555-0100')])

        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute('SELECT business_name, phone FROM leads').fetchall()
        # One row, enriched in place. The stored name is left alone.
        assert rows == [('Glow Med Spa', '(310) 555-0100')]


class TestCommandLine:
    def test_requires_at_least_one_zip(self):
        with pytest.raises(SystemExit):
            parse_args([])

    def test_parses_several_zips_and_options(self):
        args = parse_args(['--zip', '90210', '90211', '--state', 'NV', '--skip-recent', '7', '--verbose'])
        assert args.zip_codes == ['90210', '90211']
        assert args.state == 'NV'
        assert args.skip_recent == 7
        assert args.verbose is True

    def test_defaults_to_california_and_no_skipping(self):
        args = parse_args(['--zip', '90210'])
        assert args.state == 'CA'
        assert args.skip_recent == 0
        assert args.verbose is False
        # No cost limits by default, so an unflagged run behaves as it always has
        assert args.keywords is None
        assert args.max_pages == MAX_PAGES_PER_KEYWORD

    def test_accepts_a_narrowed_search(self):
        args = parse_args(['--zip', '90210', '--keywords', 'Med Spa', 'Facial Spa', '--max-pages', '1'])
        assert args.keywords == ['Med Spa', 'Facial Spa']
        assert args.max_pages == 1

    @pytest.mark.parametrize('pages', ['0', '-1', str(MAX_PAGES_PER_KEYWORD + 1)])
    def test_rejects_a_page_count_the_api_cannot_serve(self, pages):
        with pytest.raises(SystemExit):
            parse_args(['--zip', '90210', '--max-pages', pages])

    def test_exits_non_zero_without_an_api_key(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv('GOOGLE_API_KEY', raising=False)
        assert main(['--zip', '90210']) == 1

    def test_exits_non_zero_for_the_placeholder_key(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv('GOOGLE_API_KEY', 'YOUR_API_KEY_HERE')
        assert main(['--zip', '90210']) == 1

    def test_returns_zero_on_a_successful_run(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv('GOOGLE_API_KEY', 'real-key')

        async def fake_run(api_key, zip_codes, state, skip_recent_days, keywords, max_pages):
            assert (api_key, zip_codes, state, skip_recent_days) == ('real-key', ['90210'], 'CA', 0)
            # No --keywords means every default term, and the full page allowance
            assert (keywords, max_pages) == (None, MAX_PAGES_PER_KEYWORD)
            return [lead()]

        monkeypatch.setattr(medspa_scraper, 'run_medspa_scraper', fake_run)
        assert main(['--zip', '90210']) == 0

        with sqlite3.connect(DB_PATH) as conn:
            assert conn.execute('SELECT COUNT(*) FROM leads').fetchone()[0] == 1

    def test_saves_partial_results_before_exiting_non_zero(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv('GOOGLE_API_KEY', 'real-key')

        async def fake_run(api_key, zip_codes, state, skip_recent_days, keywords, max_pages):
            raise FatalApiError('OVER_QUERY_LIMIT - quota exhausted', partial_leads=[lead()])

        monkeypatch.setattr(medspa_scraper, 'run_medspa_scraper', fake_run)
        assert main(['--zip', '90210']) == 1

        # The leads already paid for are still written to the database
        with sqlite3.connect(DB_PATH) as conn:
            assert conn.execute('SELECT COUNT(*) FROM leads').fetchone()[0] == 1


def html_response(body):
    async def handler(request):
        return web.Response(text=f'<html><body>{body}</body></html>', content_type='text/html')

    return handler


class TestExtractEmail:
    """What survives from a page has to be what a visitor would read off it."""

    SITE = 'https://www.glow-spa.example/contact'

    def extract(self, html_text):
        return medspa_scraper.extract_email(html_text, self.SITE)

    def test_reads_a_plain_address(self):
        assert self.extract('<a href="mailto:info@glow-spa.example">Email</a>') \
            == 'info@glow-spa.example'

    def test_skips_the_address_of_whoever_built_the_site(self):
        page = ('<!-- support@squarespace.com --><body>Contact: hello@glow-spa.example</body>')
        assert self.extract(page) == 'hello@glow-spa.example'

    def test_skips_a_blank_form_placeholder(self):
        page = '<input placeholder="you@email.com"><p>book@glow-spa.example</p>'
        assert self.extract(page) == 'book@glow-spa.example'

    @pytest.mark.parametrize('asset', ['logo@2x.PNG', 'icon@1x.svg', 'hero@3x.JPEG'])
    def test_skips_asset_filenames_whatever_their_case(self, asset):
        page = f'<img src="{asset}"><p>info@glow-spa.example</p>'
        assert self.extract(page) == 'info@glow-spa.example'

    def test_an_address_hidden_in_a_script_loses_to_one_on_the_page(self):
        # Both sit on the business domain, so only ignoring the script can decide this
        page = ('<script>var owner="webmaster@glow-spa.example";</script>'
                '<p>hello@glow-spa.example</p>')
        assert self.extract(page) == 'hello@glow-spa.example'

    def test_an_asset_filename_alone_yields_nothing(self):
        assert self.extract('<img src="logo@2x.PNG">') == ''

    @pytest.mark.parametrize(
        'address',
        ['support@squarespace.com', 'noreply@mailchimp.com', 'k@o1.ingest.sentry.io',
         'you@email.com', 'info@yourdomain.com'],
    )
    def test_a_service_address_in_plain_text_is_still_refused(self, address):
        assert self.extract(f'<p>Questions? {address}</p>') == ''

    def test_skips_anything_inside_a_script(self):
        page = ('<script>var t="a.b@c.dfunction";Sentry.init({dsn:"https://k@o1.sentry.io/4"})'
                '</script><p>info@glow-spa.example</p>')
        assert self.extract(page) == 'info@glow-spa.example'

    def test_skips_marketing_platform_addresses_in_schema_markup(self):
        page = ('<script type="application/ld+json">{"email":"noreply@mailchimp.com"}</script>'
                '<p>owner@glow-spa.example</p>')
        assert self.extract(page) == 'owner@glow-spa.example'

    def test_reads_an_address_written_as_html_entities(self):
        assert self.extract('<p>info&#64;glow-spa.example</p>') == 'info@glow-spa.example'

    def test_refuses_an_address_that_ran_into_the_next_word(self):
        # Returning info@glow-spa.exampleOpen would put mangled text in the list, and a
        # missing address costs far less than one that cannot be trusted
        assert self.extract('<p>Email info@glow-spa.exampleOpen Tuesdays</p>') == ''

    def test_a_non_breaking_space_still_ends_an_address(self):
        assert self.extract('<p>info@glow-spa.example&nbsp;Open</p>') == 'info@glow-spa.example'

    def test_prefers_an_address_on_the_sites_own_domain(self):
        page = '<p>glowmedspa@gmail.com</p><p>hi@glow-spa.example</p>'
        assert self.extract(page) == 'hi@glow-spa.example'

    def test_keeps_a_free_mailbox_when_it_is_all_there_is(self):
        # Plenty of small businesses never set up a mailbox on their own domain
        assert self.extract('<p>glowmedspa@gmail.com</p>') == 'glowmedspa@gmail.com'

    def test_a_linked_address_beats_a_loose_one_on_the_same_domain(self):
        page = 'hr@other.example <a href="mailto:book@other.example">Book</a>'
        assert self.extract(page) == 'book@other.example'

    def test_unwraps_an_encoded_mailto(self):
        assert self.extract('<a href="mailto:book%40glow-spa.example">Book</a>') \
            == 'book@glow-spa.example'

    def test_returns_empty_when_the_page_offers_nothing(self):
        assert self.extract('<p>Walk-ins welcome</p>') == ''


class TestFetchEmailFromWebsite:
    @pytest.fixture
    async def website(self):
        async def not_found(request):
            return web.Response(text='support@glow-spa.example', status=404, content_type='text/html')

        app = web.Application()
        app.router.add_get('/with-email', html_response('Contact: sales@glow-spa.example'))
        app.router.add_get('/without-email', html_response('No contact details here'))
        app.router.add_get('/image-only', html_response('<img src="logo@2x.png">'))
        app.router.add_get('/svg-only', html_response('<img src="icon@1x.svg">'))
        app.router.add_get('/jpeg-only', html_response('<img src="hero@2x.jpeg">'))
        app.router.add_get(
            '/image-and-email',
            html_response('<img src="banner@3x.jpg">support@glow-spa.example'),
        )
        app.router.add_get(
            '/many-emails',
            html_response('first@glow-spa.example then second@glow-spa.example then third@x.example'),
        )
        app.router.add_get(
            '/linked-and-loose',
            html_response('hr@glow-spa.example <a href="mailto:book@glow-spa.example">Book</a>'),
        )
        app.router.add_get('/not-found', not_found)

        server = TestServer(app)
        await server.start_server()
        yield server
        await server.close()

    @pytest.mark.parametrize('website_url', [None, '', 123, []])
    async def test_returns_empty_string_for_invalid_input(self, website_url):
        assert await medspa_scraper.fetch_email_from_website(None, website_url) == ''

    async def test_an_address_in_a_mailto_link_wins(self, session, website):
        url = str(website.make_url('/linked-and-loose'))
        assert await medspa_scraper.fetch_email_from_website(session, url) == 'book@glow-spa.example'

    async def test_extracts_email_from_html(self, session, website):
        url = str(website.make_url('/with-email'))
        assert await medspa_scraper.fetch_email_from_website(session, url) == 'sales@glow-spa.example'

    async def test_returns_the_first_address_in_document_order(self, session, website):
        # Deduplicating through a set used to make this vary between runs
        url = str(website.make_url('/many-emails'))
        results = {await medspa_scraper.fetch_email_from_website(session, url) for _ in range(5)}
        assert results == {'first@glow-spa.example'}

    async def test_returns_empty_string_when_page_has_no_email(self, session, website):
        url = str(website.make_url('/without-email'))
        assert await medspa_scraper.fetch_email_from_website(session, url) == ''

    @pytest.mark.parametrize('path', ['/image-only', '/svg-only', '/jpeg-only'])
    async def test_ignores_image_filenames_that_look_like_emails(self, session, website, path):
        url = str(website.make_url(path))
        assert await medspa_scraper.fetch_email_from_website(session, url) == ''

    async def test_picks_real_email_over_image_filename(self, session, website):
        url = str(website.make_url('/image-and-email'))
        assert await medspa_scraper.fetch_email_from_website(session, url) == 'support@glow-spa.example'

    async def test_returns_empty_string_on_non_200_status(self, session, website):
        url = str(website.make_url('/not-found'))
        assert await medspa_scraper.fetch_email_from_website(session, url) == ''

    async def test_returns_empty_string_when_host_is_unreachable(self, session):
        assert await medspa_scraper.fetch_email_from_website(session, 'http://127.0.0.1:1/') == ''

    async def test_prepends_https_scheme_for_bare_host(self, session):
        # No scheme means the helper retries against https://, which cannot resolve here
        assert await medspa_scraper.fetch_email_from_website(session, 'host.invalid') == ''
