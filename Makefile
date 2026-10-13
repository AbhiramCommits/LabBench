# LabBench - developer entry points
SHELL := /bin/bash
VENV := .venv
PY := $(VENV)/bin/python
LABBENCH := $(VENV)/bin/labbench

.PHONY: install lint format test test-integration test-all lab-up lab-down e2e clean

install:
	$(PY) -m pip install -e '.[dev]'
	$(VENV)/bin/ansible-galaxy collection install -r ansible/requirements.yml

lint:
	$(VENV)/bin/ruff check src tests
	$(VENV)/bin/black --check src tests
	shellcheck scripts/collect_lab_config.sh
	cd ansible && ../$(VENV)/bin/ansible-lint

format:
	$(VENV)/bin/ruff check src tests --fix
	$(VENV)/bin/black src tests

test:
	$(PY) -m pytest tests -m "not integration and not validation"

test-integration:
	$(PY) -m pytest tests -m "integration"

test-all:
	$(PY) -m pytest tests -m "not validation"

lab-up:
	$(LABBENCH) lab up

lab-down:
	$(LABBENCH) lab down

e2e: lab-up
	$(LABBENCH) provision
	$(LABBENCH) attach
	$(LABBENCH) validate
	$(LABBENCH) collect
	$(LABBENCH) report

clean:
	-$(LABBENCH) lab down
	rm -rf artifacts logs .pytest_cache .ruff_cache ansible/.fact_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
