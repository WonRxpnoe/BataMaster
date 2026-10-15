import argparse
import asyncio
import logging
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import aiohttp

from init_db import DB_PATH, init_db

logger = logging.getLogger(__name__)

# Overridable so the pipeline can be exercised against a local mock server
PLACES_BASE_URL = os.getenv('PLACES_BASE_URL', 'https://maps.googleapis.com/maps/api/place')

SEARCH_URL = f"{PLACES_BASE_URL}/textsearch/json"
DETAILS_URL = f"{PLACES_BASE_URL}/details/json"

# Full keyword list for light aesthetics and beauty businesses
KEYWORDS = [
    "Med Spa",
    "Skin Care Clinic",
    "Esthetician",
    "Facial Spa",
    "Aesthetic Center",
]

EMAIL_REGEX = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'

# US addresses put the ZIP straight after the two-letter state code, as in "Beverly Hills, CA 90210"
ZIP_FROM_ADDRESS = re.compile(r'\b[A-Z]{2}\s+(\d{5})(?:-\d{4})?\b')

# Response statuses that mean the request succeeded, even when nothing matched
OK_STATUSES = {'OK', 'ZERO_RESULTS'}

# Account-level failures that will repeat identically for every remaining request
FATAL_STATUSES = {'REQUEST_DENIED', 'OVER_QUERY_LIMIT'}

# UNKNOWN_ERROR is a server hiccup, and a page token that is not ready yet arrives as INVALID_REQUEST
RETRYABLE_STATUSES = {'UNKNOWN_ERROR', 'INVALID_REQUEST'}

# Upper bound on parallel requests, so a dense zip code cannot open hundreds of sockets
MAX_CONCURRENT_REQUESTS = 10

REQUEST_TIMEOUT = 10

# Text Search caps out at three pages of twenty results
MAX_PAGES_PER_KEYWORD = 3

# A next_page_token is rejected until Google finishes preparing the following page
PAGE_TOKEN_DELAY = 2.0

MAX_ATTEMPTS = 3
RETRY_BACKOFF = 1.0
MIN_SECONDS_BETWEEN_REQUESTS = 0.05


class FatalApiError(Exception):
    """Raised when the API key, billing, or quota makes every further request pointless."""

    def __init__(self, message, partial_leads=None):
        super().__init__(message)
        self.partial_leads = partial_leads or []


class RetryableApiError(Exception):
    """Raised for failures that often succeed when the same request is repeated."""


class RateLimiter:
    """Spaces outgoing requests so a burst cannot trip the per-second API limits."""

    def __init__(self, min_interval):
        self._min_interval = min_interval
        self._lock = asyncio.Lock()
        self._next_allowed = 0.0

    async def wait(self):
        if self._min_interval <= 0:
            return
        async with self._lock:
            now = asyncio.get_running_loop().time()
            delay = self._next_allowed - now
            if delay > 0:
                await asyncio.sleep(delay)
                now += delay
            self._next_allowed = now + self._min_interval


def check_api_response(status_code, payload, context):
    """Return the payload when the response is usable, otherwise report or raise."""
    if status_code >= 500:
        raise RetryableApiError(f"HTTP {status_code}")
    if status_code != 200:
        logger.warning("%s failed: HTTP %s", context, status_code)
        return None

    status = payload.get('status', 'UNKNOWN_ERROR')
    if status in OK_STATUSES:
        return payload

    detail = payload.get('error_message', 'no error_message returned')
    if status in FATAL_STATUSES:
        raise FatalApiError(f"{status} - {detail}")
    if status in RETRYABLE_STATUSES:
        raise RetryableApiError(f"{status} - {detail}")

    logger.warning("%s failed: %s - %s", context, status, detail)
    return None


async def fetch_api_json(session, url, params, context, limiter=None):
    """Make one Places API call, retrying the failures that tend to be temporary."""
    reason = 'unknown'
    for attempt in range(1, MAX_ATTEMPTS + 1):
        if limiter:
            await limiter.wait()
        try:
            async with session.get(url, params=params, timeout=REQUEST_TIMEOUT) as response:
                # An error response may not carry a JSON body worth parsing
                payload = await response.json(content_type=None) if response.status == 200 else {}
                return check_api_response(response.status, payload, context)
        except FatalApiError:
            raise
        except RetryableApiError as e:
            reason = str(e)
        except Exception as e:
            reason = f"{type(e).__name__}: {e}"

        if attempt == MAX_ATTEMPTS:
            logger.warning("%s failed after %s attempts: %s", context, MAX_ATTEMPTS, reason)
            return None

        backoff = RETRY_BACKOFF * (2 ** (attempt - 1))
        logger.debug("%s failed (%s), retrying in %.1fs", context, reason, backoff)
        await asyncio.sleep(backoff)

    return None


