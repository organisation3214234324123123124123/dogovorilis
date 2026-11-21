#!/bin/sh
set -e
# Миграции и демо-данные запускает одноразовый сервис migrate (backend/migrate.sh).
exec "$@"
