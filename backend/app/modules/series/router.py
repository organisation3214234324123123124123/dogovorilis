import uuid

from fastapi import APIRouter

from app.core.db import SessionDep
from app.core.errors import ADMIN_403, errors
from app.core.security import AdminAccess, CurrentUser
from app.modules.series import service
from app.modules.series.schemas import (
    AgendaItemCreate,
    AgendaItemRead,
    AgendaItemUpdate,
    AnonymizeResult,
    MeetingDetail,
    MeetingRead,
    MeetingStart,
    MeetingStarted,
    MeetingUpdate,
    MemberCreate,
    MemberRead,
    ReviewRead,
    SendRequest,
    SendResult,
    SeriesCreate,
    SeriesDetail,
    SeriesRead,
    SeriesUpdate,
    SummaryText,
)

router = APIRouter()
_ARCHIVED = {409: "Серия в архиве (SERIES_ARCHIVED)"}


@router.get(
    "/series",
    tags=["series"],
    summary="Мои серии",
    description="Только серии текущего ведущего, вместе с архивными; сначала новые.",
    response_model=list[SeriesRead],
    responses=errors(401),
)
def list_series(db: SessionDep, user: CurrentUser) -> list[SeriesRead]:
    return service.list_series(db, user)


@router.post(
    "/series",
    tags=["series"],
    summary="Создать серию",
    description="Название, часовой пояс, длительность 15–240 минут и стартовый состав.",
    response_model=SeriesDetail,
    status_code=201,
    responses=errors(401, 422),
)
def create_series(body: SeriesCreate, db: SessionDep, user: CurrentUser) -> SeriesDetail:
    return service.create_series(db, user, body)


@router.get(
    "/series/{series_id}",
    tags=["series"],
    summary="Серия с составом",
    description="Серия вместе с участниками. Чужая серия - 404.",
    response_model=SeriesDetail,
    responses=errors(401, 404),
)
def get_series(series_id: uuid.UUID, db: SessionDep, user: CurrentUser) -> SeriesDetail:
    return service.get_series(db, user, series_id)


@router.patch(
    "/series/{series_id}",
    tags=["series"],
    summary="Изменить серию",
    description="Название, пояс и длительность; проверки те же, что при создании.",
    response_model=SeriesDetail,
    responses=errors(401, 404, 409, 422, notes=_ARCHIVED),
)
def update_series(
    series_id: uuid.UUID, body: SeriesUpdate, db: SessionDep, user: CurrentUser
) -> SeriesDetail:
    return service.update_series(db, user, series_id, body)


@router.post(
    "/series/{series_id}/archive",
    tags=["series"],
    summary="Архивировать серию",
    description="Серия остаётся для чтения, новые встречи в ней начать нельзя. Пока идёт встреча, "
    "архивировать нельзя. Повторный вызов - 200, дата архивации не меняется.",
    response_model=SeriesDetail,
    responses=errors(401, 404, 409, notes={409: "Идёт встреча (MEETING_ALREADY_LIVE)"}),
)
def archive_series(series_id: uuid.UUID, db: SessionDep, user: CurrentUser) -> SeriesDetail:
    return service.archive_series(db, user, series_id)


@router.get(
    "/series/{series_id}/members",
    tags=["series"],
    summary="Состав серии",
    description="Имя, e-mail и сторона каждого участника.",
    response_model=list[MemberRead],
    responses=errors(401, 404),
)
def list_members(series_id: uuid.UUID, db: SessionDep, user: CurrentUser) -> list[MemberRead]:
    return service.list_members(db, user, series_id)


@router.post(
    "/series/{series_id}/members",
    tags=["series"],
    summary="Добавить участника",
    description="Если у ведущего уже есть человек с таким e-mail, в серию попадает он, дубль не "
    "создаётся. Звать можно прямо из панели встречи: участник сразу добавляется в идущую встречу.",
    response_model=MemberRead,
    status_code=201,
    responses=errors(401, 404, 409, 422, notes=_ARCHIVED),
)
def add_member(
    series_id: uuid.UUID, body: MemberCreate, db: SessionDep, user: CurrentUser
) -> MemberRead:
    return service.add_member(db, user, series_id, body)


