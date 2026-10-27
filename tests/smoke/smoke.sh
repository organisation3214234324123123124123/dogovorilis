#!/usr/bin/env bash
# Сквозная проверка поднятого бэкенда (docker compose up): curl + jq; для проверок в БД и вызовов кода — `docker compose exec` (psql, python), см. SMOKE_PSQL и SMOKE_PY.
#   bash tests/smoke/smoke.sh http://localhost:8000
# Выход 0 - всё прошло; иначе 1 и строка FAIL с шагом и ответом.
set -u

BASE="${1:-http://localhost:8000}"
BASE="${BASE%/}"
API="$BASE/api/v1"

command -v jq >/dev/null || { echo "нужен jq"; exit 2; }
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
JAR="$TMP/cookies.txt"
BODY="$TMP/body"
STEP=0

fail() {
  echo "FAIL [шаг $STEP] $1" >&2
  [ -s "$BODY" ] && { echo "--- ответ:" >&2; head -c 1500 "$BODY" >&2; echo >&2; }
  exit 1
}
ok() { echo "ok   [$STEP] $1"; }

# req METHOD URL [JSON] [заголовок...]: тело в $BODY, код в $CODE, куки сохраняются
req() {
  local method="$1" url="$2" data="${3:-}"
  shift 3 2>/dev/null || shift $#
  local args=(-sS -m 20 -b "$JAR" -c "$JAR" -o "$BODY" -w '%{http_code}' -X "$method")
  [ -n "$data" ] && args+=(-H 'Content-Type: application/json' -d "$data")
  local h
  for h in "$@"; do args+=(-H "$h"); done
  : >"$BODY"
  CODE="$(curl "${args[@]}" "$url")" || fail "curl не достучался до $url"
}
expect() { # expect ШАГ КОД [jq-условие]
  STEP=$((STEP + 1))
  [ "$CODE" = "$2" ] || fail "$1: ожидали HTTP $2, получили $CODE"
  if [ -n "${3:-}" ]; then
    jq -e "$3" "$BODY" >/dev/null 2>&1 || fail "$1: в ответе не выполнено $3"
  fi
  ok "$1"
}
field() { jq -r "$1" "$BODY"; }

STAMP="$(date +%s)$RANDOM"
EMAIL="smoke-$STAMP@example.com"
PASS="smoke-pass-$STAMP"

# --- здоровье ---
req GET "$API/health" ""
expect "health" 200 '.status == "ok"'

# --- ведущий: регистрация и вход ---
req POST "$API/auth/register" \
  "{\"email\":\"$EMAIL\",\"password\":\"$PASS\",\"full_name\":\"Смоук Тест\",\"pd_consent\":true}"
expect "регистрация" 201 ".email == \"$EMAIL\""

req POST "$API/auth/register" \
  "{\"email\":\"$EMAIL\",\"password\":\"$PASS\",\"full_name\":\"Смоук Тест\",\"pd_consent\":true}"
expect "повторная регистрация -> 409" 409 '.code == "EMAIL_TAKEN"'

req GET "$API/auth/me" ""
expect "без входа /auth/me -> 401" 401 '.code == "UNAUTHORIZED"'

req POST "$API/auth/login" "{\"email\":\"$EMAIL\",\"password\":\"$PASS\"}"
expect "вход" 200
grep -q 'access_token' "$JAR" || fail "вход не поставил cookie access_token"

req GET "$API/auth/me" ""
expect "/auth/me с cookie" 200 ".email == \"$EMAIL\""

# --- серия и участники ---
req POST "$API/series" \
  '{"title":"Смоук-серия","planned_duration_min":30,"members":[{"full_name":"Иван Петров","email":"ivan-smoke@example.com","organization_name":"Команда"}]}'
expect "создание серии" 201 '.id != null'
SERIES_ID="$(field .id)"

req POST "$API/series/$SERIES_ID/members" \
  '{"full_name":"Анна Смирнова","email":"anna-smoke@example.com","organization_name":"Команда"}'
expect "добавление участника" 201

req GET "$API/series/$SERIES_ID/members" ""
expect "состав серии" 200 'length == 2'
IVAN_ID="$(jq -r '.[] | select(.full_name == "Иван Петров") | .person_id' "$BODY")"
[ -n "$IVAN_ID" ] || fail "в составе серии нет Ивана Петрова"

# --- встреча и пункт ---
req POST "$API/series/$SERIES_ID/meetings" '{}'
expect "старт встречи" 201 '.meeting.status == "live"'
MEETING_ID="$(field .meeting.id)"

req POST "$API/meetings/$MEETING_ID/items" '{"raw_text":"Ивану подготовить отчёт к пятнице"}'
expect "пункт строкой: разобран исполнитель и срок" 201 \
  ".assignee.id == \"$IVAN_ID\" and .due_date != null and .status == \"draft\" and .version == 1"
ITEM_ID="$(field .id)"
VERSION="$(field .version)"

