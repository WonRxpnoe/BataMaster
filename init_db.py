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
LEGACY_NAME_ZIP_COLUMNS = ['business_name', 'zip_code']

# Scratch table for the rebuild, cleared first so an interrupted attempt cannot wedge
# every later run
REBUILD_TABLE = 'leads_rebuilt'


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


def has_name_zip_constraint(cursor):
    """Look for the index SQLite creates behind a UNIQUE clause in CREATE TABLE.

    Reading the index rather than matching the stored CREATE statement as text means
    spacing and formatting in an older database cannot hide the constraint.
    """
    for _, name, is_unique, origin, _ in cursor.execute('PRAGMA index_list(leads)'):
        # origin 'u' marks an index backing a UNIQUE clause, 'c' a CREATE INDEX
        if not is_unique or origin != 'u':
            continue
        columns = [row[2] for row in cursor.execute(f'PRAGMA index_info("{name}")')]
        if columns == LEGACY_NAME_ZIP_COLUMNS:
            return True
    return False


def add_missing_columns(cursor):
    existing = {row[1] for row in cursor.execute('PRAGMA table_info(leads)')}
    for column, definition in LATER_COLUMNS.items():
        if column not in existing:
            cursor.execute(f'ALTER TABLE leads ADD COLUMN {column} {definition}')


def drop_name_zip_constraint(conn):
    """Rebuild leads without the old name and zip uniqueness, preserving every row.

    SQLite cannot drop a constraint in place, so the table has to be copied. Identity
    is left entirely to the unique index on place_id, which is what Google treats as
    one business.

    The copy and the swap run as a single transaction, and any table left over from an
    interrupted attempt is cleared first. Without both, a crash partway through would
    leave a scratch table that makes every later run fail on a duplicate id.
    """
    cursor = conn.cursor()
    if not has_name_zip_constraint(cursor):
        return

    columns = ', '.join(LEADS_COLUMNS)
    cursor.execute(f'DROP TABLE IF EXISTS {REBUILD_TABLE}')
    cursor.execute('BEGIN')
    try:
        cursor.execute(create_leads_sql(REBUILD_TABLE))
        cursor.execute(
            f'INSERT INTO {REBUILD_TABLE} (id, {columns}) SELECT id, {columns} FROM leads'
        )
        cursor.execute('DROP TABLE leads')
        cursor.execute(f'ALTER TABLE {REBUILD_TABLE} RENAME TO leads')
    except Exception:
        conn.rollback()
        raise
    conn.commit()
    logger.info('Rebuilt leads so branches of one chain are no longer merged')


def init_db():
    os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    # Transactions are opened by hand, so the rebuild below is genuinely atomic
    conn.isolation_level = None
    cursor = conn.cursor()

    # Readers no longer block the writer, so the cleaner and the scraper can overlap
    cursor.execute('PRAGMA journal_mode=WAL')

    cursor.execute(create_leads_sql('leads'))
    add_missing_columns(cursor)
    drop_name_zip_constraint(conn)

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
