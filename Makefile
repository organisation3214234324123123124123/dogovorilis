SHELL := /bin/bash
COMPOSE := docker compose
TO := bash $(CURDIR)/scripts/tmo.sh
TEST_DATABASE_URL ?= postgresql+psycopg://leaderhelp:leaderhelp@localhost:5432/leaderhelp_test
COV_MIN ?= 90
SET ?=
DEMO_SET := $(if $(SET),--set $(SET),)

.PHONY: up down logs install lint fmt test check openapi migration migrations-check schema schema-check \
	demo demo-reset test-db smoke env

env:
	@test -f .env || { cp .env.example .env; echo ".env создан из .env.example"; }

up:
	$(TO) 900 $(COMPOSE) up -d --build

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f --tail=100

.PHONY: ps restart
ps:
	$(COMPOSE) ps -a

# make restart s=worker - перезапустить один сервис, без s - все
restart:
	$(COMPOSE) restart $(s)

.PHONY: db-backup
db-backup:
	bash scripts/backup-db.sh

.PHONY: db-restore
# make db-restore f=backups/<файл>.sql.gz
db-restore:
	bash scripts/restore-db.sh $(f)

.PHONY: psql shell
# psql в базе стека
psql:
	$(COMPOSE) exec db psql -U $${POSTGRES_USER:-leaderhelp} -d $${POSTGRES_DB:-leaderhelp}

# shell в контейнере бэкенда
shell:
	$(COMPOSE) exec backend bash

install:
	cd backend && $(TO) 300 uv sync

lint:
	cd backend && $(TO) 120 uv run ruff check . && $(TO) 120 uv run ruff format --check .

fmt:
	cd backend && uv run ruff format . && uv run ruff check --fix .

test:
	cd backend && DATABASE_URL=$${DATABASE_URL:-$(TEST_DATABASE_URL)} CELERY_EAGER=1 $(TO) 900 \
		uv run pytest --cov=app --cov-report=term --cov-fail-under=$(COV_MIN)

openapi:
	cd backend && $(TO) 120 uv run python scripts/export_openapi.py

migrations-check:
	@cd backend && n=$$($(TO) 120 uv run alembic heads | grep -c '(head)'); \
	if [ "$$n" != "1" ]; then echo "alembic heads = $$n, ожидалась 1"; exit 1; fi; echo "alembic: 1 head"

migration:
	@test -n "$(m)" || { echo 'usage: make migration m="message"'; exit 1; }
	cd backend && DEV_MODE=1 uv run alembic revision --autogenerate -m "$(m)"

schema:
	$(TO) 600 bash db/dump_schema.sh

schema-check: schema
	git diff --exit-code -- db/schema.sql

check: lint test migrations-check
	$(MAKE) --no-print-directory openapi
	git diff --exit-code -- docs/api/openapi.yaml

demo:
	cd backend && DEV_MODE=1 $(TO) 120 uv run python -m app.demo load $(DEMO_SET)

demo-reset:
	cd backend && DEV_MODE=1 $(TO) 120 uv run python -m app.demo reset $(DEMO_SET)

test-db:
	$(TO) 240 ./scripts/test-db.sh

smoke:
	bash tests/smoke/smoke.sh http://localhost:$${BACKEND_PORT:-8000}
