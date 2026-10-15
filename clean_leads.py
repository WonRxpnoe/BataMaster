import argparse
import logging
import re
import sqlite3
import sys

import dns.resolver
import requests

from init_db import DB_PATH, init_db

logger = logging.getLogger(__name__)

EMAIL_REGEX = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'

# Many small business sites sit behind bot protection that rejects unbranded clients
BROWSER_USER_AGENT = (
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
)

REQUEST_TIMEOUT = 5

MIN_PHONE_DIGITS = 10


def check_email_mx(email):
    """Check the MX records of the email domain so the address will not hard bounce."""
    if not email or not re.match(EMAIL_REGEX, email):
        return False
    domain = email.split('@')[1]
    try:
        records = dns.resolver.resolve(domain, 'MX')
        return len(records) > 0
    except Exception:
        return False


def check_website_live(website):
    """Check that the website still responds, flagging closed or expired businesses."""
    if not website:
        return False
    if not website.startswith(('http://', 'https://')):
        website = 'https://' + website
    try:
        # stream=True reads the status line without downloading the whole page
        with requests.get(
            website,
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
            headers={'User-Agent': BROWSER_USER_AGENT},
            stream=True,
        ) as response:
            # Anything below 400 means the server answered for this address
            return response.status_code < 400
    except Exception:
        return False


def has_usable_phone(phone):
    return bool(phone and len(re.sub(r'\D', '', phone)) >= MIN_PHONE_DIGITS)


def clean_and_validate_leads(only_new=False):
    """Read leads from the database and record how usable each one is."""
    init_db()
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    query = "SELECT id, business_name, phone, website, email FROM leads"
    if only_new:
        query += " WHERE status = 'new'"
    cursor.execute(query)
    rows = cursor.fetchall()

    valid_count = 0
    invalid_count = 0
    live_site_count = 0

    logger.info("Cleaning %s raw leads from the database", len(rows))

    for row in rows:
        lead_id, name, phone, website, email = row

        is_phone_valid = has_usable_phone(phone)
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
            logger.debug("Valid: %s | phone: %s | email: %s", name, phone, email)
        else:
            status = 'invalid'
            invalid_count += 1
            logger.debug("Invalid: %s | no usable phone or email", name)

        cursor.execute(
            "UPDATE leads SET status = ?, website_live = ? WHERE id = ?",
            (status, website_live, lead_id),
        )

    conn.commit()
    conn.close()

    logger.info("Marked %s valid and %s invalid, nothing was deleted", valid_count, invalid_count)
    logger.info("Of the valid leads, %s also have a live website", live_site_count)
    logger.info("Review with: SELECT * FROM leads WHERE status = 'valid' AND website_live = 1")
    return valid_count, invalid_count


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Validate scraped leads and record how usable they are.")
    parser.add_argument(
        '--only-new', action='store_true',
        help="Only check leads that have not been validated yet",
    )
    parser.add_argument('--verbose', action='store_true', help='Show one line per lead')
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    # Keep the root logger quiet so third party debug output stays out of the way
    logging.basicConfig(level=logging.WARNING, format='%(levelname)s %(message)s')
    logger.setLevel(logging.DEBUG if args.verbose else logging.INFO)
    clean_and_validate_leads(only_new=args.only_new)
    return 0


if __name__ == "__main__":
    sys.exit(main())
