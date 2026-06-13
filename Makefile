.PHONY: install run once web test lint format clean

VENV ?= .venv
PY = $(VENV)/bin/python
PIP = $(VENV)/bin/pip

install:        ## create venv + install dependencies
	python3.12 -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt

run:            ## boot warm-up + 24/7 scheduler + dashboard
	$(PY) run.py

once:           ## run one full end-to-end cycle and exit
	$(PY) run.py --once

web:            ## dashboard only (no scheduler)
	$(PY) run.py --web-only

test:           ## run the test suite
	$(PY) -m pytest -q

lint:           ## static lint (ruff)
	$(PY) -m ruff check ctg tests

format:         ## auto-format (ruff)
	$(PY) -m ruff format ctg tests

clean:          ## remove caches
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache
