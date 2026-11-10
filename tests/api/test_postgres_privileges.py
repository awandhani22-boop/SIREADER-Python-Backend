"""PostgreSQL integration test for the fourteen-view security boundary.

This test runs only against an explicitly supplied disposable CI database.
It never connects to Supabase or to the user's production database.
"""

import os
from pathlib import Path

import psycopg
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from app.api.server import (
    PRIVILEGE_AUDIT_SQL,
    APPROVED_VIEW_NAMES,
    validate_approved_views_executable,
    validate_ro_database_identity,
    validate_ro_database_privileges,
)

DATABASE_URL = os.getenv("SIREADER_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="SIREADER_TEST_POSTGRES_URL is only provided by the disposable PostgreSQL CI service.",
)

ROLE_DDL = [
    """
    CREATE ROLE sireader_view_owner
      NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
      NOREPLICATION NOBYPASSRLS NOINHERIT
    """,
    """
    CREATE ROLE sireader_api_ro
      LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
      NOREPLICATION NOBYPASSRLS NOINHERIT
      PASSWORD 'ci-only-sireader-api-password'
    """,
    """
    CREATE ROLE sireader_workforce_cmd
      LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
      NOREPLICATION NOBYPASSRLS NOINHERIT
      PASSWORD 'ci-only-workforce-command-password'
    """,
]

TABLE_DDL = [
    """
    CREATE TABLE public.runtime_control_state (
        id BIGINT PRIMARY KEY,
        execution_mode VARCHAR(20) NOT NULL,
        trading_mode VARCHAR(20) NOT NULL,
        auto_execution_enabled BOOLEAN NOT NULL,
        auto_paused BOOLEAN NOT NULL,
        updated_by BIGINT,
        updated_at TIMESTAMPTZ NOT NULL
    )
    """,
    """
    CREATE TABLE public.ai_usage_logs (
        id BIGSERIAL PRIMARY KEY,
        request_id VARCHAR(255),
        usage_date DATE NOT NULL,
        provider VARCHAR(50) NOT NULL,
        model VARCHAR(255) NOT NULL,
        tier VARCHAR(30) NOT NULL,
        input_tokens INTEGER NOT NULL,
        output_tokens INTEGER NOT NULL,
        cache_read_tokens INTEGER NOT NULL,
        cache_write_tokens INTEGER NOT NULL,
        cache_write_5m_tokens INTEGER,
        cache_write_1h_tokens INTEGER,
        thinking_tokens INTEGER NOT NULL,
        cache_ttl_estimated BOOLEAN,
        billing_currency VARCHAR(3),
        cost_rp NUMERIC(18, 6),
        reserved_rp NUMERIC(18, 6),
        cost_usd NUMERIC(18, 6),
        reserved_usd NUMERIC(18, 6),
        analysis_log_id BIGINT,
        status VARCHAR(20) NOT NULL,
        error_message TEXT,
        created_at TIMESTAMPTZ NOT NULL,
        completed_at TIMESTAMPTZ
    )
    """,
    """
    CREATE TABLE public.ai_analysis_logs (
        id BIGSERIAL PRIMARY KEY,
        chat_id BIGINT,
        target_message_id BIGINT,
        status VARCHAR(40) NOT NULL,
        result_json TEXT,
        error_message TEXT,
        created_at TIMESTAMPTZ NOT NULL,
        completed_at TIMESTAMPTZ
    )
    """,
    """
    CREATE TABLE public.ai_pending_messages (
        id BIGSERIAL PRIMARY KEY,
        dedup_key VARCHAR(255),
        chat_id BIGINT,
        telegram_message_id BIGINT,
        analysis_log_id BIGINT,
        status VARCHAR(20) NOT NULL,
        reason TEXT,
        attempts INTEGER NOT NULL,
        claimed_by VARCHAR(255),
        claimed_at TIMESTAMPTZ,
        next_attempt_at TIMESTAMPTZ,
        last_error TEXT,
        created_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL
    )
    """,
    """
    CREATE TABLE public.trade_decision_watches (
        id BIGSERIAL PRIMARY KEY,
        analysis_log_id BIGINT,
        trade_episode_id BIGINT,
        symbol VARCHAR(64) NOT NULL,
        broker_server VARCHAR(128),
        status VARCHAR(32) NOT NULL,
        spread_pips NUMERIC(18, 5),
        maximum_spread_pips NUMERIC(18, 5) NOT NULL,
        last_bid NUMERIC(18, 5),
        last_ask NUMERIC(18, 5),
        last_checked_at TIMESTAMPTZ,
        next_check_at TIMESTAMPTZ NOT NULL,
        check_count INTEGER NOT NULL,
        last_reason TEXT,
        created_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL,
        resolved_at TIMESTAMPTZ
    )
    """,
    """
    CREATE TABLE public.approval_decisions (
        id BIGSERIAL PRIMARY KEY,
        analysis_log_id BIGINT,
        previous_status VARCHAR(40) NOT NULL,
        decision VARCHAR(20) NOT NULL,
        new_status VARCHAR(40) NOT NULL,
        reviewer VARCHAR(255),
        note TEXT,
        created_at TIMESTAMPTZ NOT NULL
    )
    """,
]

