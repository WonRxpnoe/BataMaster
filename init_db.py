import os
import sqlite3

DB_DIR = 'data'
DB_PATH = 'data/leads.db'

# Columns introduced after the first release, applied to databases created earlier
LATER_COLUMNS = {
    'status': "TEXT NOT NULL DEFAULT 'new'",
    'website_live': 'INTEGER',
    'place_id': 'TEXT',
}

def add_missing_columns(cursor):
    existing = {row[1] for row in cursor.execute('PRAGMA table_info(leads)')}
    for column, definition in LATER_COLUMNS.items():
        if column not in existing:
            cursor.execute(f'ALTER TABLE leads ADD COLUMN {column} {definition}')

def init_db():
    os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS leads (
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
            place_id TEXT,
            UNIQUE(business_name, zip_code)
        )
    ''')
    add_missing_columns(cursor)
    # A unique index rather than a column constraint, so existing databases can be upgraded.
    # SQLite treats NULLs as distinct, leaving rows scraped before place_id was stored untouched.
    cursor.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_leads_place_id ON leads(place_id)')
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
    init_db()
    print("Database initialized: data/leads.db is ready!")
