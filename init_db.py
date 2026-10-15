import logging
import os
import sqlite3

logger = logging.getLogger(__name__)

DB_DIR = 'data'
DB_PATH = 'data/leads.db'

# Every column of leads except the surrogate id, in the order the table declares them
LEADS_COLUMNS = [
    'business_name',
    'industry',
    'phone',
    'website',
    'email',
    'state',
    'zip_code',
    'address',
    'source',
    'status',
    'website_live',
    'place_id',
]

# Columns introduced after the first release, applied to databases created earlier
LATER_COLUMNS = {
    'status': "TEXT NOT NULL DEFAULT 'new'",
    'website_live': 'INTEGER',
    'place_id': 'TEXT',
}

# The original schema treated a name and a zip code as a business identity. Two
# branches of one chain share both, so the second was silently dropped.
LEGACY_NAME_ZIP_CONSTRAINT = 'UNIQUE(business_name, zip_code)'


def create_leads_sql(table_name):
    return f'''
        CREATE TABLE IF NOT EXISTS {table_name} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            business_name TEXT,
            industry TEXT,
            phone TEXT,
            website TEXT,
            email TEXT,
            state TEXT,
            zip_code TEXT,
            address TEXT,
            source TEXT,
            status TEXT NOT NULL DEFAULT 'new',
            website_live INTEGER,
            place_id TEXT
        )
    '''


def table_sql(cursor, table_name):
    row = cursor.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table_name,)
    ).fetchone()
    return row[0] if row else ''


def add_missing_columns(cursor):
    existing = {row[1] for row in cursor.execute('PRAGMA table_info(leads)')}
    for column, definition in LATER_COLUMNS.items():
        if column not in existing:
            cursor.execute(f'ALTER TABLE leads ADD COLUMN {column} {definition}')


def drop_name_zip_constraint(cursor):
    """Rebuild leads without the old name and zip uniqueness, preserving every row.

    SQLite cannot drop a constraint in place, so the table has to be copied. Identity
    is left entirely to the unique index on place_id, which is what Google treats as
    one business.
    """
    if LEGACY_NAME_ZIP_CONSTRAINT not in table_sql(cursor, 'leads'):
        return

    columns = ', '.join(LEADS_COLUMNS)
    cursor.execute(create_leads_sql('leads_rebuilt'))
    cursor.execute(f'INSERT INTO leads_rebuilt (id, {columns}) SELECT id, {columns} FROM leads')
    cursor.execute('DROP TABLE leads')
    cursor.execute('ALTER TABLE leads_rebuilt RENAME TO leads')
    logger.info('Rebuilt leads so branches of one chain are no longer merged')


def init_db():
    os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Readers no longer block the writer, so the cleaner and the scraper can overlap
    cursor.execute('PRAGMA journal_mode=WAL')

    cursor.execute(create_leads_sql('leads'))
    add_missing_columns(cursor)
    drop_name_zip_constraint(cursor)

    # A unique index rather than a column constraint, so existing databases can be
    # upgraded. SQLite treats NULLs as distinct, leaving rows scraped before place_id
    # was stored untouched.
    cursor.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_leads_place_id ON leads(place_id)')
    # Both the cleaner and the exporter filter on status
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status)')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS scrape_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            zip_code TEXT NOT NULL,
            state TEXT NOT NULL,
            scraped_at TEXT NOT NULL,
            leads_found INTEGER NOT NULL DEFAULT 0
        )
    ''')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_scrape_runs_area ON scrape_runs(zip_code, state)')

    conn.commit()
    conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    init_db()
    logger.info("Database initialized: %s is ready", DB_PATH)
