#!/usr/bin/env bash
# Резервная копия базы из запущенного стека: bash scripts/backup-db.sh [файл]
# По умолчанию пишет в backups/<дата-время>.sql.gz.
set -euo pipefail
cd "$(dirname "$0")/.."

USER_="${POSTGRES_USER:-leaderhelp}"
DB_="${POSTGRES_DB:-leaderhelp}"
OUT="${1:-backups/$(date +%Y%m%d-%H%M%S).sql.gz}"

mkdir -p "$(dirname "$OUT")"
docker compose exec -T db pg_dump -U "$USER_" -d "$DB_" --no-owner --no-privileges | gzip > "$OUT"
echo "резервная копия: $OUT ($(du -h "$OUT" | cut -f1))"
