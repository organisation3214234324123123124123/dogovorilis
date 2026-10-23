#!/bin/sh
set -e
# Миграции запускает только контейнер API; worker и beat ставят RUN_MIGRATIONS=0.
if [ "${RUN_MIGRATIONS:-1}" = "1" ]; then
  alembic upgrade head
fi
exec "$@"