@router.delete(
    "/series/{series_id}/members/{person_id}",
    tags=["series"],
    summary="Исключить участника",
    description="Пункты участника не меняются, но новым исполнителем его назначить уже нельзя.",
    status_code=204,
    responses=errors(401, 404, 409, notes=_ARCHIVED),
)
def remove_member(
    series_id: uuid.UUID, person_id: uuid.UUID, db: SessionDep, user: CurrentUser
) -> None:
    service.remove_member(db, user, series_id, person_id)


@router.get(
    "/series/{series_id}/meetings",
    tags=["meetings"],
    summary="Встречи серии",
    description="Встречи серии, сначала новые.",
    response_model=list[MeetingRead],
    responses=errors(401, 404),
)
def list_meetings(series_id: uuid.UUID, db: SessionDep, user: CurrentUser) -> list[MeetingRead]:
    return service.list_meetings(db, user, series_id)


@router.post(
    "/series/{series_id}/meetings",
    tags=["meetings"],
    summary="Начать встречу",
    description="Встреча сразу создаётся в live, все участники серии отмечены присутствующими. "
    "В ответе - встреча и список для сверки.",
    response_model=MeetingStarted,
    status_code=201,
    responses=errors(
        401,
        404,
        409,
        notes={409: "Уже идёт встреча (MEETING_ALREADY_LIVE) или серия в архиве (SERIES_ARCHIVED)"},
    ),
)
def start_meeting(
    series_id: uuid.UUID, body: MeetingStart, db: SessionDep, user: CurrentUser
) -> MeetingStarted:
    return service.start_meeting(db, user, series_id, body)


@router.get(
    "/meetings/{meeting_id}",
    tags=["meetings"],
    summary="Панель встречи",
    description="Встреча, участники, повестка и пункты с открытыми пробелами. После обрыва "
    "WebSocket фронт перечитывает панель целиком.",
    response_model=MeetingDetail,
    responses=errors(401, 404),
)
def get_meeting(meeting_id: uuid.UUID, db: SessionDep, user: CurrentUser) -> MeetingDetail:
    return service.get_meeting(db, user, meeting_id)


@router.patch(
    "/meetings/{meeting_id}",
    tags=["meetings"],
    summary="Изменить встречу",
    description="Меняет плановый конец и отметки присутствия.",
    response_model=MeetingDetail,
    responses=errors(401, 404, 409, 422, notes=_ARCHIVED),
)
def update_meeting(
    meeting_id: uuid.UUID, body: MeetingUpdate, db: SessionDep, user: CurrentUser
) -> MeetingDetail:
    return service.update_meeting(db, user, meeting_id, body)


@router.post(
    "/meetings/{meeting_id}/closure",
    tags=["meetings"],
    summary="Открыть режим закрытия",
    description="Переводит встречу из live в closing: пункты с пробелами поднимаются наверх, темы "
    "без решений помечаются `uncovered`, в панель уходит closure.opened. За 5 минут до "
    "planned_end то же самое делает Celery Beat (`closure_trigger = timer`, FR-23).",
    response_model=MeetingDetail,
    responses=errors(
        401,
        404,
        409,
        notes={409: "Встреча не идёт (MEETING_NOT_LIVE) или уже разослана (ALREADY_SENT)"},
    ),
)
def open_closure(meeting_id: uuid.UUID, db: SessionDep, user: CurrentUser) -> MeetingDetail:
    return service.open_closure(db, user, meeting_id)


