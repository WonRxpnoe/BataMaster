import asyncio
import os
import re
import sqlite3
import sys

import aiohttp

from init_db import DB_PATH, init_db

SEARCH_URL = "https://maps.googleapis.com/maps/api/place/textsearch/json"
DETAILS_URL = "https://maps.googleapis.com/maps/api/place/details/json"

# 1. Full keyword list for light aesthetics and beauty businesses
KEYWORDS = [
    "Med Spa", 
    "Skin Care Clinic", 
    "Esthetician", 
    "Facial Spa", 
    "Aesthetic Center"
]

EMAIL_REGEX = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'

# US addresses put the ZIP straight after the two-letter state code, as in "Beverly Hills, CA 90210"
ZIP_FROM_ADDRESS = re.compile(r'\b[A-Z]{2}\s+(\d{5})(?:-\d{4})?\b')

# Response statuses that mean the request succeeded, even when nothing matched
OK_STATUSES = {'OK', 'ZERO_RESULTS'}

# Account-level failures that will repeat identically for every remaining request
FATAL_STATUSES = {'REQUEST_DENIED', 'OVER_QUERY_LIMIT'}

# Upper bound on parallel requests, so a dense zip code cannot open hundreds of sockets
MAX_CONCURRENT_REQUESTS = 10

REQUEST_TIMEOUT = 10


class FatalApiError(Exception):
    """Raised when the API key, billing, or quota makes every further request pointless."""

    def __init__(self, message, partial_leads=None):
        super().__init__(message)
        self.partial_leads = partial_leads or []


def check_api_response(status_code, payload, context):
    """Return the payload when the response is usable, otherwise report why it is not."""
    if status_code != 200:
        print(f"{context} failed: HTTP {status_code}")
        return None

    status = payload.get('status', 'UNKNOWN_ERROR')
    if status in OK_STATUSES:
        return payload

    detail = payload.get('error_message', 'no error_message returned')
    print(f"{context} failed: {status} - {detail}")
    if status in FATAL_STATUSES:
        raise FatalApiError(f"{status} - {detail}")
    return None


async def fetch_api_json(session, url, params, context):
    """Make one Places API call and hand the body to the shared status check."""
    try:
        async with session.get(url, params=params, timeout=REQUEST_TIMEOUT) as response:
            # An error response may not carry a JSON body worth parsing
            payload = await response.json(content_type=None) if response.status == 200 else {}
            return check_api_response(response.status, payload, context)
    except FatalApiError:
        raise
    except Exception as e:
        print(f"{context} failed: {e}")
        return None


def extract_zip_code(address, fallback):
    """Prefer the ZIP from the business address; a text search also returns nearby areas."""
    match = ZIP_FROM_ADDRESS.search(address or '')
    return match.group(1) if match else str(fallback)


# Scrape an email address from a business website asynchronously
async def fetch_email_from_website(session, website_url):
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
                valid_emails = [e for e in emails if not e.endswith(('.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg'))]
                if valid_emails:
                    return valid_emails[0] # Prefer the first address found
    except Exception:
        pass
    return ""


async def fetch_email_with_limit(semaphore, session, website_url):
    async with semaphore:
        return await fetch_email_from_website(session, website_url)


# Fetch the phone number and website for a single business
async def get_place_details(session, api_key, place_id):
    params = {
        'place_id': place_id,
        'fields': 'name,formatted_address,formatted_phone_number,website',
        'key': api_key
    }
    data = await fetch_api_json(session, DETAILS_URL, params, f"Place Details for {place_id}")
    return data.get('result', {}) if data else {}


async def load_place_details(session, api_key, place_ids, cache, semaphore):
    """Look up the places missing from the cache and report how many lookups it saved."""
    missing = [place_id for place_id in dict.fromkeys(place_ids) if place_id not in cache]

    async def fetch(place_id):
        async with semaphore:
            return place_id, await get_place_details(session, api_key, place_id)

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


# Main scraping routine
async def run_medspa_scraper(api_key, zip_code="90210", state="CA"):
    fetched_leads = []
    details_cache = {}
    avoided_lookups = 0
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

    print(f"--- Searching for businesses in zip code {zip_code} ---")

    async with aiohttp.ClientSession() as session:
        try:
            for kw in KEYWORDS:
                query = f"{kw} in {zip_code}, {state}"
                data = await fetch_api_json(
                    session, SEARCH_URL, {'query': query, 'key': api_key}, f"Text Search for {query}"
                )
                if not data:
                    continue

                # Skip permanently closed businesses before paying for their details
                places = [
                    place for place in data.get('results', [])
                    if place.get('business_status') != 'CLOSED_PERMANENTLY'
                ]
                place_ids = [place.get('place_id') for place in places]
                avoided_lookups += await load_place_details(
                    session, api_key, place_ids, details_cache, semaphore
                )

                for place in places:
                    details = details_cache.get(place.get('place_id'), {})
                    address = details.get('formatted_address', place.get('formatted_address', ''))

                    fetched_leads.append({
                        "business_name": place.get('name'),
                        "industry": f"MedSpa/Esthetics ({kw})",
                        "address": address,
                        "phone": details.get('formatted_phone_number', ''),
                        "website": details.get('website', ''),
                        "email": "",        # filled in below
                        "state": state,
                        "zip_code": extract_zip_code(address, zip_code),
                        "place_id": place.get('place_id'),
                        "source": "Google_Places"
                    })
        except FatalApiError as e:
            e.partial_leads = fetched_leads
            raise

        print(f"--- Place Details: {len(details_cache)} unique businesses, {avoided_lookups} duplicate lookups avoided ---")

        # 2. Visit each website to extract the email address
        print("--- Visiting business websites to extract emails ---")
        emails = await asyncio.gather(*(
            fetch_email_with_limit(semaphore, session, lead['website']) for lead in fetched_leads
        ))

        for i, email in enumerate(emails):
            fetched_leads[i]['email'] = email

    return fetched_leads

# Persist the results to the local database
def save_to_sqlite(leads):
    if not leads:
        print("No valid leads were scraped")
        return

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
            item['zip_code'], item['address'], item['source'], item['place_id']
        ))
        saved_count += cursor.rowcount
        
    conn.commit()
    conn.close()

    with_address = sum(1 for item in leads if item['address'])
    with_phone = sum(1 for item in leads if item['phone'])
    with_email = sum(1 for item in leads if item['email'])

    print("\n==========================================")
    print(f"Done. Saved {saved_count} new leads to {DB_PATH}")
    print(f"Fetched {len(leads)}, skipped {len(leads) - saved_count} already in the database")
    print(f"Coverage of fetched leads: {with_address} with address, {with_phone} with phone, {with_email} with email")
    print("==========================================\n")

if __name__ == "__main__":
    GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "YOUR_API_KEY_HERE")
    
    if GOOGLE_API_KEY == "YOUR_API_KEY_HERE":
        print("Set the GOOGLE_API_KEY environment variable to your Google Places API key before running.")
        sys.exit(1)

    try:
        leads = asyncio.run(run_medspa_scraper(GOOGLE_API_KEY, zip_code="90210", state="CA"))
    except FatalApiError as e:
        print(f"Aborting search: {e}")
        print("Check your API key, billing status, and quota.")
        # Keep whatever was already paid for before the run stopped
        save_to_sqlite(e.partial_leads)
        sys.exit(1)

    save_to_sqlite(leads)
