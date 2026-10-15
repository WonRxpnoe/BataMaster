import os
import requests
import sqlite3
import asyncio
import aiohttp
import re

# 1. Full keyword list for light aesthetics and beauty businesses
KEYWORDS = [
    "Med Spa", 
    "Skin Care Clinic", 
    "Esthetician", 
    "Facial Spa", 
    "Aesthetic Center"
]

EMAIL_REGEX = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'

# Response statuses that mean the request succeeded, even when nothing matched
OK_STATUSES = {'OK', 'ZERO_RESULTS'}

# Account-level failures that will repeat identically for every remaining request
FATAL_STATUSES = {'REQUEST_DENIED', 'OVER_QUERY_LIMIT'}


class FatalApiError(Exception):
    """Raised when the API key, billing, or quota makes every further request pointless."""


def check_api_response(res, context):
    """Return the payload when the response is usable, otherwise report why it is not."""
    if res.status_code != 200:
        print(f"{context} failed: HTTP {res.status_code}")
        return None

    data = res.json()
    status = data.get('status', 'UNKNOWN_ERROR')
    if status in OK_STATUSES:
        return data

    detail = data.get('error_message', 'no error_message returned')
    print(f"{context} failed: {status} - {detail}")
    if status in FATAL_STATUSES:
        raise FatalApiError(f"{status} - {detail}")
    return None


# Scrape an email address from a business website asynchronously
async def fetch_email_from_website(session, website_url):
    if not website_url or not isinstance(website_url, str):
        return ""
    if not website_url.startswith(('http://', 'https://')):
        website_url = 'https://' + website_url
        
    try:
        async with session.get(website_url, timeout=10, ssl=False) as response:
            if response.status == 200:
                html_text = await response.text()
                emails = set(re.findall(EMAIL_REGEX, html_text))
                # Filter out matches that are actually image filenames
                valid_emails = [e for e in emails if not e.endswith(('.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg'))]
                if valid_emails:
                    return valid_emails[0] # Prefer the first address found
    except Exception:
        pass
    return ""

# Fetch the phone number and website for a single business
def get_place_details(api_key, place_id):
    details_url = "https://maps.googleapis.com/maps/api/place/details/json"
    params = {
        'place_id': place_id,
        'fields': 'name,formatted_address,formatted_phone_number,website',
        'key': api_key
    }
    try:
        res = requests.get(details_url, params=params, timeout=10)
        data = check_api_response(res, f"Place Details for {place_id}")
        if data:
            return data.get('result', {})
    except FatalApiError:
        raise
    except Exception as e:
        print(f"Place Details request failed for {place_id}: {e}")
    return {}

# Look up details once per business, since one place often matches several keywords
def get_cached_place_details(api_key, place_id, cache):
    if place_id not in cache:
        cache[place_id] = get_place_details(api_key, place_id)
    return cache[place_id]

# Main scraping routine
async def run_medspa_scraper(api_key, zip_code="90210", state="CA"):
    search_url = "https://maps.googleapis.com/maps/api/place/textsearch/json"
    fetched_leads = []
    details_cache = {}
    details_lookups = 0

    print(f"--- Searching for businesses in zip code {zip_code} ---")

    try:
        for kw in KEYWORDS:
            query = f"{kw} in {zip_code}, {state}"
            params = {'query': query, 'key': api_key}

            try:
                res = requests.get(search_url, params=params, timeout=10)
                data = check_api_response(res, f"Text Search for {query}")
                if not data:
                    continue

                for place in data.get('results', []):
                    if place.get('business_status') == 'CLOSED_PERMANENTLY':
                        continue

                    place_id = place.get('place_id')
                    # 1. Fetch details, which carry the real phone number and website
                    details_lookups += 1
                    details = get_cached_place_details(api_key, place_id, details_cache)

                    address = details.get('formatted_address', place.get('formatted_address', ''))
                    phone = details.get('formatted_phone_number', '')
                    website = details.get('website', '')

                    fetched_leads.append({
                        "business_name": place.get('name'),
                        "industry": f"MedSpa/Esthetics ({kw})",
                        "address": address, # 1. address
                        "phone": phone,     # 2. phone
                        "website": website,
                        "email": "",        # 3. filled in below
                        "state": state,
                        "zip_code": str(zip_code),
                        "source": "Google_Places"
                    })
            except FatalApiError:
                raise
            except Exception as e:
                print(f"Query failed for {query}: {e}")
    except FatalApiError as e:
        print(f"Aborting search: {e}")
        print("Check your API key, billing status, and quota.")
        return fetched_leads

    avoided_lookups = details_lookups - len(details_cache)
    print(f"--- Place Details: {len(details_cache)} unique businesses, {avoided_lookups} duplicate lookups avoided ---")

    # 2. Visit each website to extract the email address
    print("--- Visiting business websites to extract emails ---")
    async with aiohttp.ClientSession() as session:
        tasks = []
        for lead in fetched_leads:
            tasks.append(fetch_email_from_website(session, lead['website']))
        
        emails = await asyncio.gather(*tasks)
        
        for i, email in enumerate(emails):
            fetched_leads[i]['email'] = email

    return fetched_leads

# Persist the results to the local database
def save_to_sqlite(leads):
    if not leads:
        print("No valid leads were scraped")
        return
        
    conn = sqlite3.connect("data/leads.db")
    cursor = conn.cursor()
    
    saved_count = 0
    for item in leads:
        cursor.execute('''
            INSERT OR IGNORE INTO leads 
            (business_name, industry, phone, website, email, state, zip_code, address, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            item['business_name'], item['industry'], item['phone'], 
            item['website'], item['email'], item['state'], 
            item['zip_code'], item['address'], item['source']
        ))
        saved_count += cursor.rowcount
        
    conn.commit()
    conn.close()

    with_address = sum(1 for item in leads if item['address'])
    with_phone = sum(1 for item in leads if item['phone'])
    with_email = sum(1 for item in leads if item['email'])

    print("\n==========================================")
    print(f"Done. Saved {saved_count} new leads to data/leads.db")
    print(f"Fetched {len(leads)}, skipped {len(leads) - saved_count} already in the database")
    print(f"Coverage of fetched leads: {with_address} with address, {with_phone} with phone, {with_email} with email")
    print("==========================================\n")

if __name__ == "__main__":
    GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "YOUR_API_KEY_HERE")
    
    if GOOGLE_API_KEY == "YOUR_API_KEY_HERE":
        print("Set the GOOGLE_API_KEY environment variable to your Google Places API key before running.")
    else:
        loop = asyncio.get_event_loop()
        leads = loop.run_until_complete(run_medspa_scraper(GOOGLE_API_KEY, zip_code="90210", state="CA"))
        save_to_sqlite(leads)