POLICY_DDL = [
    f"""
    ALTER TABLE public.{table_name} ENABLE ROW LEVEL SECURITY
    """
    for table_name in (
        "runtime_control_state",
        "ai_usage_logs",
        "ai_analysis_logs",
        "ai_pending_messages",
        "trade_decision_watches",
        "approval_decisions",
    )
] + [
    f"""
    CREATE POLICY {table_name}_view_owner_select
    ON public.{table_name}
    FOR SELECT TO sireader_view_owner
    USING (true)
    """
    for table_name in (
        "runtime_control_state",
        "ai_usage_logs",
        "ai_analysis_logs",
        "ai_pending_messages",
        "trade_decision_watches",
        "approval_decisions",
    )
]

COLUMN_GRANTS = [
    "GRANT USAGE ON SCHEMA public TO sireader_view_owner",
    "GRANT USAGE ON SCHEMA public TO sireader_api_ro",
    """
    GRANT SELECT (
        execution_mode, trading_mode, auto_execution_enabled, auto_paused, updated_at
    ) ON public.runtime_control_state TO sireader_view_owner
    """,
    """
    GRANT SELECT (
        usage_date, provider, model, tier, input_tokens, output_tokens,
        cache_read_tokens, cache_write_tokens, thinking_tokens, billing_currency,
        cost_rp, reserved_rp, cost_usd, reserved_usd, status, created_at, completed_at
    ) ON public.ai_usage_logs TO sireader_view_owner
    """,
    """
    GRANT SELECT (id, status, created_at)
    ON public.ai_analysis_logs TO sireader_view_owner
    """,
    """
    GRANT SELECT (id, status, reason, attempts, claimed_at, created_at, updated_at)
    ON public.ai_pending_messages TO sireader_view_owner
    """,
    """
    GRANT SELECT (
        id, symbol, status, spread_pips, maximum_spread_pips, last_bid, last_ask,
        check_count, last_checked_at, next_check_at, created_at, updated_at
    ) ON public.trade_decision_watches TO sireader_view_owner
    """,
    """
    GRANT SELECT (id, decision, previous_status, new_status, created_at)
    ON public.approval_decisions TO sireader_view_owner
    """,
]

VIEW_DDL = [
    """
    CREATE VIEW public.v_api_system_runtime_state AS
    SELECT execution_mode, trading_mode, auto_execution_enabled, auto_paused, updated_at
    FROM public.runtime_control_state
    """,
    """
    CREATE VIEW public.v_api_telemetry_usage AS
    SELECT
        usage_date, provider, model, tier, input_tokens, output_tokens,
        cache_read_tokens, cache_write_tokens, thinking_tokens,
        COALESCE(billing_currency, 'IDR')::varchar(3) AS billing_currency,
        cost_rp, reserved_rp, cost_usd, reserved_usd, status, created_at, completed_at
    FROM public.ai_usage_logs
    """,
    """
    CREATE VIEW public.v_api_active_in_flight_tasks AS
    SELECT
        id AS task_id,
        status,
        created_at,
        EXTRACT(EPOCH FROM (now() - created_at)) AS elapsed_seconds
    FROM public.ai_analysis_logs
    WHERE status = 'PROCESSING'
    """,
    """
    CREATE VIEW public.v_api_queued_pending_messages AS
    SELECT
        id AS queue_item_id,
        status,
        CASE
            WHEN reason = 'BLOCKED_HARD_BUDGET' THEN 'BLOCKED_HARD_BUDGET'
            WHEN reason = 'MANUAL_REVIEW_BUDGET' THEN 'MANUAL_REVIEW_BUDGET'
            ELSE 'OTHER'
        END AS queue_reason,
        attempts, claimed_at, created_at, updated_at
    FROM public.ai_pending_messages
    WHERE status IN ('PENDING', 'CLAIMED')
    """,
    """
    CREATE VIEW public.v_api_active_market_watches AS
    SELECT
        id AS watch_id, symbol, status, spread_pips, maximum_spread_pips,
        last_bid, last_ask, check_count, last_checked_at, next_check_at,
        created_at, updated_at
    FROM public.trade_decision_watches
    WHERE status IN ('WAITING_FOR_SPREAD', 'RECHECKING')
    """,
    """
    CREATE VIEW public.v_api_audit_decisions AS
    SELECT
        id AS audit_id, decision, previous_status, new_status,
        'Authorized Approver'::varchar(50) AS reviewer_role,
        created_at AS decided_at
    FROM public.approval_decisions
    """,
]

VIEW_NAMES = APPROVED_VIEW_NAMES

