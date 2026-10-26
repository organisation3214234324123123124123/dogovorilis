#!/bin/sh
set -e
# Миграции запускает только контейнер API; worker и beat ставят RUN_MIGRATIONS=0.
# Демо-данные - только при DEMO_DATA=1 и только в пустую базу (локальный показ).
if [ "${RUN_MIGRATIONS:-1}" = "1" ]; then
  alembic upgrade head
  if [ "${DEMO_DATA:-0}" = "1" ]; then
    python -m app.demo load --if-empty || echo "demo data skipped (exit $?)" >&2
  fi
fi
exec "$@"
