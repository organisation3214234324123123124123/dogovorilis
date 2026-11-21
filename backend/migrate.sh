#!/bin/sh
set -e
# Одноразовый сервис migrate: миграции под владельцем таблиц (MIGRATION_DATABASE_URL).
# DEMO_DATA=1: демо-данные в пустую базу под ролью приложения (без DEV_MODE=1 команда откажется).
alembic upgrade head
if [ "${DEMO_DATA:-0}" = "1" ]; then
  python -m app.demo load --if-empty || echo "demo data skipped (exit $?)" >&2
fi