def extract_zip_code(address, fallback):
    """Prefer the ZIP from the business address; a text search also returns nearby areas."""
    match = ZIP_FROM_ADDRESS.search(address or '')
    return match.group(1) if match else str(fallback)


async def fetch_email_from_website(session, website_url):
    """Scrape an email address from a business website."""
    if not website_url or not isinstance(website_url, str):
        return ""
    if not website_url.startswith(('http://', 'https://')):
        website_url = 'https://' + website_url

    try:
        async with session.get(website_url, timeout=REQUEST_TIMEOUT, ssl=False) as response:
            if response.status == 200:
                html_text = await response.text()
                # dict.fromkeys keeps the order the addresses appear in, so reruns agree
                emails = list(dict.fromkeys(re.findall(EMAIL_REGEX, html_text)))
                # Filter out matches that are actually image filenames
                valid_emails = [
                    e for e in emails
                    if not e.endswith(('.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg'))
                ]
                if valid_emails:
                    return valid_emails[0]
    except Exception:
        pass
    return ""


async def fetch_email_with_limit(semaphore, session, website_url):
    async with semaphore:
        return await fetch_email_from_website(session, website_url)


async def get_place_details(session, api_key, place_id, limiter=None):
    """Fetch the phone number and website for a single business."""
    params = {
        'place_id': place_id,
        'fields': 'name,formatted_address,formatted_phone_number,website',
        'key': api_key,
    }
    data = await fetch_api_json(session, DETAILS_URL, params, f"Place Details for {place_id}", limiter)
    return data.get('result', {}) if data else {}


async def load_place_details(session, api_key, place_ids, cache, semaphore, limiter=None):
    """Look up the places missing from the cache and report how many lookups it saved."""
    missing = [place_id for place_id in dict.fromkeys(place_ids) if place_id not in cache]

    async def fetch(place_id):
        async with semaphore:
            return place_id, await get_place_details(session, api_key, place_id, limiter)

    results = await asyncio.gather(*(fetch(place_id) for place_id in missing), return_exceptions=True)

    fatal = None
    for result in results:
        if isinstance(result, FatalApiError):
            fatal = fatal or result
        elif isinstance(result, BaseException):
            raise result
        else:
            place_id, details = result
            cache[place_id] = details

    if fatal:
        raise fatal

    return len(place_ids) - len(missing)


async def search_keyword_pages(session, api_key, query, limiter=None):
    """Yield each page of text search results, following next_page_token."""
    params = {'query': query, 'key': api_key}

    for page in range(1, MAX_PAGES_PER_KEYWORD + 1):
        context = f"Text Search for {query} (page {page})"
        data = await fetch_api_json(session, SEARCH_URL, params, context, limiter)
        if not data:
            return

        yield data.get('results', [])

        token = data.get('next_page_token')
        if not token:
            return

        await asyncio.sleep(PAGE_TOKEN_DELAY)
        params = {'pagetoken': token, 'key': api_key}


async def scrape_zip_code(session, api_key, zip_code, state, details_cache, semaphore, limiter=None):
    """Collect every lead for one zip code, reusing the shared details cache."""
    leads = []
    avoided_lookups = 0

    for keyword in KEYWORDS:
        query = f"{keyword} in {zip_code}, {state}"

        async for results in search_keyword_pages(session, api_key, query, limiter):
            # Skip permanently closed businesses before paying for their details
            places = [
                place for place in results
                if place.get('business_status') != 'CLOSED_PERMANENTLY'
            ]
            if not places:
                continue

            place_ids = [place.get('place_id') for place in places]
            avoided_lookups += await load_place_details(
                session, api_key, place_ids, details_cache, semaphore, limiter
            )

            for place in places:
                details = details_cache.get(place.get('place_id'), {})
                address = details.get('formatted_address', place.get('formatted_address', ''))

                leads.append({
                    "business_name": place.get('name'),
                    "industry": f"MedSpa/Esthetics ({keyword})",
                    "address": address,
                    "phone": details.get('formatted_phone_number', ''),
                    "website": details.get('website', ''),
                    "email": "",
                    "state": state,
                    "zip_code": extract_zip_code(address, zip_code),
                    "place_id": place.get('place_id'),
                    "source": "Google_Places",
                })

    return leads, avoided_lookups


def recently_scraped_zip_codes(zip_codes, state, days):
    """Return the zip codes already scraped inside the cutoff window."""
    if days <= 0 or not zip_codes:
        return set()

    init_db()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    placeholders = ','.join('?' for _ in zip_codes)
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            'SELECT DISTINCT zip_code FROM scrape_runs '
            f'WHERE state = ? AND scraped_at >= ? AND zip_code IN ({placeholders})',
            [state, cutoff, *zip_codes],
        )
        return {row[0] for row in rows}


