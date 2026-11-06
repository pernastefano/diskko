VENV ?= .venv
PY := $(VENV)/bin/python
PORT ?= 8080

.PHONY: venv install run test lint format

venv:
	python3.12 -m venv $(VENV)

install: venv
	$(PY) -m pip install -r requirements-dev.txt

run:
	$(PY) -m uvicorn app.main:app --reload --port $(PORT)

test:
	$(PY) -m pytest -q

lint:
	$(PY) -m ruff check .
	$(PY) -m ruff format --check .

format:
	$(PY) -m ruff check --fix .
	$(PY) -m ruff format .