SEED_DML = [
    """
    INSERT INTO public.runtime_control_state
    (id, execution_mode, trading_mode, auto_execution_enabled, auto_paused, updated_at)
    VALUES (1, 'APPROVAL', 'DRY_RUN', false, true, now())
    """,
    """
    INSERT INTO public.ai_usage_logs
    (usage_date, provider, model, tier, input_tokens, output_tokens,
     cache_read_tokens, cache_write_tokens, thinking_tokens, billing_currency,
     cost_rp, reserved_rp, cost_usd, reserved_usd, status, created_at, completed_at)
    VALUES (current_date, 'ci', 'ci-model', 'FAST', 5, 7, 0, 0, 0, 'IDR',
            100, 20, 900, 900, 'COMPLETED', now(), now())
    """,
    """
    INSERT INTO public.ai_analysis_logs (chat_id, target_message_id, status, created_at)
    VALUES (123, 456, 'PROCESSING', now())
    """,
    """
    INSERT INTO public.ai_pending_messages
    (dedup_key, chat_id, telegram_message_id, status, reason, attempts, created_at, updated_at)
    VALUES ('private-dedup-key', 123, 456, 'PENDING', 'BLOCKED_HARD_BUDGET', 1, now(), now())
    """,
    """
    INSERT INTO public.trade_decision_watches
    (symbol, broker_server, status, spread_pips, maximum_spread_pips,
     check_count, next_check_at, last_reason, created_at, updated_at)
    VALUES ('XAUUSD', 'private-broker', 'WAITING_FOR_SPREAD', 5, 30,
            1, now(), 'private reason', now(), now())
    """,
    """
    INSERT INTO public.approval_decisions
    (analysis_log_id, previous_status, decision, new_status, reviewer, note, created_at)
    VALUES (1, 'MANUAL_REVIEW', 'APPROVED', 'APPROVED', 'private-reviewer', 'private note', now())
    """,
]


@pytest.fixture(scope="module")
def postgres_security_engine():
    """Build an isolated disposable PostgreSQL schema using CI superuser credentials."""
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    with engine.begin() as connection:
        # This test database is ephemeral and dedicated to this test module.
        for view_name in VIEW_NAMES:
            connection.exec_driver_sql(f"DROP VIEW IF EXISTS public.{view_name} CASCADE")
        for table_name in (
            "runtime_control_state", "ai_usage_logs", "ai_analysis_logs",
            "ai_pending_messages", "trade_decision_watches", "approval_decisions",
        ):
            connection.exec_driver_sql(f"DROP TABLE IF EXISTS public.{table_name} CASCADE")
        for table_name in ("workforce_task_review_payloads", "workforce_task_reviews"):
            connection.exec_driver_sql(f"DROP TABLE IF EXISTS public.{table_name} CASCADE")
        for table_name in (
            "workforce_command_audit", "workforce_activity_events", "workforce_task_attempts", "workforce_tasks",
            "workforce_heartbeats", "workforce_worker_capabilities",
            "workforce_capabilities", "workforce_workers",
        ):
            connection.exec_driver_sql(f"DROP TABLE IF EXISTS public.{table_name} CASCADE")
        connection.exec_driver_sql("DROP FUNCTION IF EXISTS public.sireader_notify_workforce_v1() CASCADE")
        connection.exec_driver_sql("DROP FUNCTION IF EXISTS public.sireader_guard_workforce_result_release_v1() CASCADE")
        connection.exec_driver_sql("DROP FUNCTION IF EXISTS public.sireader_guard_review_payloads_v1() CASCADE")
        connection.exec_driver_sql("DROP FUNCTION IF EXISTS public.sireader_guard_review_metadata_v1() CASCADE")
        for role_name in ("sireader_workforce_cmd", "sireader_api_ro", "sireader_view_owner"):
            connection.exec_driver_sql(
                f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role_name}') THEN "
                f"EXECUTE 'DROP OWNED BY {role_name} CASCADE'; END IF; END $$;"
            )
            connection.exec_driver_sql(f"DROP ROLE IF EXISTS {role_name}")

        for statement in ROLE_DDL:
            connection.exec_driver_sql(statement)
        for statement in TABLE_DDL:
            connection.exec_driver_sql(statement)
        for statement in POLICY_DDL:
            connection.exec_driver_sql(statement)
        for statement in COLUMN_GRANTS:
            connection.exec_driver_sql(statement)
        for statement in VIEW_DDL:
            connection.exec_driver_sql(statement)

    # Apply transactional SQL files through psycopg autocommit, matching the
    # dedicated disposable-PostgreSQL migration test harness.
    migration_root = Path(__file__).resolve().parents[2] / "migrations"
    psycopg_url = make_url(DATABASE_URL).set(drivername="postgresql").render_as_string(hide_password=False)
    with psycopg.connect(psycopg_url, autocommit=True) as admin:
        for migration_name in (
            "2026_11_09_workforce_runtime_v1.sql",
            "2026_11_09_workforce_api_views_v1.sql",
            "2026_11_09_workforce_commands_v1.sql",
            "2026_11_09_workforce_timeline_v2.sql",
            "2026_11_10_workforce_reviews_v1.sql",
            "2026_11_10_workforce_review_creation_boundary_v3.sql",
            "2026_11_10_workforce_review_payload_insert_guard_v2.sql",
            "2026_11_10_workforce_budget_observability_v1.sql",
            "2026_11_10_workforce_review_command_security_v1.sql",
            "2026_11_10_workforce_review_command_security_v2.sql",
            "2026_11_10_workforce_review_command_security_v3.sql",
        ):
            admin.execute((migration_root / migration_name).read_text(), prepare=False)

    with engine.begin() as connection:
        for view_name in VIEW_NAMES:
            connection.exec_driver_sql(
                f"ALTER VIEW public.{view_name} OWNER TO sireader_view_owner"
            )
        connection.exec_driver_sql(
            """
            GRANT SELECT ON
                public.v_api_system_runtime_state,
                public.v_api_telemetry_usage,
                public.v_api_active_in_flight_tasks,
                public.v_api_queued_pending_messages,
                public.v_api_active_market_watches,
                public.v_api_audit_decisions
            TO sireader_api_ro
            """
        )
        for statement in SEED_DML:
            connection.exec_driver_sql(statement)

    yield engine
    engine.dispose()


