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
