import argparse
import logging
import os
import sqlite3
import sys

import pandas as pd

from init_db import DB_PATH, init_db

logger = logging.getLogger(__name__)

DEFAULT_OUTPUT = 'data/leads.csv'

EXPORT_COLUMNS = [
    'business_name',
    'industry',
    'phone',
    'email',
    'website',
    'address',
    'zip_code',
    'state',
    'status',
    'website_live',
    'source',
    'place_id',
]


def build_query(status=None, live_site_only=False):
    """Build the export query and its parameters from the requested filters."""
    query = f"SELECT {', '.join(EXPORT_COLUMNS)} FROM leads"
    conditions = []
    params = []

    if status:
        conditions.append('status = ?')
        params.append(status)
    if live_site_only:
        conditions.append('website_live = 1')

    if conditions:
        query += ' WHERE ' + ' AND '.join(conditions)
    query += ' ORDER BY business_name'
    return query, params


def export_leads(output_path=DEFAULT_OUTPUT, status='valid', live_site_only=False):
    """Write the matching leads to a CSV file and return how many rows were written."""
    init_db()
    query, params = build_query(status, live_site_only)

    with sqlite3.connect(DB_PATH) as conn:
        frame = pd.read_sql_query(query, conn, params=params)

    # Businesses with no website leave this NULL, which would otherwise widen the whole
    # column to float and write every flag as 1.0
    frame['website_live'] = frame['website_live'].astype('Int64')

    directory = os.path.dirname(output_path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    frame.to_csv(output_path, index=False)

    logger.info("Exported %s leads to %s", len(frame), output_path)
    if frame.empty:
        logger.warning("No leads matched the filters. Run the scraper and cleaner first.")
    return len(frame)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Export leads from the database to a CSV file.")
    parser.add_argument('--output', default=DEFAULT_OUTPUT, help=f'Destination path (default: {DEFAULT_OUTPUT})')
    parser.add_argument(
        '--status', default='valid', choices=['new', 'valid', 'invalid', 'all'],
        help='Only export leads with this status (default: valid)',
    )
    parser.add_argument(
        '--live-site-only', action='store_true',
        help='Only export leads whose website answered during cleaning',
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    # Keep the root logger quiet so third party debug output stays out of the way
    logging.basicConfig(level=logging.WARNING, format='%(levelname)s %(message)s')
    logger.setLevel(logging.INFO)
    status = None if args.status == 'all' else args.status
    export_leads(output_path=args.output, status=status, live_site_only=args.live_site_only)
    return 0


if __name__ == "__main__":
    sys.exit(main())
