# jansky-observe — station software for the Discovery Dish (see plans/jansky_observe.md).
# Mirrors the jansky / jansky-research conventions: everything goes through uv
# so you get the pinned Python 3.12 environment.

.DEFAULT_GOAL := help

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

setup: ## Install the pinned environment (uv sync)
	uv sync

test: ## Run the test suite
	uv run pytest

cov: ## Tests with coverage (85% floor)
	uv run pytest --cov=jansky_observe --cov-report=term-missing

e2e: ## Browser end-to-end tests (needs chromium: uv run playwright install chromium)
	uv run pytest tests/e2e -m e2e

lint: ## Ruff lint
	uv run ruff check src/ tests/

fmt: ## Ruff format
	uv run ruff format src/ tests/

typecheck: ## Mypy
	uv run mypy

run: ## Run the API server (dev, auto-reload)
	uv run uvicorn jansky_observe.server.app:app --host 0.0.0.0 --port 8000 --reload

daemon: ## Run the capture daemon (synthetic source, M0)
	uv run jansky-observe-capture --synthetic

build: ## Build sdist + wheel into dist/
	uv build

qemu-install: ## Full-fidelity install test against the pinned Pi OS image (QEMU)
	deploy/qemu/run-install-test.sh

clean: ## Remove caches and build artifacts
	rm -rf .pytest_cache/ .ruff_cache/ .mypy_cache/ dist/ .coverage
	find . -type d -name __pycache__ -prune -exec rm -rf {} +

.PHONY: help setup test cov e2e lint fmt typecheck run daemon build qemu-install clean