@router.post(
    "/meetings/{meeting_id}/send",
    tags=["meetings"],
    summary="Разослать итог",
    description="Переводит пункты из draft в pending/accepted, ставит в очередь письма SUMMARY и "
    "CONFIRM_REQUEST, выдаёт исполнителям ссылки. Повторное нажатие писем не добавляет (BR-5). "
    "Встречу в sent можно разослать снова: уйдут только новые draft-пункты, SUMMARY второй раз "
    "не отправляется (OQ-02).",
    response_model=SendResult,
    responses=errors(
        401,
        404,
        409,
        422,
        notes={422: "Остались пробелы, нужен confirm_open_gaps (GAPS_OPEN)"},
    ),
)
def send_summary(
    meeting_id: uuid.UUID, body: SendRequest, db: SessionDep, user: CurrentUser
) -> SendResult:
    return service.send_summary(db, user, meeting_id, body)


@router.post(
    "/meetings/{meeting_id}/summary-text",
    tags=["meetings"],
    summary="Текст итога для мессенджера",
    description="Доступен для встречи в closing или sent: решения, исполнители («не назначен»), "
    "сроки; без ссылок /p/ и токенов. Пишет событие summary_copied (items_total, был ли "
    "summary_sent).",
    response_model=SummaryText,
    responses=errors(401, 404, 409, notes={409: "Встреча ещё идёт, итога нет (SUMMARY_NOT_READY)"}),
)
def summary_text(meeting_id: uuid.UUID, db: SessionDep, user: CurrentUser) -> SummaryText:
    return service.summary_text(db, user, meeting_id)


@router.get(
    "/meetings/{meeting_id}/review",
    tags=["meetings"],
    summary="Сверка в начале встречи",
    description="Тот же список, что в ответе «Начать встречу»: без исполнителя, просроченные, "
    "disputed, pending старше 2 рабочих дней, сделанные с прошлой встречи.",
    response_model=ReviewRead,
    responses=errors(401, 404),
)
def get_review(meeting_id: uuid.UUID, db: SessionDep, user: CurrentUser) -> ReviewRead:
    return service.get_review(db, user, meeting_id)


@router.post(
    "/meetings/{meeting_id}/review/skip",
    tags=["meetings"],
    summary="Пропустить сверку",
    description="Пункты остаются открытыми и покажутся на следующей встрече; пишется событие "
    "review_skipped.",
    status_code=204,
    responses=errors(401, 404),
)
def skip_review(meeting_id: uuid.UUID, db: SessionDep, user: CurrentUser) -> None:
    service.skip_review(db, user, meeting_id)


@router.post(
    "/meetings/{meeting_id}/agenda",
    tags=["meetings"],
    summary="Добавить пункт повестки",
    description="Не больше 20 пунктов на встречу.",
    response_model=AgendaItemRead,
    status_code=201,
    responses=errors(401, 404, 409, 422, notes=_ARCHIVED),
)
def add_agenda_item(
    meeting_id: uuid.UUID, body: AgendaItemCreate, db: SessionDep, user: CurrentUser
) -> AgendaItemRead:
    return AgendaItemRead.model_validate(service.add_agenda_item(db, user, meeting_id, body))


@router.patch(
    "/agenda/{agenda_id}",
    tags=["meetings"],
    summary="Изменить пункт повестки",
    description="Меняет название, позицию или статус пункта повестки.",
    response_model=AgendaItemRead,
    responses=errors(401, 404, 409, 422, notes=_ARCHIVED),
)
def update_agenda_item(
    agenda_id: uuid.UUID, body: AgendaItemUpdate, db: SessionDep, user: CurrentUser
) -> AgendaItemRead:
    return AgendaItemRead.model_validate(service.update_agenda_item(db, user, agenda_id, body))


@router.post(
    "/admin/persons/{person_id}/anonymize",
    tags=["admin"],
    dependencies=[AdminAccess],
    summary="Обезличить участника",
    description="Обезличивание по 152-ФЗ: имя меняется на «Участник N», e-mail удаляется, "
    "комментарии чистятся, ссылки отзываются. Пункты и статистика остаются. Служебный "
    "доступ: `X-Admin-Token`.",
    response_model=AnonymizeResult,
    responses=errors(403, 404, notes=ADMIN_403),
)
def anonymize_person(person_id: uuid.UUID, db: SessionDep) -> AnonymizeResult:
    return service.anonymize_person(db, person_id)
