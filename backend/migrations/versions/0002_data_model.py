"""Создаёт модель данных: 17 таблиц, без таблиц интеграции с трекером.

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None

ENUMS = {
    'llm_task': postgresql.ENUM('parse', 'gaps', 'question', name='llm_task', create_type=False),
    'capture_mode': postgresql.ENUM('manual', 'audio', name='capture_mode', create_type=False),
    'series_status': postgresql.ENUM('active', 'archived', name='series_status', create_type=False),
    'meeting_status': postgresql.ENUM('planned', 'live', 'closing', 'sent', name='meeting_status', create_type=False),
    'closure_trigger': postgresql.ENUM('button', 'timer', name='closure_trigger', create_type=False),
    'agenda_item_status': postgresql.ENUM('open', 'covered', 'moved', name='agenda_item_status', create_type=False),
    'llm_outcome': postgresql.ENUM('ok', 'timeout', 'invalid_output', name='llm_outcome', create_type=False),
    'item_source': postgresql.ENUM('manual', 'asr', name='item_source', create_type=False),
    'item_status': postgresql.ENUM('draft', 'pending', 'accepted', 'disputed', 'done', 'cancelled', name='item_status', create_type=False),
    'item_done_by': postgresql.ENUM('assignee', 'host', 'tracker', name='item_done_by', create_type=False),
    'gap_type': postgresql.ENUM('no_assignee', 'no_due', 'due_vague', 'due_past', 'vague_action', name='gap_type', create_type=False),
    'gap_source': postgresql.ENUM('rule', 'llm', name='gap_source', create_type=False),
    'item_comment_kind': postgresql.ENUM('not_mine', 'reschedule', name='item_comment_kind', create_type=False),
    'item_actor_type': postgresql.ENUM('host', 'assignee', 'system', 'tracker', name='item_actor_type', create_type=False),
    'item_action': postgresql.ENUM('created', 'updated', 'accepted_in_meeting', 'sent', 'accepted', 'rescheduled', 'disputed', 'reassigned', 'done', 'cancelled', 'reopened', name='item_action', create_type=False),
    'notification_type': postgresql.ENUM('SUMMARY', 'CONFIRM_REQUEST', 'CONFIRM_REPEAT', 'HOST_RESPONSE', 'DUE_TOMORROW', 'ITEM_CHANGED', 'ITEM_REASSIGNED', name='notification_type', create_type=False),
    'notification_status': postgresql.ENUM('queued', 'sent', 'failed', 'cancelled', name='notification_status', create_type=False),
}

# item_events только пополняется (BR-19): UPDATE запрещён, DELETE остаётся
# для ON DELETE CASCADE и очистки по BR-20.
ITEM_EVENTS_NO_UPDATE = """
CREATE FUNCTION item_events_no_update() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'item_events is append-only';
END;
$$;
CREATE TRIGGER item_events_no_update BEFORE UPDATE ON item_events
    FOR EACH ROW EXECUTE FUNCTION item_events_no_update();
