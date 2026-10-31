#!/usr/bin/env bash
# Восстанавливает базу стека из резервной копии: bash scripts/restore-db.sh backups/<файл>.sql.gz
# Текущие данные базы удаляются, бэкенд, worker и beat на это время останавливаются.
set -euo pipefail
cd "$(dirname "$0")/.."

FILE="${1:?укажи файл резервной копии}"
USER_="${POSTGRES_USER:-leaderhelp}"
DB_="${POSTGRES_DB:-leaderhelp}"

read -r -p "База $DB_ будет перезаписана из $FILE. Продолжить? [y/N] " ok
[ "$ok" = "y" ] || { echo "отменено"; exit 1; }

docker compose stop backend worker beat
docker compose exec -T db dropdb -U "$USER_" --if-exists "$DB_"
docker compose exec -T db createdb -U "$USER_" "$DB_"
gunzip -c "$FILE" | docker compose exec -T db psql -q -U "$USER_" -d "$DB_"
docker compose start backend worker beat
echo "база восстановлена из $FILE"
