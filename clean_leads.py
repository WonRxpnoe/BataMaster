import dns.resolver
import re
import sqlite3
import requests

from init_db import DB_PATH, init_db

EMAIL_REGEX = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'

# Many small business sites sit behind bot protection that rejects unbranded clients
BROWSER_USER_AGENT = (
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
)

def check_email_mx(email):
    """1. Check the MX records of the email domain so the address will not hard bounce."""
    if not email or not re.match(EMAIL_REGEX, email):
        return False
    domain = email.split('@')[1]
    try:
        records = dns.resolver.resolve(domain, 'MX')
        return len(records) > 0
    except Exception:
        return False

def check_website_live(website):
    """2. Check that the website still responds, dropping closed or expired businesses."""
    if not website:
        return False
    if not website.startswith(('http://', 'https://')):
        website = 'https://' + website
    try:
        # stream=True reads the status line without downloading the whole page
        with requests.get(
            website,
            timeout=5,
            allow_redirects=True,
            headers={'User-Agent': BROWSER_USER_AGENT},
            stream=True,
        ) as response:
            # Anything below 400 means the server answered for this address
            return response.status_code < 400
    except Exception:
        return False

def clean_and_validate_leads():
    """3. Read leads from the database and record how usable each one is."""
    init_db()
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Load every raw lead that was scraped
    cursor.execute("SELECT id, business_name, phone, website, email FROM leads")
    rows = cursor.fetchall()
    
    valid_count = 0
    invalid_count = 0
    live_site_count = 0
    
    print(f"--- Cleaning {len(rows)} raw leads from the database ---")
    
    for row in rows:
        lead_id, name, phone, website, email = row
        
        is_phone_valid = bool(phone and len(re.sub(r'\D', '', phone)) >= 10)
        is_email_valid = check_email_mx(email)
        
        # Liveness is recorded alongside the lead rather than used to reject it
        website_live = None
        if website:
            website_live = 1 if check_website_live(website) else 0
        
        # Keep the lead when there is any working way to reach the business
        if is_phone_valid or is_email_valid:
            status = 'valid'
            valid_count += 1
            live_site_count += website_live or 0
            print(f"[VALID] {name} | phone: {phone} | email: {email}")
        else:
            status = 'invalid'
            invalid_count += 1
            print(f"[INVALID] {name} | no usable phone or email")
        
        cursor.execute(
            "UPDATE leads SET status = ?, website_live = ? WHERE id = ?",
            (status, website_live, lead_id),
        )
            
    conn.commit()
    conn.close()
    
    print("\n==========================================")
    print(f"Cleaning complete. Marked {valid_count} valid and {invalid_count} invalid, nothing was deleted")
    print(f"Of the valid leads, {live_site_count} also have a live website")
    print("Review with: SELECT * FROM leads WHERE status = 'valid' AND website_live = 1")
    print("==========================================\n")

if __name__ == "__main__":
    clean_and_validate_leads()
