PYTHON ?= python3
IMAGE  ?= aso-keyword-finder
TAG    ?= latest
PORT   ?= 8000

.DEFAULT_GOAL := help
.PHONY: help install lint format typecheck test cov serve docker-build docker-run clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-14s %s\n", $$1, $$2}'

install: ## Install the package in editable mode with dev dependencies
	$(PYTHON) -m pip install -e ".[dev]"

lint: ## Run ruff lint and format check
	$(PYTHON) -m ruff check .
	$(PYTHON) -m ruff format --check .

format: ## Format code and apply safe lint fixes
	$(PYTHON) -m ruff format .
	$(PYTHON) -m ruff check --fix .

typecheck: ## Run mypy --strict
	$(PYTHON) -m mypy

test: ## Run the test suite
	$(PYTHON) -m pytest

cov: ## Run the test suite with coverage
	$(PYTHON) -m pytest --cov=keyfinder --cov-report=term-missing --cov-report=html

serve: ## Run the web UI / API with auto-reload
	keyfinder serve --reload --port $(PORT)

docker-build: ## Build the Docker image
	docker build -t $(IMAGE):$(TAG) .

docker-run: ## Run the Docker image on localhost:$(PORT) with a named data volume
	docker run --rm -it -p 127.0.0.1:$(PORT):8000 -v keyfinder-data:/data \
		$(if $(KEYFINDER_API_KEY),-e KEYFINDER_API_KEY) $(IMAGE):$(TAG)

clean: ## Remove build outputs, caches and coverage files
	rm -rf build dist *.egg-info src/*.egg-info .mypy_cache .pytest_cache .ruff_cache \
		.coverage .coverage.* coverage.xml htmlcov
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
