import os
import sqlite3
import pandas as pd

# Columns introduced after the first release, applied to databases created earlier
LATER_COLUMNS = {
    'status': "TEXT NOT NULL DEFAULT 'new'",
    'website_live': 'INTEGER',
}

def add_missing_columns(cursor):
    existing = {row[1] for row in cursor.execute('PRAGMA table_info(leads)')}
    for column, definition in LATER_COLUMNS.items():
        if column not in existing:
            cursor.execute(f'ALTER TABLE leads ADD COLUMN {column} {definition}')

def init_db():
    os.makedirs('data', exist_ok=True)
    conn = sqlite3.connect('data/leads.db')
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
            UNIQUE(business_name, zip_code)
        )
    ''')
    add_missing_columns(cursor)
    conn.commit()
    conn.close()

if __name__ == "__main__":
    init_db()
    print("Database initialized: data/leads.db is ready!")
