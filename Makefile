VENV := .venv
BIN := $(VENV)/bin
COMPOSE := docker compose -f dev/docker-compose.yml

.PHONY: help setup test lint format ha ha-restart ha-logs ha-stop clean

help:  ## Show available commands
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  make %-12s %s\n", $$1, $$2}'

$(BIN)/pytest: requirements_test.txt
	python3 -m venv $(VENV)
	$(BIN)/pip install --upgrade pip
	$(BIN)/pip install -r requirements_test.txt
	@touch $@

setup: $(BIN)/pytest  ## Create .venv and install test tooling (Python 3.14)

test: setup  ## Run the test suite with coverage
	$(BIN)/pytest

lint: setup  ## Ruff lint + format check + mypy (strict)
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .
	$(BIN)/mypy custom_components

format: setup  ## Auto-format and fix lint where possible
	$(BIN)/ruff format .
	$(BIN)/ruff check --fix .

ha:  ## Start a local Home Assistant on http://localhost:8123
	$(COMPOSE) up -d
	@echo "Home Assistant: http://localhost:8123 (first start takes a minute)"

ha-restart:  ## Restart Home Assistant to load code changes
	$(COMPOSE) restart

ha-logs:  ## Follow Home Assistant logs, filtered to this integration
	$(COMPOSE) logs -f homeassistant | grep --line-buffered -i hikvision

ha-stop:  ## Stop the local Home Assistant
	$(COMPOSE) down

clean:  ## Remove the virtualenv and caches
	rm -rf $(VENV) .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov
