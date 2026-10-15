PYTHON := .venv/bin/python
DRYRUN_DIR := .dryrun
MOCK_URL := http://127.0.0.1:8899

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

# Exercise all three stages against the local mock, no API key and no quota spent.
# Everything runs inside DRYRUN_DIR so invented businesses never reach data/leads.db.
dryrun:
	@set -e; \
	root="$(CURDIR)"; py="$$root/$(PYTHON)"; \
	rm -rf "$(DRYRUN_DIR)"; mkdir -p "$(DRYRUN_DIR)"; \
	"$$py" mock_places_server.py & \
	mock_pid=$$!; \
	trap 'kill $$mock_pid 2>/dev/null' EXIT; \
	sleep 2; \
	cd "$(DRYRUN_DIR)"; \
	export PYTHONPATH="$$root" PLACES_BASE_URL=$(MOCK_URL) GOOGLE_API_KEY=mock-key; \
	"$$py" "$$root/medspa_scraper.py" --zip 90210; \
	"$$py" "$$root/clean_leads.py"; \
	"$$py" "$$root/export_leads.py"; \
	echo "Dry run artifacts are in $(DRYRUN_DIR)/data/"

clean:
	rm -rf .pytest_cache .coverage __pycache__ $(DRYRUN_DIR)
