#!/usr/bin/env bash
# Поднимает Postgres + Redis для тестов. Docker, если работает, иначе нативные пакеты.
set -u
cd "$(dirname "$0")/.."

wait_pg() {
  for _ in $(seq 1 30); do
    pg_isready -q -h localhost -p 5432 2>/dev/null && return 0
    sleep 1
  done
  return 1
}

if docker info >/dev/null 2>&1; then
  echo "test-db: docker"
  timeout 180 docker compose -f compose.yaml -f infra/docker-compose.test.yml up -d db redis || exit 1
  for _ in $(seq 1 30); do
    docker compose -f compose.yaml exec -T db pg_isready -U leaderhelp >/dev/null 2>&1 && break
    sleep 1
  done
  docker compose -f compose.yaml exec -T db \
    psql -U leaderhelp -d leaderhelp -tc "SELECT 1 FROM pg_database WHERE datname='leaderhelp_test'" | grep -q 1 \
    || docker compose -f compose.yaml exec -T db createdb -U leaderhelp leaderhelp_test
  exit 0
fi

echo "test-db: native"
SUDO=""; [ "$(id -u)" != 0 ] && SUDO="sudo"
if command -v pg_ctlcluster >/dev/null 2>&1; then
  $SUDO pg_ctlcluster 16 main start 2>/dev/null || true
fi
wait_pg || { echo "test-db: postgres не запустился"; exit 1; }
PSQL="$SUDO su postgres -c"
if [ -z "$SUDO" ]; then PSQL="su postgres -c"; fi
$PSQL "psql -tc \"SELECT 1 FROM pg_roles WHERE rolname='leaderhelp'\"" | grep -q 1 \
  || $PSQL "psql -c \"CREATE ROLE leaderhelp LOGIN SUPERUSER PASSWORD 'leaderhelp'\""
for db in leaderhelp leaderhelp_test; do
  $PSQL "psql -tc \"SELECT 1 FROM pg_database WHERE datname='$db'\"" | grep -q 1 \
    || $PSQL "createdb -O leaderhelp $db"
done
if command -v redis-server >/dev/null 2>&1; then
  redis-cli ping >/dev/null 2>&1 || redis-server --daemonize yes >/dev/null
fi
echo "test-db: ready"