"""


def upgrade() -> None:
    op.execute('CREATE EXTENSION IF NOT EXISTS citext')
    bind = op.get_bind()
    for enum in ENUMS.values():
        enum.create(bind, checkfirst=False)
    op.create_table('users',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('email', postgresql.CITEXT(), nullable=False),
    sa.Column('password_hash', sa.String(length=255), nullable=False),
    sa.Column('full_name', sa.String(length=200), nullable=False),
    sa.Column('timezone', sa.String(length=64), server_default='Europe/Moscow', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_users')),
    sa.UniqueConstraint('email', name=op.f('uq_users_email'))
    )
    op.create_table('llm_prompts',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('task', ENUMS['llm_task'], nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('text', sa.Text(), nullable=False),
    sa.Column('is_active', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('created_by', sa.Uuid(), nullable=True),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_llm_prompts_created_by_users'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_llm_prompts')),
    sa.UniqueConstraint('task', 'version', name=op.f('uq_llm_prompts_task_version'))
    )
    op.create_index('uq_llm_prompts_task_active', 'llm_prompts', ['task'], unique=True, postgresql_where=sa.text('is_active'))
    op.create_table('organizations',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('owner_user_id', sa.Uuid(), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('is_own', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['owner_user_id'], ['users.id'], name=op.f('fk_organizations_owner_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_organizations'))
    )
    op.create_index('uq_organizations_owner_user_id_lower_name', 'organizations', ['owner_user_id', sa.literal_column('lower(name)')], unique=True)
    op.create_index('uq_organizations_owner_user_id_own', 'organizations', ['owner_user_id'], unique=True, postgresql_where=sa.text('is_own'))
    op.create_table('series',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('owner_user_id', sa.Uuid(), nullable=False),
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('timezone', sa.String(length=64), nullable=False),
    sa.Column('planned_duration_min', sa.SmallInteger(), nullable=False),
    sa.Column('capture_mode', ENUMS['capture_mode'], server_default='manual', nullable=False),
    sa.Column('status', ENUMS['series_status'], server_default='active', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('planned_duration_min BETWEEN 15 AND 240', name=op.f('ck_series_planned_duration_min')),
    sa.ForeignKeyConstraint(['owner_user_id'], ['users.id'], name=op.f('fk_series_owner_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_series'))
    )
    op.create_index('ix_series_owner_user_id_status', 'series', ['owner_user_id', 'status'], unique=False)
    op.create_table('meetings',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('series_id', sa.Uuid(), nullable=False),
    sa.Column('planned_start', sa.DateTime(timezone=True), nullable=False),
    sa.Column('planned_end', sa.DateTime(timezone=True), nullable=False),
    sa.Column('status', ENUMS['meeting_status'], server_default='planned', nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('closure_opened_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('closure_trigger', ENUMS['closure_trigger'], nullable=True),
    sa.Column('summary_sent_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('planned_end > planned_start', name=op.f('ck_meetings_planned_period')),
    sa.ForeignKeyConstraint(['series_id'], ['series.id'], name=op.f('fk_meetings_series_id_series'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_meetings'))
    )
    op.create_index('ix_meetings_series_id_planned_start', 'meetings', ['series_id', sa.literal_column('planned_start DESC')], unique=False)
    op.create_index('uq_meetings_series_id_active', 'meetings', ['series_id'], unique=True, postgresql_where=sa.text("status IN ('live', 'closing')"))
    op.create_table('persons',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('owner_user_id', sa.Uuid(), nullable=False),
    sa.Column('organization_id', sa.Uuid(), nullable=False),
    sa.Column('full_name', sa.String(length=200), nullable=False),
    sa.Column('email', postgresql.CITEXT(), nullable=True),
    sa.Column('anonymized_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], name=op.f('fk_persons_organization_id_organizations'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['owner_user_id'], ['users.id'], name=op.f('fk_persons_owner_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_persons')),
    sa.UniqueConstraint('owner_user_id', 'email', name=op.f('uq_persons_owner_user_id_email'))
    )
    op.create_index(op.f('ix_persons_organization_id'), 'persons', ['organization_id'], unique=False)
    op.create_table('access_links',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('token_hash', sa.LargeBinary(length=32), nullable=False),
    sa.Column('person_id', sa.Uuid(), nullable=False),
    sa.Column('meeting_id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint('octet_length(token_hash) = 32', name=op.f('ck_access_links_token_hash_len')),
    sa.ForeignKeyConstraint(['meeting_id'], ['meetings.id'], name=op.f('fk_access_links_meeting_id_meetings'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['person_id'], ['persons.id'], name=op.f('fk_access_links_person_id_persons'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_access_links')),
    sa.UniqueConstraint('token_hash', name=op.f('uq_access_links_token_hash'))
    )
    op.create_index('ix_access_links_expires_at', 'access_links', ['expires_at'], unique=False)
    op.create_index(op.f('ix_access_links_meeting_id'), 'access_links', ['meeting_id'], unique=False)
    op.create_index('uq_access_links_person_id_meeting_id_active', 'access_links', ['person_id', 'meeting_id'], unique=True, postgresql_where=sa.text('revoked_at IS NULL'))
    op.create_table('agenda_items',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('meeting_id', sa.Uuid(), nullable=False),
    sa.Column('position', sa.SmallInteger(), nullable=False),
    sa.Column('title', sa.String(length=300), nullable=False),
    sa.Column('status', ENUMS['agenda_item_status'], server_default='open', nullable=False),
    sa.ForeignKeyConstraint(['meeting_id'], ['meetings.id'], name=op.f('fk_agenda_items_meeting_id_meetings'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_agenda_items')),
    sa.UniqueConstraint('meeting_id', 'position', name=op.f('uq_agenda_items_meeting_id_position'))
    )
    op.create_table('llm_calls',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('meeting_id', sa.Uuid(), nullable=True),
    sa.Column('task', ENUMS['llm_task'], nullable=False),
    sa.Column('provider', sa.String(length=100), nullable=False),
    sa.Column('model', sa.String(length=100), nullable=False),
    sa.Column('prompt_id', sa.Uuid(), nullable=True),
    sa.Column('latency_ms', sa.Integer(), nullable=True),
    sa.Column('input_tokens', sa.Integer(), nullable=True),
    sa.Column('output_tokens', sa.Integer(), nullable=True),
    sa.Column('outcome', ENUMS['llm_outcome'], nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['meeting_id'], ['meetings.id'], name=op.f('fk_llm_calls_meeting_id_meetings'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['prompt_id'], ['llm_prompts.id'], name=op.f('fk_llm_calls_prompt_id_llm_prompts'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_llm_calls'))
    )
    op.create_index('ix_llm_calls_created_at', 'llm_calls', ['created_at'], unique=False)
    op.create_index(op.f('ix_llm_calls_meeting_id'), 'llm_calls', ['meeting_id'], unique=False)
    op.create_table('meeting_participants',
    sa.Column('meeting_id', sa.Uuid(), nullable=False),
    sa.Column('person_id', sa.Uuid(), nullable=False),
    sa.Column('present', sa.Boolean(), server_default='true', nullable=False),
    sa.ForeignKeyConstraint(['meeting_id'], ['meetings.id'], name=op.f('fk_meeting_participants_meeting_id_meetings'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['person_id'], ['persons.id'], name=op.f('fk_meeting_participants_person_id_persons'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('meeting_id', 'person_id', name=op.f('pk_meeting_participants'))
    )
    op.create_index(op.f('ix_meeting_participants_person_id'), 'meeting_participants', ['person_id'], unique=False)
    op.create_table('series_members',
    sa.Column('series_id', sa.Uuid(), nullable=False),
    sa.Column('person_id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['person_id'], ['persons.id'], name=op.f('fk_series_members_person_id_persons'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['series_id'], ['series.id'], name=op.f('fk_series_members_series_id_series'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('series_id', 'person_id', name=op.f('pk_series_members'))
    )
    op.create_index(op.f('ix_series_members_person_id'), 'series_members', ['person_id'], unique=False)
    op.create_table('items',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('meeting_id', sa.Uuid(), nullable=False),
    sa.Column('series_id', sa.Uuid(), nullable=False),
    sa.Column('agenda_item_id', sa.Uuid(), nullable=True),
    sa.Column('raw_text', sa.String(length=1000), nullable=False),
    sa.Column('what', sa.String(length=500), nullable=True),
    sa.Column('assignee_person_id', sa.Uuid(), nullable=True),
    sa.Column('due_date', sa.Date(), nullable=True),
    sa.Column('due_text', sa.String(length=100), nullable=True),
    sa.Column('done_criteria', sa.String(length=500), nullable=True),
    sa.Column('manual_fields', postgresql.ARRAY(sa.String()), server_default='{}', nullable=False),
    sa.Column('source', ENUMS['item_source'], server_default='manual', nullable=False),
    sa.Column('suggested', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('status', ENUMS['item_status'], server_default='draft', nullable=False),
    sa.Column('accepted_in_meeting', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('accepted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('done_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('done_by', ENUMS['item_done_by'], nullable=True),
    sa.Column('cancelled_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('reschedule_count', sa.Integer(), server_default='0', nullable=False),
    sa.Column('version', sa.Integer(), server_default='1', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(status = 'cancelled') = (cancelled_at IS NOT NULL)", name=op.f('ck_items_cancelled_at')),
    sa.CheckConstraint("(status = 'done') = (done_at IS NOT NULL)", name=op.f('ck_items_done_at')),
    sa.CheckConstraint('reschedule_count >= 0', name=op.f('ck_items_reschedule_count')),
    sa.CheckConstraint('version >= 1', name=op.f('ck_items_version')),
    sa.ForeignKeyConstraint(['agenda_item_id'], ['agenda_items.id'], name=op.f('fk_items_agenda_item_id_agenda_items'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['assignee_person_id'], ['persons.id'], name=op.f('fk_items_assignee_person_id_persons'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['meeting_id'], ['meetings.id'], name=op.f('fk_items_meeting_id_meetings'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['series_id'], ['series.id'], name=op.f('fk_items_series_id_series'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_items'))
    )
    op.create_index(op.f('ix_items_assignee_person_id'), 'items', ['assignee_person_id'], unique=False)
    op.create_index('ix_items_due_date_open', 'items', ['due_date'], unique=False, postgresql_where=sa.text("status IN ('pending', 'accepted')"))
    op.create_index(op.f('ix_items_meeting_id'), 'items', ['meeting_id'], unique=False)
    op.create_index('ix_items_series_id_status', 'items', ['series_id', 'status'], unique=False)
    op.create_table('analytics_events',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('name', sa.String(length=64), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=True),
    sa.Column('series_id', sa.Uuid(), nullable=True),
    sa.Column('meeting_id', sa.Uuid(), nullable=True),
    sa.Column('item_id', sa.Uuid(), nullable=True),
    sa.Column('props', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['item_id'], ['items.id'], name=op.f('fk_analytics_events_item_id_items'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['meeting_id'], ['meetings.id'], name=op.f('fk_analytics_events_meeting_id_meetings'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['series_id'], ['series.id'], name=op.f('fk_analytics_events_series_id_series'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_analytics_events_user_id_users'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_analytics_events'))
    )
    op.create_index('ix_analytics_events_name_created_at', 'analytics_events', ['name', 'created_at'], unique=False)
    op.create_table('gaps',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('item_id', sa.Uuid(), nullable=False),
    sa.Column('type', ENUMS['gap_type'], nullable=False),
    sa.Column('source', ENUMS['gap_source'], server_default='rule', nullable=False),
    sa.Column('question', sa.String(length=300), nullable=True),
    sa.Column('detected_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('dismissed_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['item_id'], ['items.id'], name=op.f('fk_gaps_item_id_items'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_gaps'))
    )
    op.create_index('uq_gaps_item_id_type_open', 'gaps', ['item_id', 'type'], unique=True, postgresql_where=sa.text('resolved_at IS NULL AND dismissed_at IS NULL'))
    op.create_table('item_comments',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('item_id', sa.Uuid(), nullable=False),
    sa.Column('author_person_id', sa.Uuid(), nullable=False),
    sa.Column('kind', ENUMS['item_comment_kind'], nullable=False),
    sa.Column('text', sa.String(length=500), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('anonymized_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['author_person_id'], ['persons.id'], name=op.f('fk_item_comments_author_person_id_persons'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['item_id'], ['items.id'], name=op.f('fk_item_comments_item_id_items'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_item_comments'))
    )
    op.create_index(op.f('ix_item_comments_item_id'), 'item_comments', ['item_id'], unique=False)
    op.create_table('item_events',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('item_id', sa.Uuid(), nullable=False),
    sa.Column('actor_type', ENUMS['item_actor_type'], nullable=False),
    sa.Column('actor_user_id', sa.Uuid(), nullable=True),
    sa.Column('actor_person_id', sa.Uuid(), nullable=True),
    sa.Column('action', ENUMS['item_action'], nullable=False),
    sa.Column('data', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['actor_person_id'], ['persons.id'], name=op.f('fk_item_events_actor_person_id_persons')),
    sa.ForeignKeyConstraint(['actor_user_id'], ['users.id'], name=op.f('fk_item_events_actor_user_id_users')),
    sa.ForeignKeyConstraint(['item_id'], ['items.id'], name=op.f('fk_item_events_item_id_items'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_item_events'))
    )
    op.create_index(op.f('ix_item_events_item_id'), 'item_events', ['item_id'], unique=False)
    op.create_table('notifications',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('type', ENUMS['notification_type'], nullable=False),
    sa.Column('recipient_person_id', sa.Uuid(), nullable=True),
    sa.Column('recipient_user_id', sa.Uuid(), nullable=True),
    sa.Column('meeting_id', sa.Uuid(), nullable=True),
    sa.Column('item_id', sa.Uuid(), nullable=True),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('dedupe_key', sa.String(length=200), nullable=False),
    sa.Column('scheduled_for', sa.DateTime(timezone=True), nullable=False),
    sa.Column('status', ENUMS['notification_status'], server_default='queued', nullable=False),
    sa.Column('attempts', sa.SmallInteger(), server_default='0', nullable=False),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('attempts >= 0', name=op.f('ck_notifications_attempts')),
    sa.CheckConstraint('num_nonnulls(recipient_person_id, recipient_user_id) = 1', name=op.f('ck_notifications_one_recipient')),
    sa.ForeignKeyConstraint(['item_id'], ['items.id'], name=op.f('fk_notifications_item_id_items'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['meeting_id'], ['meetings.id'], name=op.f('fk_notifications_meeting_id_meetings'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['recipient_person_id'], ['persons.id'], name=op.f('fk_notifications_recipient_person_id_persons'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['recipient_user_id'], ['users.id'], name=op.f('fk_notifications_recipient_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_notifications')),
    sa.UniqueConstraint('dedupe_key', name=op.f('uq_notifications_dedupe_key'))
    )
    op.create_index(op.f('ix_notifications_item_id'), 'notifications', ['item_id'], unique=False)
    op.create_index(op.f('ix_notifications_meeting_id'), 'notifications', ['meeting_id'], unique=False)
    op.create_index(op.f('ix_notifications_recipient_person_id'), 'notifications', ['recipient_person_id'], unique=False)
    op.create_index('ix_notifications_status_scheduled_for', 'notifications', ['status', 'scheduled_for'], unique=False)
    op.execute(ITEM_EVENTS_NO_UPDATE)


def downgrade() -> None:
    op.drop_index('ix_notifications_status_scheduled_for', table_name='notifications')
    op.drop_index(op.f('ix_notifications_recipient_person_id'), table_name='notifications')
    op.drop_index(op.f('ix_notifications_meeting_id'), table_name='notifications')
    op.drop_index(op.f('ix_notifications_item_id'), table_name='notifications')
    op.drop_table('notifications')
    op.drop_index(op.f('ix_item_events_item_id'), table_name='item_events')
    op.drop_table('item_events')
    op.drop_index(op.f('ix_item_comments_item_id'), table_name='item_comments')
    op.drop_table('item_comments')
    op.drop_index('uq_gaps_item_id_type_open', table_name='gaps', postgresql_where=sa.text('resolved_at IS NULL AND dismissed_at IS NULL'))
    op.drop_table('gaps')
    op.drop_index('ix_analytics_events_name_created_at', table_name='analytics_events')
    op.drop_table('analytics_events')
    op.drop_index('ix_items_series_id_status', table_name='items')
    op.drop_index(op.f('ix_items_meeting_id'), table_name='items')
    op.drop_index('ix_items_due_date_open', table_name='items', postgresql_where=sa.text("status IN ('pending', 'accepted')"))
    op.drop_index(op.f('ix_items_assignee_person_id'), table_name='items')
    op.drop_table('items')
    op.drop_index(op.f('ix_series_members_person_id'), table_name='series_members')
    op.drop_table('series_members')
    op.drop_index(op.f('ix_meeting_participants_person_id'), table_name='meeting_participants')
    op.drop_table('meeting_participants')
    op.drop_index(op.f('ix_llm_calls_meeting_id'), table_name='llm_calls')
    op.drop_index('ix_llm_calls_created_at', table_name='llm_calls')
    op.drop_table('llm_calls')
    op.drop_table('agenda_items')
    op.drop_index('uq_access_links_person_id_meeting_id_active', table_name='access_links', postgresql_where=sa.text('revoked_at IS NULL'))
    op.drop_index(op.f('ix_access_links_meeting_id'), table_name='access_links')
    op.drop_index('ix_access_links_expires_at', table_name='access_links')
    op.drop_table('access_links')
    op.drop_index(op.f('ix_persons_organization_id'), table_name='persons')
    op.drop_table('persons')
    op.drop_index('uq_meetings_series_id_active', table_name='meetings', postgresql_where=sa.text("status IN ('live', 'closing')"))
    op.drop_index('ix_meetings_series_id_planned_start', table_name='meetings')
    op.drop_table('meetings')
    op.drop_index('ix_series_owner_user_id_status', table_name='series')
    op.drop_table('series')
    op.drop_index('uq_organizations_owner_user_id_own', table_name='organizations', postgresql_where=sa.text('is_own'))
    op.drop_index('uq_organizations_owner_user_id_lower_name', table_name='organizations')
    op.drop_table('organizations')
    op.drop_index('uq_llm_prompts_task_active', table_name='llm_prompts', postgresql_where=sa.text('is_active'))
    op.drop_table('llm_prompts')
    op.drop_table('users')
    op.execute('DROP FUNCTION item_events_no_update()')
    bind = op.get_bind()
    for enum in reversed(ENUMS.values()):
        enum.drop(bind, checkfirst=False)
