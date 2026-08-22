# Convenience targets. Everything here is a one-liner you could type instead.
.PHONY: help install up down logs migrate revision seed dev test lint fmt types check smoke reset

help:
	@grep -E '^[a-z-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

install: ## Install dependencies
	uv sync

up: ## Start Postgres and Redis
	docker compose up -d db redis

down: ## Stop the containers (data is kept)
	docker compose down

logs: ## Tail the api container
	docker compose logs -f api

migrate: ## Apply migrations
	uv run alembic upgrade head

revision: ## Generate a migration — make revision m="what changed"
	uv run alembic revision --autogenerate -m "$(m)"

seed: ## Create the superadmin from .env (idempotent)
	uv run python -m app.seed

dev: ## Run the API with reload on :8010
	uv run uvicorn app.main:app --reload --port 8010

test: ## Run the test suite
	uv run pytest -q

lint: ## Lint
	uv run ruff check .

fmt: ## Format
	uv run ruff format .

types: ## Typecheck (strict)
	uv run mypy app scripts

check: lint types test ## Everything CI runs

smoke: ## End-to-end check against a running server
	uv run python scripts/smoke.py

reset: ## Drop the database and rebuild it from scratch. Destroys all data.
	docker compose down -v
	docker compose up -d db redis
	@echo "waiting for postgres..."
	@until docker compose exec -T db pg_isready -U $${POSTGRES_USER:-intelliwealth} >/dev/null 2>&1; do sleep 1; done
	$(MAKE) migrate seed
