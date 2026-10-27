-- DDL схемы базы «Договорились» (PostgreSQL 16).
-- Файл собран из миграций backend/migrations командой: bash db/dump_schema.sh
-- Руками не править: сначала миграция, потом пересборка файла.

--
-- PostgreSQL database dump
--



SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: citext; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS citext WITH SCHEMA public;


--
-- Name: agenda_item_status; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.agenda_item_status AS ENUM (
    'open',
    'covered',
    'moved'
);


--
-- Name: capture_mode; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.capture_mode AS ENUM (
    'manual',
    'audio'
);


--
-- Name: closure_trigger; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.closure_trigger AS ENUM (
    'button',
    'timer'
);


--
-- Name: gap_source; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.gap_source AS ENUM (
    'rule',
    'llm'
);


--
-- Name: gap_type; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.gap_type AS ENUM (
    'no_assignee',
    'no_due',
    'due_vague',
    'due_past',
    'vague_action'
);


--
-- Name: item_action; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.item_action AS ENUM (
    'created',
    'updated',
    'accepted_in_meeting',
    'sent',
    'accepted',
    'rescheduled',
    'disputed',
    'reassigned',
    'done',
    'cancelled',
    'reopened'
);


--
-- Name: item_actor_type; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.item_actor_type AS ENUM (
    'host',
    'assignee',
    'system',
    'tracker'
);


--
-- Name: item_comment_kind; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.item_comment_kind AS ENUM (
    'not_mine',
    'reschedule'
);


--
-- Name: item_done_by; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.item_done_by AS ENUM (
    'assignee',
    'host',
    'tracker'
);


--
-- Name: item_source; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.item_source AS ENUM (
    'manual',
    'asr'
);


--
-- Name: item_status; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.item_status AS ENUM (
    'draft',
    'pending',
    'accepted',
    'disputed',
    'done',
    'cancelled'
);


--
-- Name: llm_outcome; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.llm_outcome AS ENUM (
    'ok',
    'timeout',
    'invalid_output'
);


--
-- Name: llm_task; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.llm_task AS ENUM (
    'parse',
    'gaps',
    'question',
    'agenda'
);


--
-- Name: meeting_status; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.meeting_status AS ENUM (
    'planned',
    'live',
    'closing',
    'sent'
);


--
-- Name: notification_status; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.notification_status AS ENUM (
    'queued',
    'sent',
    'failed',
    'cancelled',
    'sending'
);


--
-- Name: notification_type; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.notification_type AS ENUM (
    'SUMMARY',
    'CONFIRM_REQUEST',
    'CONFIRM_REPEAT',
    'HOST_RESPONSE',
    'DUE_TOMORROW',
    'ITEM_CHANGED',
    'ITEM_REASSIGNED',
    'PASSWORD_RESET'
);


--
-- Name: series_status; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.series_status AS ENUM (
    'active',
    'archived'
);


