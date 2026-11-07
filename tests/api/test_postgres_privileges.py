"""PostgreSQL integration test for the six-view security boundary.

This test runs only against an explicitly supplied disposable CI database.
It never connects to Supabase or to the user's production database.
"""

import os

import pytest
from sqlalchemy import create_engine, text
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
        connection.exec_driver_sql("DROP ROLE IF EXISTS sireader_api_ro")
        connection.exec_driver_sql("DROP ROLE IF EXISTS sireader_view_owner")

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
                text(f"SELECT * FROM public.{view_name} LIMIT 2")
            ).mappings().all()
            assert len(rows) == 1, f"{view_name} did not return the permitted RLS row"

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
