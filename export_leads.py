import argparse
import logging
import os
import re
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


def _digits(value):
    return re.sub(r'\D', '', str(value or ''))


def _simplified(value):
    """Reduce a name or an address to the core that survives rewording."""
    return re.sub(r'[^a-z0-9]', '', str(value or '').lower())


def contact_signals(row):
    """The identifiers that, shared at one address, mean two listings are one business.

    A name is deliberately excluded: Google punctuates the same business differently
    between its duplicate entries, so "Quiet Esthetics" and "Quiet Esthetics LLC" must
    still be recognised as one shop.
    """
    signals = set()
    phone = _digits(row['phone'])[-10:]
    if len(phone) == 10:
        signals.add(('phone', phone))
    email = str(row['email'] or '').strip().lower()
    if email:
        signals.add(('email', email))
    website = _simplified(row['website'])
    if website:
        signals.add(('website', website))
    return signals


def group_one_business_together(frame):
    """Label each listing with the business it belongs to.

    Listings are only ever compared within one street address, which is what keeps
    branches of a chain apart: they share a central phone number and a website, but
    never an address. Within a single address, any shared phone, email or website means
    Google listed one shop twice, which it does whenever a business has a duplicate
    entry. Chaining the matches lets a listing carrying only an email join one carrying
    only a phone, as long as some third listing ties the two together.
    """
    parent = list(range(len(frame)))

    def root(index):
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    first_seen = {}
    for position, (_, row) in enumerate(frame.iterrows()):
        address = _simplified(row['address'])
        keys = [(address, signal) for signal in contact_signals(row)]
        # A listing with no way to reach it at all can only be matched on its name
        keys = keys or [(address, ('name', _simplified(row['business_name'])))]
        for key in keys:
            if key in first_seen:
                parent[root(position)] = root(first_seen[key])
            else:
                first_seen[key] = position
    return [root(position) for position in range(len(frame))]


def _first_present(values):
    """Take the first value a column actually carries across the merged listings."""
    present = values[values.notna() & (values.astype(str) != '')]
    return present.iloc[0] if len(present) else values.iloc[0]


# A merged row carries the contact details of every listing behind it, so a verdict
# reached on one listing alone can only understate how reachable the business is
STATUS_RANK = {'invalid': 0, 'new': 1, 'valid': 2}


def _best_status(values):
    """The most favourable verdict any of the merged listings earned."""
    return max(values, key=lambda status: STATUS_RANK.get(status, -1))


def _best_liveness(values):
    """A website that answered for one listing answers for the business."""
    present = values[values.notna()]
    return present.max() if len(present) else values.iloc[0]


def merge_duplicate_listings(frame):
    """Collapse repeated listings of one business into a single row.

    The surviving row keeps the first value each column actually has, so a listing
    that carried only an email still contributes that email to the merged result.
    """
    if frame.empty:
        return frame
    groups = group_one_business_together(frame)
    # status and website_live are conclusions the cleaner reached about a single
    # listing, so merging them the way a phone number is merged would carry over a
    # verdict that the combined contact details have already outgrown
    how = dict.fromkeys(frame.columns, _first_present)
    how['status'] = _best_status
    how['website_live'] = _best_liveness
    merged = frame.groupby(groups, sort=False).agg(how)
    return merged.reset_index(drop=True).sort_values('business_name', ignore_index=True)


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

    listings = len(frame)
    frame = merge_duplicate_listings(frame)
    if listings > len(frame):
        logger.info("Merged %s repeat listings of a business already in the list",
                    listings - len(frame))

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
    parser.add_argument(
        '--output', default=DEFAULT_OUTPUT,
        help=f'Destination path (default: {DEFAULT_OUTPUT})',
    )
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
