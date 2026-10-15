"""Fixtures shared by every suite.

Each test gets its own working directory, so the database a test creates is thrown
away with it and the suites cannot see each other's rows.
"""

import logging

import pytest

import medspa_scraper

MODULES_UNDER_TEST = ['clean_leads', 'export_leads', 'init_db', 'medspa_scraper']


@pytest.fixture(autouse=True)
def isolated_workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture(autouse=True)
def capture_logs(caplog):
    for module in MODULES_UNDER_TEST:
        caplog.set_level(logging.DEBUG, logger=module)
    return caplog


@pytest.fixture(autouse=True)
def fast_waits(monkeypatch):
    """Strip the production delays so the suite does not sleep for real."""
    monkeypatch.setattr(medspa_scraper, 'PAGE_TOKEN_DELAY', 0)
    monkeypatch.setattr(medspa_scraper, 'RETRY_BACKOFF', 0)
    monkeypatch.setattr(medspa_scraper, 'MIN_SECONDS_BETWEEN_REQUESTS', 0)