def record_scrape_run(zip_code, state, leads_found):
    """Remember that a zip code finished, so a later run can skip it."""
    init_db()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            'INSERT INTO scrape_runs (zip_code, state, scraped_at, leads_found) VALUES (?, ?, ?, ?)',
            (zip_code, state, datetime.now(timezone.utc).isoformat(), leads_found),
        )


async def run_medspa_scraper(api_key, zip_codes, state="CA", skip_recent_days=0):
    """Scrape every requested zip code, then fill in emails from the business websites."""
    zip_codes = [str(zip_code) for zip_code in zip_codes]
    skipped = recently_scraped_zip_codes(zip_codes, state, skip_recent_days)
    for zip_code in zip_codes:
        if zip_code in skipped:
            logger.info("Skipping %s, already scraped within the last %s days", zip_code, skip_recent_days)

    pending = [zip_code for zip_code in zip_codes if zip_code not in skipped]

    all_leads = []
    details_cache = {}
    avoided_lookups = 0
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
    limiter = RateLimiter(MIN_SECONDS_BETWEEN_REQUESTS)

    async with aiohttp.ClientSession() as session:
        try:
            for zip_code in pending:
                logger.info("Searching for businesses in zip code %s", zip_code)
                leads, avoided = await scrape_zip_code(
                    session, api_key, zip_code, state, details_cache, semaphore, limiter
                )
                all_leads.extend(leads)
                avoided_lookups += avoided
                record_scrape_run(zip_code, state, len(leads))
                logger.info("Zip code %s produced %s leads", zip_code, len(leads))
        except FatalApiError as e:
            e.partial_leads = all_leads
            raise

        logger.info(
            "Place Details: %s unique businesses, %s duplicate lookups avoided",
            len(details_cache),
            avoided_lookups,
        )

        logger.info("Visiting %s business websites to extract emails", len(all_leads))
        emails = await asyncio.gather(*(
            fetch_email_with_limit(semaphore, session, lead['website']) for lead in all_leads
        ))

        for i, email in enumerate(emails):
            all_leads[i]['email'] = email

    return all_leads


def save_to_sqlite(leads):
    """Persist the results to the local database."""
    if not leads:
        logger.info("No valid leads were scraped")
        return 0

    init_db()
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    saved_count = 0
    for item in leads:
        cursor.execute('''
            INSERT OR IGNORE INTO leads
            (business_name, industry, phone, website, email, state, zip_code, address, source, place_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            item['business_name'], item['industry'], item['phone'],
            item['website'], item['email'], item['state'],
            item['zip_code'], item['address'], item['source'], item['place_id'],
        ))
        saved_count += cursor.rowcount

    conn.commit()
    conn.close()

    with_address = sum(1 for item in leads if item['address'])
    with_phone = sum(1 for item in leads if item['phone'])
    with_email = sum(1 for item in leads if item['email'])

    logger.info("Saved %s new leads to %s", saved_count, DB_PATH)
    logger.info("Fetched %s, skipped %s already in the database", len(leads), len(leads) - saved_count)
    logger.info(
        "Coverage of fetched leads: %s with address, %s with phone, %s with email",
        with_address,
        with_phone,
        with_email,
    )
    return saved_count


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Scrape med spa leads from Google Places.")
    parser.add_argument(
        '--zip', dest='zip_codes', nargs='+', required=True, metavar='ZIP',
        help='One or more zip codes to search',
    )
    parser.add_argument('--state', default='CA', help='Two letter state code (default: CA)')
    parser.add_argument(
        '--skip-recent', type=int, default=0, metavar='DAYS',
        help='Skip zip codes already scraped within this many days',
    )
    parser.add_argument('--verbose', action='store_true', help='Show debug level logging')
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    # Keep the root logger quiet so third party debug output stays out of the way
    logging.basicConfig(level=logging.WARNING, format='%(levelname)s %(message)s')
    logger.setLevel(logging.DEBUG if args.verbose else logging.INFO)

    api_key = os.getenv("GOOGLE_API_KEY", "")
    if not api_key or api_key == "YOUR_API_KEY_HERE":
        logger.error("Set the GOOGLE_API_KEY environment variable to your Google Places API key before running.")
        return 1

    try:
        leads = asyncio.run(
            run_medspa_scraper(api_key, args.zip_codes, args.state, args.skip_recent)
        )
    except FatalApiError as e:
        logger.error("Aborting search: %s", e)
        logger.error("Check your API key, billing status, and quota.")
        # Keep whatever was already paid for before the run stopped
        save_to_sqlite(e.partial_leads)
        return 1

    save_to_sqlite(leads)
    return 0


if __name__ == "__main__":
    sys.exit(main())
