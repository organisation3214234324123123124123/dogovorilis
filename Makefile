SHELL := /bin/bash
COMPOSE := docker compose
TO := bash $(CURDIR)/scripts/tmo.sh
TEST_DATABASE_URL ?= postgresql+psycopg://leaderhelp:leaderhelp@localhost:5432/leaderhelp_test
COV_MIN ?= 90
SET ?=
DEMO_SET := $(if $(SET),--set $(SET),)

.PHONY: env up down logs install lint fmt test test-db

env:
	@test -f .env || { cp .env.example .env; echo ".env создан из .env.example"; }

up:
	$(TO) 900 $(COMPOSE) up -d --build

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f --tail=100

install:
	cd backend && $(TO) 300 uv sync

lint:
	cd backend && $(TO) 120 uv run ruff check . && $(TO) 120 uv run ruff format --check .

fmt:
	cd backend && uv run ruff format . && uv run ruff check --fix .

test:
	cd backend && DATABASE_URL=$${DATABASE_URL:-$(TEST_DATABASE_URL)} CELERY_EAGER=1 $(TO) 900 \
		uv run pytest --cov=app --cov-report=term --cov-fail-under=$(COV_MIN)

test-db:
	$(TO) 240 ./scripts/test-db.sh