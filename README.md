# datamaster

Lead pipeline for med spa and skin care businesses, built on the Google Places API.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
export GOOGLE_API_KEY=your-key-here
```

The database, the CSV exports, and `.env` files are all gitignored.

## Pipeline

The three stages share `data/leads.db` and create or upgrade it automatically, so
they can be run in any order and as often as needed.

**1. Scrape.** Searches Google Places for each keyword in every requested zip code,
fetches phone and website details, then visits each site to extract an email.

```bash
.venv/bin/python medspa_scraper.py --zip 90210 90211 --state CA
.venv/bin/python medspa_scraper.py --zip 90210 --skip-recent 7   # skip recent work
```

**2. Clean.** Checks that the phone number is complete, that the email domain has MX
records, and whether the website still answers. Results are written to the `status`
and `website_live` columns. Nothing is ever deleted.

```bash
.venv/bin/python clean_leads.py
.venv/bin/python clean_leads.py --only-new   # skip leads already validated
```

**3. Export.**

```bash
.venv/bin/python export_leads.py                     # valid leads to data/leads.csv
.venv/bin/python export_leads.py --live-site-only    # only leads with a working site
.venv/bin/python export_leads.py --status all
```

Add `--verbose` to the scraper or the cleaner for one log line per record.

## Dry run without an API key

`mock_places_server.py` serves fake Places responses and the business websites they
point at, so the real commands can be exercised end to end for free.

```bash
.venv/bin/python mock_places_server.py &
export PLACES_BASE_URL=http://127.0.0.1:8899 GOOGLE_API_KEY=mock-key
.venv/bin/python medspa_scraper.py --zip 90210 90211
.venv/bin/python clean_leads.py
.venv/bin/python export_leads.py
```

Unset `PLACES_BASE_URL` to go back to the real API. The mock data deliberately
includes a permanently closed business, one with no website, one whose site has no
email, and one with no phone number.

## Schema

`leads` holds one row per business, deduplicated on `place_id`. A lead is `valid`
when it has either a usable phone number or a deliverable email. `website_live`
records the site check separately, so a stricter list is a query rather than a
code change:

```sql
SELECT * FROM leads WHERE status = 'valid' AND website_live = 1;
```

`scrape_runs` records when each zip code was last completed, which is what
`--skip-recent` reads.

## Tests

```bash
.venv/bin/python -m pytest
```

Each module has its own suite against mocked HTTP and DNS. `test_smoke.py` runs all
three stages in sequence against the mock server, which is what catches a column or
a status value drifting apart between stages.
