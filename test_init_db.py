import sqlite3

import pytest

from init_db import init_db

DB_PATH = 'data/leads.db'

EXPECTED_COLUMNS = [
    'id',
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
]

LEGACY_SCHEMA = '''
    CREATE TABLE leads (
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
        UNIQUE(business_name, zip_code)
    )
'''


@pytest.fixture(autouse=True)
def run_in_tmp_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


def insert_lead(conn, business_name, zip_code):
    conn.execute(
        'INSERT INTO leads (business_name, zip_code) VALUES (?, ?)',
        (business_name, zip_code),
    )
    conn.commit()


def test_creates_database_file_and_parent_directory(tmp_path):
    init_db()
    assert (tmp_path / DB_PATH).is_file()


def test_creates_leads_table_with_expected_columns():
    init_db()
    with sqlite3.connect(DB_PATH) as conn:
        columns = [row[1] for row in conn.execute('PRAGMA table_info(leads)')]
    assert columns == EXPECTED_COLUMNS


def test_is_idempotent_and_preserves_existing_rows():
    init_db()
    with sqlite3.connect(DB_PATH) as conn:
        insert_lead(conn, 'Acme Plumbing', '78701')

    init_db()

    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute('SELECT business_name, zip_code FROM leads').fetchall()
    assert rows == [('Acme Plumbing', '78701')]


def test_rejects_duplicate_name_and_zip():
    init_db()
    with sqlite3.connect(DB_PATH) as conn:
        insert_lead(conn, 'Acme Plumbing', '78701')
        with pytest.raises(sqlite3.IntegrityError):
            insert_lead(conn, 'Acme Plumbing', '78701')


def test_allows_same_name_in_different_zip():
    init_db()
    with sqlite3.connect(DB_PATH) as conn:
        insert_lead(conn, 'Acme Plumbing', '78701')
        insert_lead(conn, 'Acme Plumbing', '78702')
        count = conn.execute('SELECT COUNT(*) FROM leads').fetchone()[0]
    assert count == 2


def test_new_leads_start_with_the_new_status():
    init_db()
    with sqlite3.connect(DB_PATH) as conn:
        insert_lead(conn, 'Acme Plumbing', '78701')
        row = conn.execute('SELECT status, website_live FROM leads').fetchone()
    assert row == ('new', None)


def test_upgrades_a_database_created_before_the_status_columns(tmp_path):
    import os

    os.makedirs('data', exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(LEGACY_SCHEMA)
        conn.execute(
            "INSERT INTO leads (business_name, zip_code, phone) VALUES ('Acme Plumbing', '78701', '555')"
        )

    init_db()

    with sqlite3.connect(DB_PATH) as conn:
        columns = [row[1] for row in conn.execute('PRAGMA table_info(leads)')]
        row = conn.execute('SELECT business_name, phone, status, website_live FROM leads').fetchone()
    assert columns == EXPECTED_COLUMNS
    # The existing row survives the migration and picks up the default status
    assert row == ('Acme Plumbing', '555', 'new', None)


def test_migration_is_safe_to_run_repeatedly():
    init_db()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE leads SET status = 'valid'")
        insert_lead(conn, 'Acme Plumbing', '78701')
        conn.execute("UPDATE leads SET status = 'valid' WHERE business_name = 'Acme Plumbing'")

    init_db()

    with sqlite3.connect(DB_PATH) as conn:
        columns = [row[1] for row in conn.execute('PRAGMA table_info(leads)')]
        status = conn.execute('SELECT status FROM leads').fetchone()[0]
    assert columns == EXPECTED_COLUMNS
    # A second run must not reset statuses that cleaning already assigned
    assert status == 'valid'


def test_null_zip_code_bypasses_unique_constraint():
    # SQLite treats NULLs as distinct, so rows missing a zip are never deduplicated
    init_db()
    with sqlite3.connect(DB_PATH) as conn:
        insert_lead(conn, 'Acme Plumbing', None)
        insert_lead(conn, 'Acme Plumbing', None)
        count = conn.execute('SELECT COUNT(*) FROM leads').fetchone()[0]
    assert count == 2
