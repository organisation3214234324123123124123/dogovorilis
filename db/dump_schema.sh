#!/usr/bin/env bash
# Пересобирает db/schema.sql (DDL всей схемы) из миграций Alembic.
# Нужен только Docker: bash db/dump_schema.sh (из корня проекта).
# Схема строится во временной базе ddl_dump, рабочая база не трогается.
set -euo pipefail
cd "$(dirname "$0")/.."

DC="docker compose"
USER_="${POSTGRES_USER:-leaderhelp}"
PASS_="${POSTGRES_PASSWORD:-leaderhelp}"
TMP_DB=ddl_dump

$DC up -d --wait db >/dev/null
$DC exec -T db dropdb -U "$USER_" --if-exists "$TMP_DB"
$DC exec -T db createdb -U "$USER_" "$TMP_DB"

$DC run --rm --no-deps -T \
  -e RUN_MIGRATIONS=0 \
  -e DATABASE_URL="postgresql+psycopg://$USER_:$PASS_@db:5432/$TMP_DB" \
  backend alembic upgrade head

{
  echo "-- DDL схемы базы «Договорились» (PostgreSQL 16)."
  echo "-- Файл собран из миграций backend/migrations командой: bash db/dump_schema.sh"
  echo "-- Руками не править: сначала миграция, потом пересборка файла."
  echo
  $DC exec -T db pg_dump -U "$USER_" -d "$TMP_DB" \
    --schema-only --no-owner --no-privileges --no-comments \
    --exclude-table=public.alembic_version \
    | grep -v -E '^\\(un)?restrict ' \
    | grep -v -E '^-- Dumped (from|by) '
} > db/schema.sql

$DC exec -T db dropdb -U "$USER_" "$TMP_DB"
echo "db/schema.sql: $(grep -c '^CREATE TABLE' db/schema.sql) таблиц"
