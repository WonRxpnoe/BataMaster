"""Rank US zip codes by the average income reported on their tax returns.

The figures come from the IRS Statistics of Income file, which reports, for every zip
code, how many returns were filed and the total adjusted gross income on them. Dividing
one by the other gives the average income per return, which is what the ranking uses.

    python rank_income_by_zip.py --top 50

Averages, not medians: a handful of very large incomes lift a zip code's average well
above what a typical household there earns. That is a reasonable signal for finding
areas with money to spend, and a poor one for describing a typical resident.
"""

import argparse
import logging
import os
import sys

import pandas as pd

logger = logging.getLogger(__name__)

SOURCE_PATH = 'data/irs_zip_2022.csv'
DEFAULT_OUTPUT = 'data/income_by_zip.csv'

# Returns filed, and total adjusted gross income in thousands of dollars
RETURNS = 'N1'
TOTAL_AGI = 'A00100'

# Rows the IRS uses for state and national totals rather than a real zip code
AGGREGATE_ZIPS = {'00000', '99999'}

# Below this a zip code's average swings wildly on a few returns, so the ranking would
# be topped by tiny places that tell us nothing about where to look for customers
MIN_RETURNS = 200


def load_income(source_path=SOURCE_PATH, min_returns=MIN_RETURNS):
    """Return one row per zip code with its average income, richest first."""
    frame = pd.read_csv(
        source_path,
        usecols=['STATE', 'ZIPCODE', RETURNS, TOTAL_AGI],
        dtype={'ZIPCODE': str},
    )

    frame = frame[~frame['ZIPCODE'].isin(AGGREGATE_ZIPS)]
    frame = frame[frame[RETURNS] >= min_returns]

    # The IRS reports income in thousands, so the product of the two columns is dollars
    frame['avg_income'] = (frame[TOTAL_AGI] * 1000 / frame[RETURNS]).round().astype(int)
    frame['returns'] = frame[RETURNS].astype(int)

    ranked = frame[['ZIPCODE', 'STATE', 'avg_income', 'returns']].rename(
        columns={'ZIPCODE': 'zip_code', 'STATE': 'state'}
    )
    ranked = ranked.sort_values('avg_income', ascending=False, ignore_index=True)
    ranked.insert(0, 'rank', ranked.index + 1)
    return ranked


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--source', default=SOURCE_PATH, help='IRS SOI zip code file')
    parser.add_argument('--output', default=DEFAULT_OUTPUT, help='Where to write the ranking')
    parser.add_argument('--top', type=int, default=25, help='How many rows to print')
    parser.add_argument(
        '--state', help='Limit the printed rows to one two letter state code',
    )
    parser.add_argument(
        '--min-returns', type=int, default=MIN_RETURNS,
        help=f'Ignore zip codes with fewer returns than this (default: {MIN_RETURNS})',
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')

    if not os.path.exists(args.source):
        logger.error("Missing %s. Download it from www.irs.gov/pub/irs-soi first.", args.source)
        return 1

    ranked = load_income(args.source, args.min_returns)

    directory = os.path.dirname(args.output)
    if directory:
        os.makedirs(directory, exist_ok=True)
    ranked.to_csv(args.output, index=False)
    logger.info("Ranked %s zip codes into %s", len(ranked), args.output)

    shown = ranked[ranked['state'] == args.state] if args.state else ranked
    print(shown.head(args.top).to_string(index=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