--
-- Name: item_events_no_direct_delete(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.item_events_no_direct_delete() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF pg_trigger_depth() <= 1 AND coalesce(current_setting('app.retention_cleanup', true), '') <> 'on' THEN
        RAISE EXCEPTION 'item_events is append-only: rows go only with their item';
    END IF;
    RETURN OLD;
END;
$$;


--
-- Name: item_events_no_update(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.item_events_no_update() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    RAISE EXCEPTION 'item_events is append-only';
END;
$$;


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: access_link_tokens; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.access_link_tokens (
    id uuid NOT NULL,
    link_id uuid NOT NULL,
    token_hash bytea NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_access_link_tokens_ck_access_link_tokens_token_hash_len CHECK ((octet_length(token_hash) = 32))
);


--
-- Name: access_links; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.access_links (
    id uuid NOT NULL,
    token_hash bytea NOT NULL,
    person_id uuid NOT NULL,
    meeting_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    revoked_at timestamp with time zone,
    last_used_at timestamp with time zone,
    CONSTRAINT ck_access_links_token_hash_len CHECK ((octet_length(token_hash) = 32))
);


--
-- Name: agenda_items; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agenda_items (
    id uuid NOT NULL,
    meeting_id uuid NOT NULL,
    "position" smallint NOT NULL,
    title character varying(300) NOT NULL,
    status public.agenda_item_status DEFAULT 'open'::public.agenda_item_status NOT NULL
);


--
-- Name: analytics_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.analytics_events (
    id bigint NOT NULL,
    name character varying(64) NOT NULL,
    user_id uuid,
    series_id uuid,
    meeting_id uuid,
    item_id uuid,
    props jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: analytics_events_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.analytics_events_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: analytics_events_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.analytics_events_id_seq OWNED BY public.analytics_events.id;


--
-- Name: gaps; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.gaps (
    id uuid NOT NULL,
    item_id uuid NOT NULL,
    type public.gap_type NOT NULL,
    source public.gap_source DEFAULT 'rule'::public.gap_source NOT NULL,
    question character varying(300),
    detected_at timestamp with time zone DEFAULT now() NOT NULL,
    resolved_at timestamp with time zone,
    dismissed_at timestamp with time zone
);


--
-- Name: item_comments; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.item_comments (
    id uuid NOT NULL,
    item_id uuid NOT NULL,
    author_person_id uuid NOT NULL,
    kind public.item_comment_kind NOT NULL,
    text character varying(500),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    anonymized_at timestamp with time zone
);


--
-- Name: item_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.item_events (
    id bigint NOT NULL,
    item_id uuid NOT NULL,
    actor_type public.item_actor_type NOT NULL,
    actor_user_id uuid,
    actor_person_id uuid,
    action public.item_action NOT NULL,
    data jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: item_events_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.item_events_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: item_events_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.item_events_id_seq OWNED BY public.item_events.id;


--
-- Name: items; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.items (
    id uuid NOT NULL,
    meeting_id uuid NOT NULL,
    series_id uuid NOT NULL,
    agenda_item_id uuid,
    raw_text character varying(1000) NOT NULL,
    what character varying(500),
    assignee_person_id uuid,
    due_date date,
    due_text character varying(100),
    done_criteria character varying(500),
    manual_fields character varying[] DEFAULT '{}'::character varying[] NOT NULL,
    source public.item_source DEFAULT 'manual'::public.item_source NOT NULL,
    suggested boolean DEFAULT false NOT NULL,
    status public.item_status DEFAULT 'draft'::public.item_status NOT NULL,
    accepted_in_meeting boolean DEFAULT false NOT NULL,
    accepted_at timestamp with time zone,
    sent_at timestamp with time zone,
    done_at timestamp with time zone,
    done_by public.item_done_by,
    cancelled_at timestamp with time zone,
    reschedule_count integer DEFAULT 0 NOT NULL,
    version integer DEFAULT 1 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_items_cancelled_at CHECK (((status = 'cancelled'::public.item_status) = (cancelled_at IS NOT NULL))),
    CONSTRAINT ck_items_done_at CHECK (((status = 'done'::public.item_status) = (done_at IS NOT NULL))),
    CONSTRAINT ck_items_reschedule_count CHECK ((reschedule_count >= 0)),
    CONSTRAINT ck_items_version CHECK ((version >= 1))
);


--
-- Name: llm_calls; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.llm_calls (
    id uuid NOT NULL,
    meeting_id uuid,
    task public.llm_task NOT NULL,
    provider character varying(100) NOT NULL,
    model character varying(100) NOT NULL,
    prompt_id uuid,
    latency_ms integer,
    input_tokens integer,
    output_tokens integer,
    outcome public.llm_outcome NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: llm_prompts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.llm_prompts (
    id uuid NOT NULL,
    task public.llm_task NOT NULL,
    version integer NOT NULL,
    text text NOT NULL,
    is_active boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by uuid
);


--
-- Name: login_attempts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.login_attempts (
    id uuid NOT NULL,
    email public.citext NOT NULL,
    attempted_at timestamp with time zone NOT NULL
);


--
-- Name: meeting_participants; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.meeting_participants (
    meeting_id uuid NOT NULL,
    person_id uuid NOT NULL,
    present boolean DEFAULT true NOT NULL
);


--
-- Name: meetings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.meetings (
    id uuid NOT NULL,
    series_id uuid NOT NULL,
    planned_start timestamp with time zone NOT NULL,
    planned_end timestamp with time zone NOT NULL,
    status public.meeting_status DEFAULT 'planned'::public.meeting_status NOT NULL,
    started_at timestamp with time zone,
    closure_opened_at timestamp with time zone,
    closure_trigger public.closure_trigger,
    summary_sent_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_meetings_planned_period CHECK ((planned_end > planned_start))
);


--
-- Name: notifications; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.notifications (
    id uuid NOT NULL,
    type public.notification_type NOT NULL,
    recipient_person_id uuid,
    recipient_user_id uuid,
    meeting_id uuid,
    item_id uuid,
    payload jsonb,
    dedupe_key character varying(200) NOT NULL,
    scheduled_for timestamp with time zone NOT NULL,
    status public.notification_status DEFAULT 'queued'::public.notification_status NOT NULL,
    attempts smallint DEFAULT '0'::smallint NOT NULL,
    last_error text,
    sent_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    claimed_at timestamp with time zone,
    postponed boolean DEFAULT false NOT NULL,
    CONSTRAINT ck_notifications_attempts CHECK ((attempts >= 0)),
    CONSTRAINT ck_notifications_one_recipient CHECK ((num_nonnulls(recipient_person_id, recipient_user_id) = 1))
);


--
-- Name: organizations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.organizations (
    id uuid NOT NULL,
    owner_user_id uuid NOT NULL,
    name character varying(200) NOT NULL,
    is_own boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: password_reset_tokens; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.password_reset_tokens (
    id uuid NOT NULL,
    user_id uuid NOT NULL,
    token_hash character varying(64) NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    used_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: persons; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.persons (
    id uuid NOT NULL,
    owner_user_id uuid NOT NULL,
    organization_id uuid NOT NULL,
    full_name character varying(200) NOT NULL,
    email public.citext,
    anonymized_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: series; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.series (
    id uuid NOT NULL,
    owner_user_id uuid NOT NULL,
    title character varying(200) NOT NULL,
    timezone character varying(64) NOT NULL,
    planned_duration_min smallint NOT NULL,
    capture_mode public.capture_mode DEFAULT 'manual'::public.capture_mode NOT NULL,
    status public.series_status DEFAULT 'active'::public.series_status NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    load_warning_threshold integer,
    archived_at timestamp with time zone,
    CONSTRAINT ck_series_load_warning_threshold CHECK ((load_warning_threshold >= 1)),
    CONSTRAINT ck_series_planned_duration_min CHECK (((planned_duration_min >= 15) AND (planned_duration_min <= 240)))
);


--
-- Name: series_members; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.series_members (
    series_id uuid NOT NULL,
    person_id uuid NOT NULL
);


--
-- Name: users; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.users (
    id uuid NOT NULL,
    email public.citext NOT NULL,
    password_hash character varying(255) NOT NULL,
    full_name character varying(200) NOT NULL,
    timezone character varying(64) DEFAULT 'Europe/Moscow'::character varying NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    pd_consent_at timestamp with time zone
);


--
-- Name: analytics_events id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.analytics_events ALTER COLUMN id SET DEFAULT nextval('public.analytics_events_id_seq'::regclass);


--
-- Name: item_events id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.item_events ALTER COLUMN id SET DEFAULT nextval('public.item_events_id_seq'::regclass);


--
-- Name: access_link_tokens pk_access_link_tokens; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.access_link_tokens
    ADD CONSTRAINT pk_access_link_tokens PRIMARY KEY (id);


--
-- Name: access_links pk_access_links; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.access_links
    ADD CONSTRAINT pk_access_links PRIMARY KEY (id);


--
-- Name: agenda_items pk_agenda_items; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agenda_items
    ADD CONSTRAINT pk_agenda_items PRIMARY KEY (id);


--
-- Name: analytics_events pk_analytics_events; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.analytics_events
    ADD CONSTRAINT pk_analytics_events PRIMARY KEY (id);


--
-- Name: gaps pk_gaps; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.gaps
    ADD CONSTRAINT pk_gaps PRIMARY KEY (id);


--
-- Name: item_comments pk_item_comments; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.item_comments
    ADD CONSTRAINT pk_item_comments PRIMARY KEY (id);


--
-- Name: item_events pk_item_events; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.item_events
    ADD CONSTRAINT pk_item_events PRIMARY KEY (id);


--
-- Name: items pk_items; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.items
    ADD CONSTRAINT pk_items PRIMARY KEY (id);


--
-- Name: llm_calls pk_llm_calls; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.llm_calls
    ADD CONSTRAINT pk_llm_calls PRIMARY KEY (id);


--
-- Name: llm_prompts pk_llm_prompts; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.llm_prompts
    ADD CONSTRAINT pk_llm_prompts PRIMARY KEY (id);


--
-- Name: login_attempts pk_login_attempts; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.login_attempts
    ADD CONSTRAINT pk_login_attempts PRIMARY KEY (id);


--
-- Name: meeting_participants pk_meeting_participants; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.meeting_participants
    ADD CONSTRAINT pk_meeting_participants PRIMARY KEY (meeting_id, person_id);


--
-- Name: meetings pk_meetings; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.meetings
    ADD CONSTRAINT pk_meetings PRIMARY KEY (id);


--
-- Name: notifications pk_notifications; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notifications
    ADD CONSTRAINT pk_notifications PRIMARY KEY (id);


--
-- Name: organizations pk_organizations; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.organizations
    ADD CONSTRAINT pk_organizations PRIMARY KEY (id);


--
-- Name: password_reset_tokens pk_password_reset_tokens; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.password_reset_tokens
    ADD CONSTRAINT pk_password_reset_tokens PRIMARY KEY (id);


--
-- Name: persons pk_persons; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.persons
    ADD CONSTRAINT pk_persons PRIMARY KEY (id);


--
-- Name: series pk_series; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.series
    ADD CONSTRAINT pk_series PRIMARY KEY (id);


--
-- Name: series_members pk_series_members; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.series_members
    ADD CONSTRAINT pk_series_members PRIMARY KEY (series_id, person_id);


--
-- Name: users pk_users; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT pk_users PRIMARY KEY (id);


--
-- Name: access_link_tokens uq_access_link_tokens_token_hash; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.access_link_tokens
    ADD CONSTRAINT uq_access_link_tokens_token_hash UNIQUE (token_hash);


--
-- Name: access_links uq_access_links_token_hash; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.access_links
    ADD CONSTRAINT uq_access_links_token_hash UNIQUE (token_hash);


--
-- Name: agenda_items uq_agenda_items_meeting_id_position; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agenda_items
    ADD CONSTRAINT uq_agenda_items_meeting_id_position UNIQUE (meeting_id, "position");


--
-- Name: llm_prompts uq_llm_prompts_task_version; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.llm_prompts
    ADD CONSTRAINT uq_llm_prompts_task_version UNIQUE (task, version);


--
-- Name: notifications uq_notifications_dedupe_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notifications
    ADD CONSTRAINT uq_notifications_dedupe_key UNIQUE (dedupe_key);


--
-- Name: password_reset_tokens uq_password_reset_tokens_token_hash; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.password_reset_tokens
    ADD CONSTRAINT uq_password_reset_tokens_token_hash UNIQUE (token_hash);


--
-- Name: persons uq_persons_owner_user_id_email; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.persons
    ADD CONSTRAINT uq_persons_owner_user_id_email UNIQUE (owner_user_id, email);


--
-- Name: users uq_users_email; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT uq_users_email UNIQUE (email);


--
-- Name: ix_access_link_tokens_link_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_access_link_tokens_link_id ON public.access_link_tokens USING btree (link_id);


--
-- Name: ix_access_links_expires_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_access_links_expires_at ON public.access_links USING btree (expires_at);


--
-- Name: ix_access_links_meeting_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_access_links_meeting_id ON public.access_links USING btree (meeting_id);


--
-- Name: ix_analytics_events_name_created_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_analytics_events_name_created_at ON public.analytics_events USING btree (name, created_at);


--
-- Name: ix_item_comments_item_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_item_comments_item_id ON public.item_comments USING btree (item_id);


--
-- Name: ix_item_events_created_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_item_events_created_at ON public.item_events USING btree (created_at);


--
-- Name: ix_item_events_item_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_item_events_item_id ON public.item_events USING btree (item_id);


--
-- Name: ix_items_assignee_person_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_items_assignee_person_id ON public.items USING btree (assignee_person_id);


--
-- Name: ix_items_due_date_open; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_items_due_date_open ON public.items USING btree (due_date) WHERE (status = ANY (ARRAY['pending'::public.item_status, 'accepted'::public.item_status]));


--
-- Name: ix_items_meeting_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_items_meeting_id ON public.items USING btree (meeting_id);


--
-- Name: ix_items_series_id_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_items_series_id_status ON public.items USING btree (series_id, status);


--
-- Name: ix_llm_calls_created_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_llm_calls_created_at ON public.llm_calls USING btree (created_at);


--
-- Name: ix_llm_calls_meeting_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_llm_calls_meeting_id ON public.llm_calls USING btree (meeting_id);


--
-- Name: ix_login_attempts_email_attempted_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_login_attempts_email_attempted_at ON public.login_attempts USING btree (email, attempted_at);


--
-- Name: ix_meeting_participants_person_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_meeting_participants_person_id ON public.meeting_participants USING btree (person_id);


--
-- Name: ix_meetings_series_id_planned_start; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_meetings_series_id_planned_start ON public.meetings USING btree (series_id, planned_start DESC);


--
-- Name: ix_notifications_item_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_notifications_item_id ON public.notifications USING btree (item_id);


--
-- Name: ix_notifications_meeting_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_notifications_meeting_id ON public.notifications USING btree (meeting_id);


--
-- Name: ix_notifications_recipient_person_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_notifications_recipient_person_id ON public.notifications USING btree (recipient_person_id);


--
-- Name: ix_notifications_status_scheduled_for; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_notifications_status_scheduled_for ON public.notifications USING btree (status, scheduled_for);


--
-- Name: ix_password_reset_tokens_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_password_reset_tokens_user_id ON public.password_reset_tokens USING btree (user_id);


--
-- Name: ix_persons_organization_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_persons_organization_id ON public.persons USING btree (organization_id);


--
-- Name: ix_series_archived_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_series_archived_at ON public.series USING btree (archived_at);


--
-- Name: ix_series_members_person_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_series_members_person_id ON public.series_members USING btree (person_id);


--
-- Name: ix_series_owner_user_id_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_series_owner_user_id_status ON public.series USING btree (owner_user_id, status);


--
-- Name: uq_access_links_person_id_meeting_id_active; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_access_links_person_id_meeting_id_active ON public.access_links USING btree (person_id, meeting_id) WHERE (revoked_at IS NULL);


--
-- Name: uq_gaps_item_id_type_open; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_gaps_item_id_type_open ON public.gaps USING btree (item_id, type) WHERE ((resolved_at IS NULL) AND (dismissed_at IS NULL));


--
-- Name: uq_llm_prompts_task_active; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_llm_prompts_task_active ON public.llm_prompts USING btree (task) WHERE is_active;


--
-- Name: uq_meetings_series_id_active; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_meetings_series_id_active ON public.meetings USING btree (series_id) WHERE (status = ANY (ARRAY['live'::public.meeting_status, 'closing'::public.meeting_status]));


--
-- Name: uq_organizations_owner_user_id_lower_name; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_organizations_owner_user_id_lower_name ON public.organizations USING btree (owner_user_id, lower((name)::text));


--
-- Name: uq_organizations_owner_user_id_own; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_organizations_owner_user_id_own ON public.organizations USING btree (owner_user_id) WHERE is_own;


--
-- Name: item_events item_events_no_direct_delete; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER item_events_no_direct_delete BEFORE DELETE ON public.item_events FOR EACH ROW EXECUTE FUNCTION public.item_events_no_direct_delete();


--
-- Name: item_events item_events_no_update; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER item_events_no_update BEFORE UPDATE ON public.item_events FOR EACH ROW EXECUTE FUNCTION public.item_events_no_update();


--
-- Name: access_link_tokens fk_access_link_tokens_link_id_access_links; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.access_link_tokens
    ADD CONSTRAINT fk_access_link_tokens_link_id_access_links FOREIGN KEY (link_id) REFERENCES public.access_links(id) ON DELETE CASCADE;


--
-- Name: access_links fk_access_links_meeting_id_meetings; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.access_links
    ADD CONSTRAINT fk_access_links_meeting_id_meetings FOREIGN KEY (meeting_id) REFERENCES public.meetings(id) ON DELETE CASCADE;


--
-- Name: access_links fk_access_links_person_id_persons; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.access_links
    ADD CONSTRAINT fk_access_links_person_id_persons FOREIGN KEY (person_id) REFERENCES public.persons(id) ON DELETE CASCADE;


--
-- Name: agenda_items fk_agenda_items_meeting_id_meetings; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agenda_items
    ADD CONSTRAINT fk_agenda_items_meeting_id_meetings FOREIGN KEY (meeting_id) REFERENCES public.meetings(id) ON DELETE CASCADE;


--
-- Name: analytics_events fk_analytics_events_item_id_items; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.analytics_events
    ADD CONSTRAINT fk_analytics_events_item_id_items FOREIGN KEY (item_id) REFERENCES public.items(id) ON DELETE SET NULL;


--
-- Name: analytics_events fk_analytics_events_meeting_id_meetings; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.analytics_events
    ADD CONSTRAINT fk_analytics_events_meeting_id_meetings FOREIGN KEY (meeting_id) REFERENCES public.meetings(id) ON DELETE SET NULL;


--
-- Name: analytics_events fk_analytics_events_series_id_series; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.analytics_events
    ADD CONSTRAINT fk_analytics_events_series_id_series FOREIGN KEY (series_id) REFERENCES public.series(id) ON DELETE SET NULL;


--
-- Name: analytics_events fk_analytics_events_user_id_users; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.analytics_events
    ADD CONSTRAINT fk_analytics_events_user_id_users FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE SET NULL;


--
-- Name: gaps fk_gaps_item_id_items; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.gaps
    ADD CONSTRAINT fk_gaps_item_id_items FOREIGN KEY (item_id) REFERENCES public.items(id) ON DELETE CASCADE;


--
-- Name: item_comments fk_item_comments_author_person_id_persons; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.item_comments
    ADD CONSTRAINT fk_item_comments_author_person_id_persons FOREIGN KEY (author_person_id) REFERENCES public.persons(id) ON DELETE CASCADE;


--
-- Name: item_comments fk_item_comments_item_id_items; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.item_comments
    ADD CONSTRAINT fk_item_comments_item_id_items FOREIGN KEY (item_id) REFERENCES public.items(id) ON DELETE CASCADE;


--
-- Name: item_events fk_item_events_actor_person_id_persons; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.item_events
    ADD CONSTRAINT fk_item_events_actor_person_id_persons FOREIGN KEY (actor_person_id) REFERENCES public.persons(id);


--
-- Name: item_events fk_item_events_actor_user_id_users; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.item_events
    ADD CONSTRAINT fk_item_events_actor_user_id_users FOREIGN KEY (actor_user_id) REFERENCES public.users(id);


--
-- Name: item_events fk_item_events_item_id_items; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.item_events
    ADD CONSTRAINT fk_item_events_item_id_items FOREIGN KEY (item_id) REFERENCES public.items(id) ON DELETE CASCADE;


--
-- Name: items fk_items_agenda_item_id_agenda_items; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.items
    ADD CONSTRAINT fk_items_agenda_item_id_agenda_items FOREIGN KEY (agenda_item_id) REFERENCES public.agenda_items(id) ON DELETE SET NULL;


--
-- Name: items fk_items_assignee_person_id_persons; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.items
    ADD CONSTRAINT fk_items_assignee_person_id_persons FOREIGN KEY (assignee_person_id) REFERENCES public.persons(id) ON DELETE RESTRICT;


--
-- Name: items fk_items_meeting_id_meetings; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.items
    ADD CONSTRAINT fk_items_meeting_id_meetings FOREIGN KEY (meeting_id) REFERENCES public.meetings(id) ON DELETE CASCADE;


--
-- Name: items fk_items_series_id_series; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.items
    ADD CONSTRAINT fk_items_series_id_series FOREIGN KEY (series_id) REFERENCES public.series(id) ON DELETE CASCADE;


--
-- Name: llm_calls fk_llm_calls_meeting_id_meetings; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.llm_calls
    ADD CONSTRAINT fk_llm_calls_meeting_id_meetings FOREIGN KEY (meeting_id) REFERENCES public.meetings(id) ON DELETE SET NULL;


--
-- Name: llm_calls fk_llm_calls_prompt_id_llm_prompts; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.llm_calls
    ADD CONSTRAINT fk_llm_calls_prompt_id_llm_prompts FOREIGN KEY (prompt_id) REFERENCES public.llm_prompts(id) ON DELETE RESTRICT;


--
-- Name: llm_prompts fk_llm_prompts_created_by_users; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.llm_prompts
    ADD CONSTRAINT fk_llm_prompts_created_by_users FOREIGN KEY (created_by) REFERENCES public.users(id) ON DELETE SET NULL;


--
-- Name: meeting_participants fk_meeting_participants_meeting_id_meetings; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.meeting_participants
    ADD CONSTRAINT fk_meeting_participants_meeting_id_meetings FOREIGN KEY (meeting_id) REFERENCES public.meetings(id) ON DELETE CASCADE;


--
-- Name: meeting_participants fk_meeting_participants_person_id_persons; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.meeting_participants
    ADD CONSTRAINT fk_meeting_participants_person_id_persons FOREIGN KEY (person_id) REFERENCES public.persons(id) ON DELETE CASCADE;


--
-- Name: meetings fk_meetings_series_id_series; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.meetings
    ADD CONSTRAINT fk_meetings_series_id_series FOREIGN KEY (series_id) REFERENCES public.series(id) ON DELETE CASCADE;


--
-- Name: notifications fk_notifications_item_id_items; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notifications
    ADD CONSTRAINT fk_notifications_item_id_items FOREIGN KEY (item_id) REFERENCES public.items(id) ON DELETE CASCADE;


--
-- Name: notifications fk_notifications_meeting_id_meetings; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notifications
    ADD CONSTRAINT fk_notifications_meeting_id_meetings FOREIGN KEY (meeting_id) REFERENCES public.meetings(id) ON DELETE CASCADE;


--
-- Name: notifications fk_notifications_recipient_person_id_persons; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notifications
    ADD CONSTRAINT fk_notifications_recipient_person_id_persons FOREIGN KEY (recipient_person_id) REFERENCES public.persons(id) ON DELETE CASCADE;


--
-- Name: notifications fk_notifications_recipient_user_id_users; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notifications
    ADD CONSTRAINT fk_notifications_recipient_user_id_users FOREIGN KEY (recipient_user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: organizations fk_organizations_owner_user_id_users; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.organizations
    ADD CONSTRAINT fk_organizations_owner_user_id_users FOREIGN KEY (owner_user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: password_reset_tokens fk_password_reset_tokens_user_id_users; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.password_reset_tokens
    ADD CONSTRAINT fk_password_reset_tokens_user_id_users FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: persons fk_persons_organization_id_organizations; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.persons
    ADD CONSTRAINT fk_persons_organization_id_organizations FOREIGN KEY (organization_id) REFERENCES public.organizations(id) ON DELETE RESTRICT;


--
-- Name: persons fk_persons_owner_user_id_users; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.persons
    ADD CONSTRAINT fk_persons_owner_user_id_users FOREIGN KEY (owner_user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: series_members fk_series_members_person_id_persons; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.series_members
    ADD CONSTRAINT fk_series_members_person_id_persons FOREIGN KEY (person_id) REFERENCES public.persons(id) ON DELETE CASCADE;


--
-- Name: series_members fk_series_members_series_id_series; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.series_members
    ADD CONSTRAINT fk_series_members_series_id_series FOREIGN KEY (series_id) REFERENCES public.series(id) ON DELETE CASCADE;


--
-- Name: series fk_series_owner_user_id_users; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.series
    ADD CONSTRAINT fk_series_owner_user_id_users FOREIGN KEY (owner_user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- PostgreSQL database dump complete
--


