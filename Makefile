PYTHON := .venv/bin/python

.PHONY: install check lint test coverage dryrun clean

install:
	python3 -m venv .venv
	$(PYTHON) -m pip install --quiet --upgrade pip
	$(PYTHON) -m pip install --quiet -r requirements-dev.txt

# What CI runs, and what to run before committing
check: lint test

lint:
	$(PYTHON) -m pyflakes *.py
	$(PYTHON) -m pycodestyle *.py

test:
	$(PYTHON) -m pytest -q

coverage:
	$(PYTHON) -m pytest -q --cov --cov-report=term-missing

# Exercise all three stages against the local mock, no API key and no quota spent
dryrun:
	$(PYTHON) mock_places_server.py & echo $$! > .mock.pid
	sleep 2
	PLACES_BASE_URL=http://127.0.0.1:8899 GOOGLE_API_KEY=mock-key $(PYTHON) medspa_scraper.py --zip 90210
	PLACES_BASE_URL=http://127.0.0.1:8899 $(PYTHON) clean_leads.py
	$(PYTHON) export_leads.py
	kill `cat .mock.pid` && rm -f .mock.pid

clean:
	rm -rf .pytest_cache .coverage __pycache__ .mock.pid