def test_postgres_effective_role_and_privilege_boundary(postgres_security_engine):
    with postgres_security_engine.connect() as connection:
        connection.exec_driver_sql("SET ROLE sireader_api_ro")
        connection.exec_driver_sql("SET TRANSACTION READ ONLY")

        identity = connection.execute(text("""
            SELECT
                current_user AS role_name,
                current_setting('transaction_read_only') AS transaction_read_only,
                r.rolcanlogin, r.rolsuper, r.rolinherit, r.rolcreatedb,
                r.rolcreaterole, r.rolreplication, r.rolbypassrls
            FROM pg_roles r
            WHERE r.rolname = current_user
        """)).mappings().one()
        validate_ro_database_identity(identity)

        privileges = connection.execute(text(PRIVILEGE_AUDIT_SQL)).mappings().one()
        validate_ro_database_privileges(privileges)
        validate_approved_views_executable(connection)

        for view_name in VIEW_NAMES:
            rows = connection.execute(
                text(f"SELECT * FROM public.{view_name} LIMIT 10")
            ).mappings().all()
            expected_rows = {
                "v_api_workforce_workers": 7,
                "v_api_workforce_tasks": 0,
                "v_api_workforce_attempts": 0,
                "v_api_workforce_activity": 0,
            }.get(view_name, 1)
            assert len(rows) == expected_rows, f"{view_name} returned an unexpected row count"

        assert connection.execute(
            text("SELECT trading_mode FROM public.v_api_system_runtime_state")
        ).scalar_one() == "DRY_RUN"

        assert connection.execute(
            text("SELECT billing_currency FROM public.v_api_telemetry_usage")
        ).scalar_one() == "IDR"


@pytest.mark.parametrize(
    "table_name",
    [
        "runtime_control_state",
        "ai_usage_logs",
        "ai_analysis_logs",
        "ai_pending_messages",
        "trade_decision_watches",
        "approval_decisions",
        "workforce_workers",
        "workforce_capabilities",
        "workforce_worker_capabilities",
        "workforce_heartbeats",
        "workforce_tasks",
        "workforce_task_attempts",
        "workforce_activity_events",
    ],
)
def test_postgres_api_role_cannot_select_any_base_table(postgres_security_engine, table_name):
    with postgres_security_engine.connect() as connection:
        connection.exec_driver_sql("SET ROLE sireader_api_ro")
        with pytest.raises(DBAPIError):
            connection.execute(text(f"SELECT * FROM public.{table_name} LIMIT 1"))
        connection.rollback()


def test_postgres_readonly_transaction_rejects_mutation(postgres_security_engine):
    with postgres_security_engine.connect() as connection:
        connection.exec_driver_sql("SET ROLE sireader_api_ro")
        connection.exec_driver_sql("SET TRANSACTION READ ONLY")
        with pytest.raises(DBAPIError):
            connection.execute(text(
                "INSERT INTO public.runtime_control_state "
                "(id, execution_mode, trading_mode, auto_execution_enabled, auto_paused, updated_at) "
                "VALUES (99, 'APPROVAL', 'DRY_RUN', false, true, now())"
            ))
        connection.rollback()


