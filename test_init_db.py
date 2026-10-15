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
    'place_id',
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


def test_place_id_is_unique_across_rows():
    init_db()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO leads (business_name, zip_code, place_id) VALUES ('Glow Spa', '90210', 'place-1')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            # The same business found under a different keyword or search zip
            conn.execute(
                "INSERT INTO leads (business_name, zip_code, place_id) VALUES ('Glow Spa', '90212', 'place-1')"
            )


def test_rows_without_a_place_id_do_not_collide():
    # Leads scraped before place_id was stored all carry NULL, which SQLite treats as distinct
    init_db()
    with sqlite3.connect(DB_PATH) as conn:
        insert_lead(conn, 'Acme Plumbing', '78701')
        insert_lead(conn, 'Beta Plumbing', '78702')
        assert conn.execute('SELECT COUNT(*) FROM leads WHERE place_id IS NULL').fetchone()[0] == 2


def test_migration_adds_the_place_id_index_to_a_legacy_database():
    import os

    os.makedirs('data', exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(LEGACY_SCHEMA)
        conn.execute("INSERT INTO leads (business_name, zip_code) VALUES ('Acme Plumbing', '78701')")

    init_db()

    with sqlite3.connect(DB_PATH) as conn:
        indexes = {row[1] for row in conn.execute('PRAGMA index_list(leads)')}
        conn.execute("UPDATE leads SET place_id = 'place-1'")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO leads (business_name, zip_code, place_id) VALUES ('Other', '78702', 'place-1')"
            )
    assert 'idx_leads_place_id' in indexes


def test_creates_the_scrape_runs_table():
    init_db()
    with sqlite3.connect(DB_PATH) as conn:
        columns = [row[1] for row in conn.execute('PRAGMA table_info(scrape_runs)')]
    assert columns == ['id', 'zip_code', 'state', 'scraped_at', 'leads_found']


def test_adds_scrape_runs_to_a_legacy_database():
    import os

    os.makedirs('data', exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(LEGACY_SCHEMA)

    init_db()

    with sqlite3.connect(DB_PATH) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert 'scrape_runs' in tables


def test_scrape_runs_survive_a_second_init():
    init_db()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO scrape_runs (zip_code, state, scraped_at, leads_found) "
            "VALUES ('90210', 'CA', '2026-01-01T00:00:00+00:00', 7)"
        )

    init_db()

    with sqlite3.connect(DB_PATH) as conn:
        assert conn.execute('SELECT leads_found FROM scrape_runs').fetchone() == (7,)


def test_null_zip_code_bypasses_unique_constraint():
    # SQLite treats NULLs as distinct, so rows missing a zip are never deduplicated
    init_db()
    with sqlite3.connect(DB_PATH) as conn:
        insert_lead(conn, 'Acme Plumbing', None)
        insert_lead(conn, 'Acme Plumbing', None)
        count = conn.execute('SELECT COUNT(*) FROM leads').fetchone()[0]
    assert count == 2