req PATCH "$API/items/$ITEM_ID" '{"done_criteria":"Отчёт лежит в общей папке"}'
expect "правка без If-Match -> 428" 428 '.code == "PRECONDITION_REQUIRED"'

req PATCH "$API/items/$ITEM_ID" '{"done_criteria":"Отчёт лежит в общей папке"}' "If-Match: $VERSION"
expect "правка пункта с If-Match" 200 \
  '.done_criteria == "Отчёт лежит в общей папке" and .version == 2'

req PATCH "$API/items/$ITEM_ID" '{"done_criteria":"Другое"}' "If-Match: $VERSION"
expect "правка с устаревшей версией -> 409" 409 '.code == "VERSION_CONFLICT"'

req GET "$API/meetings/$MEETING_ID" ""
expect "GET /meetings/{id}" 200 \
  ".id == \"$MEETING_ID\" and ([.. | objects | select(.id? == \"$ITEM_ID\")] | length) > 0"

# --- закрытие, рассылка, ответ исполнителя по ссылке ---
# Письма при пустом SMTP_HOST только пишутся в лог (токен там замаскирован), поэтому выпускаем
# свежий токен ссылки тем же кодом, что и отправитель письма: id ссылки из БД (access_links) +
# confirm.service.issue_token (в БД пишется только хеш). Команды можно переопределить
# для локального запуска без docker: SMOKE_PSQL (psql -tA ...) и SMOKE_PY (python с backend в PYTHONPATH).
PSQL_CMD="${SMOKE_PSQL:-docker compose exec -T db psql -U leaderhelp -d leaderhelp -tA}"
PY_CMD="${SMOKE_PY:-docker compose exec -T backend python}"

req POST "$API/meetings/$MEETING_ID/closure" ""
expect "закрытие встречи" 200

req POST "$API/meetings/$MEETING_ID/send" '{"confirm_open_gaps":true}'
expect "рассылка итога" 200 '.items_sent >= 1 and .meeting.status == "sent"'

req POST "$API/meetings/$MEETING_ID/summary-text" ""
expect "текст итога для мессенджера" 200 '(.text | length) > 0 and .items_total >= 1'

STEP=$((STEP + 1))
LINK_ID="$($PSQL_CMD -c "select id from access_links where meeting_id = '$MEETING_ID' and person_id = '$IVAN_ID' and revoked_at is null limit 1" | tr -d '[:space:]')"
[ -n "$LINK_ID" ] || fail "после рассылки в access_links нет ссылки Ивана (встреча $MEETING_ID)"
TOKEN_PY='import sys, uuid
import app.models
from app.core.db import SessionLocal, get_engine
from app.modules.confirm.service import issue_token
with SessionLocal(bind=get_engine()) as db:
    token = issue_token(db, uuid.UUID(sys.argv[1]))
    db.commit()
print(token)
'
TOKEN="$(printf '%s' "$TOKEN_PY" | $PY_CMD - "$LINK_ID" | tr -d '[:space:]')"
[ -n "$TOKEN" ] || fail "не удалось выпустить токен ссылки $LINK_ID"
ok "токен ссылки исполнителя получен"

PJAR="$JAR"
JAR="$TMP/exec-cookies.txt" # исполнитель - отдельный клиент без сессии ведущего
req GET "$API/p/$TOKEN" ""
expect "страница по ссылке отдаёт пункт" 200 ".csrf_token != null and ([.items[] | select(.id == \"$ITEM_ID\")] | length) == 1"
CSRF="$(field .csrf_token)"
grep -q 'csrf_token' "$JAR" || fail "GET /p/{token} не поставил cookie csrf_token"

req POST "$API/p/$TOKEN/items/$ITEM_ID/accept" ""
expect "ответ без CSRF -> 403" 403 '.code == "CSRF_INVALID"'

req POST "$API/p/$TOKEN/items/$ITEM_ID/accept" "" "X-CSRF-Token: $CSRF"
expect "исполнитель принимает пункт (с CSRF)" 200 '.status == "accepted"'
JAR="$PJAR"

req GET "$API/items/$ITEM_ID" ""
expect "у ведущего статус пункта сменился" 200 '.status == "accepted"'

# --- 5. Демо-пользователь (entrypoint backend грузит демо-данные при DEMO_DATA=1)
DEMO_JAR_BACKUP="$JAR"
JAR="$TMP/demo-cookies.txt"
req POST "$API/auth/login" '{"email":"host@example.com","password":"demo1234"}'
expect "вход демо-пользователем host@example.com" 200
req GET "$API/series" ""
expect "у демо-пользователя есть серии" 200 'length >= 1'
JAR="$DEMO_JAR_BACKUP"

# --- Swagger и контракт ---
req GET "$BASE/api/docs" ""
expect "Swagger /api/docs" 200
grep -qi 'swagger' "$BODY" || fail "/api/docs не похож на Swagger UI"

req GET "$BASE/api/openapi.json" ""
expect "openapi.json" 200 '.paths | length > 10'

echo "PASS"