def test_postgres_privilege_audit_detects_public_table_and_column_grants(postgres_security_engine):
    # Granting SELECT on an approved view to PUBLIC must be detected even though
    # the API login already has its own explicit SELECT grant.
    with postgres_security_engine.begin() as admin:
        admin.exec_driver_sql(
            "GRANT SELECT ON public.v_api_system_runtime_state TO PUBLIC"
        )
    try:
        with postgres_security_engine.connect() as api:
            api.exec_driver_sql("SET ROLE sireader_api_ro")
            api.exec_driver_sql("SET TRANSACTION READ ONLY")
            snapshot = api.execute(text(PRIVILEGE_AUDIT_SQL)).mappings().one()
            assert snapshot["public_table_grants"] is True
    finally:
        with postgres_security_engine.begin() as admin:
            admin.exec_driver_sql(
                "REVOKE SELECT ON public.v_api_system_runtime_state FROM PUBLIC"
            )

    # A PUBLIC column grant on a sensitive base-table column must be detected.
    with postgres_security_engine.begin() as admin:
        admin.exec_driver_sql(
            "GRANT SELECT (chat_id) ON public.ai_analysis_logs TO PUBLIC"
        )
    try:
        with postgres_security_engine.connect() as api:
            api.exec_driver_sql("SET ROLE sireader_api_ro")
            api.exec_driver_sql("SET TRANSACTION READ ONLY")
            snapshot = api.execute(text(PRIVILEGE_AUDIT_SQL)).mappings().one()
            assert snapshot["public_column_grants"] is True
            assert snapshot["api_base_column_access"] is True
    finally:
        with postgres_security_engine.begin() as admin:
            admin.exec_driver_sql(
                "REVOKE SELECT (chat_id) ON public.ai_analysis_logs FROM PUBLIC"
            )


def test_workforce_command_role_has_only_the_declared_command_boundary(postgres_security_engine):
    from sqlalchemy.orm import Session as OrmSession
    from app.command_api.database import validate_command_database_security

    trading_task_id = "ci-trading-task-rls-0001"
    safe_task_id = "ci-safe-task-rls-0001"
    trading_attempt_id = "ci-trading-attempt-rls-0001"
    with postgres_security_engine.connect() as connection:
        with connection.begin():
            # Seed a non-trading task and a trading-adjacent task as the DB owner
            # before switching to the restricted command login.
            connection.exec_driver_sql(
                """
                INSERT INTO public.workforce_tasks
                    (task_id, idempotency_key, correlation_id, task_type, title,
                     required_capability, requested_worker_id, status, input_payload)
                VALUES
                    ('ci-trading-task-rls-0001', 'ci-trading-key-rls-0001',
                     'ci-trading-correlation-0001', 'RULE_VALIDATION',
                     'Trading-adjacent rule fixture', 'rule_validation',
                     'decision_engineer', 'FAILED', '{}'::jsonb),
                    ('ci-safe-task-rls-0001', 'ci-safe-key-rls-0001',
                     'ci-safe-correlation-0001', 'SUMMARIZATION',
                     'Safe non-trading fixture', 'summarization',
                     'haiku', 'FAILED', '{}'::jsonb)
                """
            )
            connection.exec_driver_sql(
                """
                INSERT INTO public.workforce_task_attempts
                    (attempt_id, task_id, attempt_number, worker_id, lease_token, status)
                VALUES
                    ('ci-trading-attempt-rls-0001', 'ci-trading-task-rls-0001',
                     1, 'decision_engineer', 'ci-trading-lease-rls-0001', 'FAILED')
                """
            )

            connection.exec_driver_sql("SET LOCAL ROLE sireader_workforce_cmd")
            with OrmSession(bind=connection, join_transaction_mode="create_savepoint") as session:
                validate_command_database_security(session)

            # Only five general-purpose workers and non-trading capabilities/tasks
            # are visible through the database role itself.
            assert connection.exec_driver_sql(
                "SELECT count(*) FROM public.workforce_workers"
            ).scalar_one() == 5
            assert connection.exec_driver_sql(
                "SELECT count(*) FROM public.workforce_workers WHERE worker_id IN ('decision_engineer','market_monitor')"
            ).scalar_one() == 0
            assert connection.exec_driver_sql(
                "SELECT count(*) FROM public.workforce_capabilities WHERE capability_id = 'rule_validation'"
            ).scalar_one() == 0
            assert connection.exec_driver_sql(
                "SELECT count(*) FROM public.workforce_tasks WHERE task_id = 'ci-trading-task-rls-0001'"
            ).scalar_one() == 0
            assert connection.exec_driver_sql(
                "SELECT count(*) FROM public.workforce_task_attempts WHERE attempt_id = 'ci-trading-attempt-rls-0001'"
            ).scalar_one() == 0
            assert connection.exec_driver_sql(
                "SELECT count(*) FROM public.workforce_tasks WHERE task_id = 'ci-safe-task-rls-0001'"
            ).scalar_one() == 1

            # Scheduling writes are permitted for Haiku but silently cannot
            # affect Decision Engineer or Market Monitor rows.
            assert connection.exec_driver_sql(
                "UPDATE public.workforce_workers SET accepting_tasks = false WHERE worker_id = 'haiku'"
            ).rowcount == 1
            assert connection.exec_driver_sql(
                "UPDATE public.workforce_workers SET accepting_tasks = false WHERE worker_id = 'decision_engineer'"
            ).rowcount == 0

            # Task/attempt UPDATE grants are still row-scoped by RLS.
            assert connection.exec_driver_sql(
                "UPDATE public.workforce_tasks SET status = 'CANCELLED' WHERE task_id = 'ci-trading-task-rls-0001'"
            ).rowcount == 0
            assert connection.exec_driver_sql(
                "UPDATE public.workforce_task_attempts SET status = 'CANCELLED' "
                "WHERE attempt_id = 'ci-trading-attempt-rls-0001'"
            ).rowcount == 0

            # Direct SQL cannot INSERT a trading-adjacent workforce task/event.
            with pytest.raises(DBAPIError):
                with connection.begin_nested():
                    connection.exec_driver_sql(
                        """
                        INSERT INTO public.workforce_tasks
                            (task_id, idempotency_key, correlation_id, task_type, title,
                             required_capability, requested_worker_id, status, input_payload)
                        VALUES ('ci-trading-insert-rls-0001', 'ci-trading-insert-key-0001',
                                'ci-trading-insert-correlation-0001', 'RULE_VALIDATION',
                                'Must fail RLS', 'rule_validation', 'decision_engineer',
                                'QUEUED', '{}'::jsonb)
                        """
                    )
            with pytest.raises(DBAPIError):
                with connection.begin_nested():
                    connection.exec_driver_sql(
                        """
                        INSERT INTO public.workforce_activity_events
                            (event_id, event_type, occurred_at, worker_id, correlation_id, payload)
                        VALUES ('ci-trading-event-rls-0001', 'WORKER_STATE_CHANGED',
                                now(), 'decision_engineer', 'ci-trading-event-correlation-0001', '{}'::jsonb)
                        """
                    )

            # Audit append is permitted; direct mutation remains denied by
            # both privileges and the database trigger.
            connection.exec_driver_sql(
                """
                INSERT INTO public.workforce_command_audit
                    (command_id, actor_id, command_type, target_id, idempotency_key,
                     request_fingerprint, correlation_id, outcome, created_at)
                VALUES ('ci-command-audit-0001', 'local_operator', 'WORKER_SCHEDULING_SET',
                        'haiku', 'ci-command-key-0001', repeat('a', 64),
                        'ci-command-correlation-0001', '{}'::jsonb, now())
                """
            )
            for statement in (
                "UPDATE public.workforce_command_audit SET outcome = '{}'::jsonb "
                "WHERE command_id = 'ci-command-audit-0001'",
                "DELETE FROM public.workforce_command_audit "
                "WHERE command_id = 'ci-command-audit-0001'",
                "SELECT * FROM public.workforce_heartbeats LIMIT 1",
                "SELECT * FROM public.approval_decisions LIMIT 1",
                "SELECT * FROM public.ai_analysis_logs LIMIT 1",
                "UPDATE public.workforce_workers SET enabled = false WHERE worker_id = 'haiku'",
            ):
                with pytest.raises(DBAPIError):
                    with connection.begin_nested():
                        connection.exec_driver_sql(statement)
            assert connection.exec_driver_sql(
                "SELECT count(*) FROM public.workforce_command_audit"
            ).scalar_one() == 1



