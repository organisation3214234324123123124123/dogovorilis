#!/bin/sh
set -e
# Миграции запускает только контейнер API; worker и beat ставят RUN_MIGRATIONS=0.
# DEMO_DATA=1: демо-данные в пустую базу (без DEV_MODE=1 команда откажется).
if [ "${RUN_MIGRATIONS:-1}" = "1" ]; then
  alembic upgrade head
  if [ "${DEMO_DATA:-0}" = "1" ]; then
    python -m app.demo load --if-empty || echo "demo data skipped (exit $?)" >&2
  fi
fi
exec "$@"
