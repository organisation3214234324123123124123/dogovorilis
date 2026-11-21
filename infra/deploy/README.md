# Выкладка на сервер

Сервис запускается только с `compose.yaml` (без `compose.override.yaml`), в боевом режиме.

## Сервисы и права

| Сервис | Подключение к БД | Назначение |
|---|---|---|
| `migrate` | `MIGRATION_DATABASE_URL` (владелец таблиц), `APP_DB_PASSWORD`, `CLEANUP_DB_PASSWORD` | Одноразово: миграции и создание ролей БД. Завершается до старта остальных |
| `backend` | `DATABASE_URL` (роль `leaderhelp_app`) | API |
| `worker` | `DATABASE_URL` (`leaderhelp_app`), очередь `celery` | Рассылки, фоновые задачи |
| `beat` | `DATABASE_URL` (`leaderhelp_app`) | Расписание |
| `worker-maintenance` | `DATABASE_URL` + `CLEANUP_DATABASE_URL` (роль `leaderhelp_cleanup`), очередь `maintenance` | Только `cleanup_retention` (03:00 UTC): удаляет старые `item_events` и `llm_calls` |

В `x-backend-env` четыре секрета (`MIGRATION_DATABASE_URL`, `CLEANUP_DATABASE_URL`, `APP_DB_PASSWORD`, `CLEANUP_DB_PASSWORD`) обнулены, поэтому значения из `.env` не попадают в `backend`, `worker` и `beat`. Свои значения получают только `migrate` и `worker-maintenance`.

## Переменные окружения

В `.env` на сервере:

- `SECRET_KEY` - 32+ символа;
- `POSTGRES_PASSWORD` - пароль владельца таблиц (используется только `migrate`);
- `APP_DB_PASSWORD`, `CLEANUP_DB_PASSWORD` - пароли ролей, не короче 16 символов и не значения по умолчанию, иначе процесс не стартует;
- `DEV_MODE` и `DEMO_DATA` пустые.

Значения по умолчанию (`leaderhelp_app`, `leaderhelp_cleanup`) годятся только для локального запуска.

## Порядок выкладки

1. Задать пароли ролей в `.env`.
2. `docker compose -f compose.yaml pull && docker compose -f compose.yaml build`.
3. `docker compose -f compose.yaml up -d`. Compose сам запустит `migrate`, дождётся его успешного завершения и только затем поднимет `backend`, `worker`, `beat`, `worker-maintenance`.
4. Проверить: `docker compose -f compose.yaml ps -a` - `migrate` в статусе `Exited (0)`, остальные `running`/`healthy`.
5. Смена пароля роли: изменить значение в `.env` и перезапустить стек (`up -d`): `migrate` применит его идемпотентно.

## Проверка на стенде

```bash
# миграции завершились до старта backend
docker compose -f compose.yaml ps -a --format '{{.Service}} {{.Status}}'
# секретов нет в backend, worker и beat
for s in backend worker beat; do
  docker compose -f compose.yaml exec -T $s env | grep -E '^(MIGRATION_DATABASE_URL|CLEANUP_DATABASE_URL|APP_DB_PASSWORD|CLEANUP_DB_PASSWORD)=.+' && echo "ЕСТЬ секрет в $s"
done
# CLEANUP_DATABASE_URL есть только у worker-maintenance
docker compose -f compose.yaml exec -T worker-maintenance env | grep -c '^CLEANUP_DATABASE_URL=.\+'
# очередь maintenance слушает только worker-maintenance
docker compose -f compose.yaml exec -T worker-maintenance celery -A app.worker inspect active_queues
# приложение не может менять журнал (ожидается 42501)
docker compose -f compose.yaml exec -T db psql -U leaderhelp_app -d leaderhelp -c "DELETE FROM item_events"
```

## Откат

Миграции необратимо ничего не удаляют. При проблеме остановить стек (`docker compose -f compose.yaml down`), вернуть прежний образ (`APP_VERSION`) и запустить снова.