def test_workforce_command_role_can_adjudicate_reviews_with_scoped_rls(postgres_security_engine):
    from datetime import datetime, timedelta, timezone
    from sqlalchemy.orm import Session as OrmSession
    from app.command_api.database import validate_command_database_security
    from app.workers.repository import adjudicate_review

    now = datetime.now(timezone.utc)
    safe_rows = [
        ("ci-review-approved-task", "ci-review-approved-attempt", "ci-review-approved", "a" * 64, {"approved": "original"}),
        ("ci-review-edited-task", "ci-review-edited-attempt", "ci-review-edited", "b" * 64, {"edited": "original"}),
    ]
    trading = ("ci-review-trading-task", "ci-review-trading-attempt", "ci-review-trading")

    with postgres_security_engine.begin() as admin:
        for task_id, attempt_id, review_id, digest, payload in safe_rows:
            admin.execute(text("""
                INSERT INTO public.workforce_tasks
                    (task_id, idempotency_key, correlation_id, schema_version, task_type,
                     title, required_capability, requested_worker_id, priority, status,
                     attempts, max_attempts, not_before, created_at, updated_at,
                     completed_at, input_payload)
                VALUES (:task_id, :idem, :corr, 1, 'SUMMARIZATION', 'Review acceptance',
                        'summarization', 'haiku', 50, 'COMPLETED', 1, 2,
                        :now, :now, :now, :now, '{}'::jsonb)
            """), {"task_id": task_id, "idem": "idem-"+task_id, "corr": "corr-"+task_id, "now": now})
            admin.execute(text("""
                INSERT INTO public.workforce_task_attempts
                    (attempt_id, task_id, attempt_number, worker_id, lease_token,
                     status, started_at, finished_at, created_at)
                VALUES (:attempt_id, :task_id, 1, 'haiku', :lease, 'COMPLETED',
                        :now, :now, :now)
            """), {"attempt_id": attempt_id, "task_id": task_id, "lease": "lease-"+attempt_id, "now": now})
            admin.execute(text("""
                INSERT INTO public.workforce_task_reviews
                    (review_id, task_id, attempt_id, status, original_output_sha256,
                     review_expires_at, created_at, updated_at)
                VALUES (:review_id, :task_id, :attempt_id, 'PENDING', :digest,
                        :expires, :now, :now)
            """), {"review_id": review_id, "task_id": task_id, "attempt_id": attempt_id,
                   "digest": digest, "expires": now + timedelta(days=1), "now": now})
            admin.execute(text("""
                INSERT INTO public.workforce_task_review_payloads
                    (review_id, original_output, created_at, updated_at)
                VALUES (:review_id, CAST(:payload AS jsonb), :now, :now)
            """), {"review_id": review_id, "payload": __import__('json').dumps(payload), "now": now})

        task_id, attempt_id, review_id = trading
        admin.execute(text("""
            INSERT INTO public.workforce_tasks
                (task_id, idempotency_key, correlation_id, schema_version, task_type,
                 title, required_capability, requested_worker_id, priority, status,
                 attempts, max_attempts, not_before, created_at, updated_at,
                 completed_at, input_payload)
            VALUES (:task_id, :idem, :corr, 1, 'RULE_VALIDATION', 'Trading review fixture',
                    'rule_validation', 'decision_engineer', 50, 'COMPLETED', 1, 2,
                    :now, :now, :now, :now, '{}'::jsonb)
        """), {"task_id": task_id, "idem": "idem-"+task_id, "corr": "corr-"+task_id, "now": now})
        admin.execute(text("""
            INSERT INTO public.workforce_task_attempts
                (attempt_id, task_id, attempt_number, worker_id, lease_token,
                 status, started_at, finished_at, created_at)
            VALUES (:attempt_id, :task_id, 1, 'decision_engineer', :lease, 'COMPLETED',
                    :now, :now, :now)
        """), {"attempt_id": attempt_id, "task_id": task_id, "lease": "lease-"+attempt_id, "now": now})
        admin.execute(text("""
            INSERT INTO public.workforce_task_reviews
                (review_id, task_id, attempt_id, status, original_output_sha256,
                 review_expires_at, created_at, updated_at)
            VALUES (:review_id, :task_id, :attempt_id, 'PENDING', repeat('c', 64),
                    :expires, :now, :now)
        """), {"review_id": review_id, "task_id": task_id, "attempt_id": attempt_id,
               "expires": now + timedelta(days=1), "now": now})
        admin.execute(text("""
            INSERT INTO public.workforce_task_review_payloads
                (review_id, original_output, created_at, updated_at)
            VALUES (:review_id, '{"trading":"must stay hidden"}'::jsonb, :now, :now)
        """), {"review_id": review_id, "now": now})

    with postgres_security_engine.connect() as connection:
        with connection.begin():
            connection.exec_driver_sql("SET LOCAL ROLE sireader_workforce_cmd")
            with OrmSession(bind=connection, join_transaction_mode="create_savepoint") as db:
                validate_command_database_security(db)
                assert db.execute(
                    text("SELECT count(*) FROM public.workforce_task_reviews WHERE review_id = :id"),
                    {"id": trading[2]},
                ).scalar_one() == 0

                adjudicate_review(
                    db, review_id="ci-review-approved", decision="APPROVED",
                    operator_id="local_operator", expected_sha256="a" * 64,
                )
                adjudicate_review(
                    db, review_id="ci-review-edited", decision="EDITED",
                    operator_id="local_operator", expected_sha256="b" * 64,
                    adjusted_output={"edited": "approved text"},
                )
                db.flush()

            approved = connection.execute(text(
                "SELECT result_payload FROM public.workforce_tasks WHERE task_id = 'ci-review-approved-task'"
            )).scalar_one()
            edited = connection.execute(text(
                "SELECT result_payload FROM public.workforce_tasks WHERE task_id = 'ci-review-edited-task'"
            )).scalar_one()
            assert approved == {"approved": "original"}
            assert edited == {"edited": "approved text"}

            with pytest.raises(DBAPIError):
                with connection.begin_nested():
                    connection.exec_driver_sql(
                        "UPDATE public.workforce_tasks SET result_payload = '{\"forged\":true}'::jsonb "
                        "WHERE task_id = 'ci-review-approved-task'"
                    )



def test_workforce_command_role_can_append_non_trading_retry_lineage_event(postgres_security_engine):
    """TASK_RETRIED remains writable only for two visible non-trading task rows."""
    from datetime import datetime, timezone
    from uuid import uuid4

    suffix = uuid4().hex[:12]
    parent_id = f"retry-parent-{suffix}"
    child_id = f"retry-child-{suffix}"
    now = datetime.now(timezone.utc)

    with postgres_security_engine.connect() as connection:
        outer = connection.begin()
        try:
            for task_id, status in ((parent_id, "FAILED"), (child_id, "QUEUED")):
                connection.execute(text("""
                    INSERT INTO public.workforce_tasks
                        (task_id, idempotency_key, correlation_id, task_type, title,
                         required_capability, requested_worker_id, priority, status,
                         attempts, max_attempts, created_at, updated_at)
                    VALUES
                        (:task_id, :idempotency_key, :correlation_id, 'SUMMARIZATION',
                         'Retry RLS policy fixture', 'summarization', 'haiku', 50,
                         :status, 1, 3, :now, :now)
                """), {
                    "task_id": task_id,
                    "idempotency_key": f"idem-{task_id}",
                    "correlation_id": f"corr-{suffix}",
                    "status": status,
                    "now": now,
                })

            connection.exec_driver_sql("SET LOCAL ROLE sireader_workforce_cmd")
            connection.execute(text("""
                INSERT INTO public.workforce_activity_events
                    (event_id, event_type, occurred_at, task_id,
                     correlation_id, actor_type, actor_id, related_task_id)
                VALUES
                    (:event_id, 'TASK_RETRIED', :now, :parent_id,
                     :correlation_id, 'OPERATOR', 'local_operator', :child_id)
            """), {
                "event_id": f"evt-retry-{suffix}",
                "now": now,
                "parent_id": parent_id,
                "correlation_id": f"corr-{suffix}",
                "child_id": child_id,
            })
            count = connection.execute(text("""
                SELECT count(*) FROM public.workforce_activity_events
                WHERE event_id = :event_id AND event_type = 'TASK_RETRIED'
            """), {"event_id": f"evt-retry-{suffix}"}).scalar_one()
            assert count == 1
        finally:
            outer.rollback()



def test_workforce_command_role_can_lock_pending_review_payload_but_cannot_edit_it_early(postgres_security_engine):
    """SELECT FOR UPDATE works on PENDING payloads while UPDATE remains EDITED-only."""
    from datetime import datetime, timedelta, timezone
    from uuid import uuid4

    suffix = uuid4().hex[:12]
    task_id = f"payload-lock-task-{suffix}"
    attempt_id = f"payload-lock-attempt-{suffix}"
    review_id = f"rev_payload_lock_{suffix}"
    now = datetime.now(timezone.utc)
    digest = "a" * 64

    with postgres_security_engine.connect() as connection:
        outer = connection.begin()
        try:
            connection.execute(text("""
                INSERT INTO public.workforce_tasks
                    (task_id, idempotency_key, correlation_id, task_type, title,
                     required_capability, requested_worker_id, priority, status,
                     attempts, max_attempts, created_at, updated_at, completed_at)
                VALUES
                    (:task_id, :idem, :corr, 'SUMMARIZATION', 'Pending review payload lock',
                     'summarization', 'haiku', 50, 'COMPLETED', 1, 3, :now, :now, :now)
            """), {"task_id": task_id, "idem": f"idem-{suffix}", "corr": f"corr-{suffix}", "now": now})
            connection.execute(text("""
                INSERT INTO public.workforce_task_attempts
                    (attempt_id, task_id, attempt_number, worker_id, lease_token, status, created_at)
                VALUES (:attempt_id, :task_id, 1, 'haiku', :lease, 'COMPLETED', :now)
            """), {"attempt_id": attempt_id, "task_id": task_id, "lease": f"lease-{suffix}", "now": now})
            connection.execute(text("""
                INSERT INTO public.workforce_task_reviews
                    (review_id, task_id, attempt_id, status, original_output_sha256,
                     review_expires_at, created_at, updated_at)
                VALUES (:review_id, :task_id, :attempt_id, 'PENDING', :digest,
                        :expires, :now, :now)
            """), {"review_id": review_id, "task_id": task_id, "attempt_id": attempt_id,
                   "digest": digest, "expires": now + timedelta(hours=24), "now": now})
            connection.execute(text("""
                INSERT INTO public.workforce_task_review_payloads
                    (review_id, original_output, created_at, updated_at)
                VALUES (:review_id, '{"safe":"original"}'::jsonb, :now, :now)
            """), {"review_id": review_id, "now": now})

            connection.exec_driver_sql("SET LOCAL ROLE sireader_workforce_cmd")
            locked = connection.execute(text("""
                SELECT review_id FROM public.workforce_task_review_payloads
                WHERE review_id=:review_id FOR UPDATE
            """), {"review_id": review_id}).scalar_one()
            assert locked == review_id

            with pytest.raises(DBAPIError):
                with connection.begin_nested():
                    connection.execute(text("""
                        UPDATE public.workforce_task_review_payloads
                        SET adjusted_output='{"unsafe":"too-early"}'::jsonb, updated_at=:now
                        WHERE review_id=:review_id
                    """), {"review_id": review_id, "now": now})
        finally:
            outer.rollback()
