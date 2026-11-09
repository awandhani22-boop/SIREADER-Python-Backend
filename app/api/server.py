"""Application factory dan entry point Uvicorn untuk API Gateway mandiri."""
from collections.abc import Mapping
from contextlib import asynccontextmanager
import asyncio
import logging
import sys
from typing import Any, AsyncIterator

import uvicorn
from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.api.config import api_settings
from app.api.database import ro_session_factory
from app.api.dependencies import verify_bearer_token
from app.api.event_stream import EventHub, PostgresNotifyListener
from app.api.routes import (
    audit_router,
    market_watches_router,
    pipeline_router,
    system_router,
    telemetry_router,
    events_router,
    workforce_router,
)

logger = logging.getLogger(__name__)

APPROVED_VIEW_NAMES = (
    "v_api_system_runtime_state",
    "v_api_telemetry_usage",
    "v_api_active_in_flight_tasks",
    "v_api_queued_pending_messages",
    "v_api_active_market_watches",
    "v_api_audit_decisions",
    "v_api_workforce_workers",
    "v_api_workforce_tasks",
    "v_api_workforce_attempts",
    "v_api_workforce_activity",
    "v_api_workforce_runtime",
)

PRIVILEGE_AUDIT_SQL = """
WITH
base_tables(table_name) AS (
    VALUES
        ('runtime_control_state'),
        ('ai_usage_logs'),
        ('ai_analysis_logs'),
        ('ai_pending_messages'),
        ('trade_decision_watches'),
        ('approval_decisions'),
        ('workforce_workers'),
        ('workforce_capabilities'),
        ('workforce_worker_capabilities'),
        ('workforce_heartbeats'),
        ('workforce_tasks'),
        ('workforce_task_attempts'),
        ('workforce_activity_events')
),
table_privileges(privilege_name) AS (
    VALUES
        ('SELECT'), ('INSERT'), ('UPDATE'), ('DELETE'),
        ('TRUNCATE'), ('REFERENCES'), ('TRIGGER')
),
column_privileges(privilege_name) AS (
    VALUES ('SELECT'), ('INSERT'), ('UPDATE'), ('REFERENCES')
),
view_names(view_name) AS (
    VALUES
        ('v_api_system_runtime_state'),
        ('v_api_telemetry_usage'),
        ('v_api_active_in_flight_tasks'),
        ('v_api_queued_pending_messages'),
        ('v_api_active_market_watches'),
        ('v_api_audit_decisions'),
        ('v_api_workforce_workers'),
        ('v_api_workforce_tasks'),
        ('v_api_workforce_attempts'),
        ('v_api_workforce_activity'),
        ('v_api_workforce_runtime')
),
protected_objects(object_name) AS (
    SELECT table_name FROM base_tables
    UNION ALL
    SELECT view_name FROM view_names
),
public_table_grants AS (
    SELECT EXISTS (
        SELECT 1
        FROM information_schema.table_privileges
        WHERE grantee = 'PUBLIC'
          AND table_schema = 'public'
          AND table_name IN (SELECT object_name FROM protected_objects)
    ) AS has_any
),
public_column_grants AS (
    SELECT EXISTS (
        SELECT 1
        FROM information_schema.column_privileges
        WHERE grantee = 'PUBLIC'
          AND table_schema = 'public'
          AND table_name IN (SELECT object_name FROM protected_objects)
    ) AS has_any
),
view_owner_columns(table_name, column_name) AS (
    VALUES
        ('runtime_control_state', 'execution_mode'),
        ('runtime_control_state', 'trading_mode'),
        ('runtime_control_state', 'auto_execution_enabled'),
        ('runtime_control_state', 'auto_paused'),
        ('runtime_control_state', 'updated_at'),
        ('ai_usage_logs', 'usage_date'),
        ('ai_usage_logs', 'provider'),
        ('ai_usage_logs', 'model'),
        ('ai_usage_logs', 'tier'),
        ('ai_usage_logs', 'input_tokens'),
        ('ai_usage_logs', 'output_tokens'),
        ('ai_usage_logs', 'cache_read_tokens'),
        ('ai_usage_logs', 'cache_write_tokens'),
        ('ai_usage_logs', 'thinking_tokens'),
        ('ai_usage_logs', 'billing_currency'),
        ('ai_usage_logs', 'cost_rp'),
        ('ai_usage_logs', 'reserved_rp'),
        ('ai_usage_logs', 'cost_usd'),
        ('ai_usage_logs', 'reserved_usd'),
        ('ai_usage_logs', 'status'),
        ('ai_usage_logs', 'created_at'),
        ('ai_usage_logs', 'completed_at'),
        ('ai_analysis_logs', 'id'),
        ('ai_analysis_logs', 'status'),
        ('ai_analysis_logs', 'created_at'),
        ('ai_pending_messages', 'id'),
        ('ai_pending_messages', 'status'),
        ('ai_pending_messages', 'reason'),
        ('ai_pending_messages', 'attempts'),
        ('ai_pending_messages', 'claimed_at'),
        ('ai_pending_messages', 'created_at'),
        ('ai_pending_messages', 'updated_at'),
        ('trade_decision_watches', 'id'),
        ('trade_decision_watches', 'symbol'),
        ('trade_decision_watches', 'status'),
        ('trade_decision_watches', 'spread_pips'),
        ('trade_decision_watches', 'maximum_spread_pips'),
        ('trade_decision_watches', 'last_bid'),
        ('trade_decision_watches', 'last_ask'),
        ('trade_decision_watches', 'check_count'),
        ('trade_decision_watches', 'last_checked_at'),
        ('trade_decision_watches', 'next_check_at'),
        ('trade_decision_watches', 'created_at'),
        ('trade_decision_watches', 'updated_at'),
        ('approval_decisions', 'id'),
        ('approval_decisions', 'decision'),
        ('approval_decisions', 'previous_status'),
        ('approval_decisions', 'new_status'),
        ('approval_decisions', 'created_at'),
        ('workforce_workers', 'worker_id'),
        ('workforce_workers', 'display_name'),
        ('workforce_workers', 'role'),
        ('workforce_workers', 'department'),
        ('workforce_workers', 'enabled'),
        ('workforce_workers', 'max_concurrency'),
        ('workforce_workers', 'model_tier'),
        ('workforce_workers', 'schema_version'),
        ('workforce_workers', 'created_at'),
        ('workforce_workers', 'updated_at'),
        ('workforce_heartbeats', 'worker_id'),
        ('workforce_heartbeats', 'process_state'),
        ('workforce_heartbeats', 'heartbeat_at'),
        ('workforce_heartbeats', 'active_task_count'),
        ('workforce_heartbeats', 'in_flight_provider_requests'),
        ('workforce_tasks', 'task_id'),
        ('workforce_tasks', 'correlation_id'),
        ('workforce_tasks', 'schema_version'),
        ('workforce_tasks', 'task_type'),
        ('workforce_tasks', 'title'),
        ('workforce_tasks', 'required_capability'),
        ('workforce_tasks', 'requested_worker_id'),
        ('workforce_tasks', 'assigned_worker_id'),
        ('workforce_tasks', 'priority'),
        ('workforce_tasks', 'status'),
        ('workforce_tasks', 'attempts'),
        ('workforce_tasks', 'max_attempts'),
        ('workforce_tasks', 'not_before'),
        ('workforce_tasks', 'deadline_at'),
        ('workforce_tasks', 'lease_expires_at'),
        ('workforce_tasks', 'failure_code'),
        ('workforce_tasks', 'created_at'),
        ('workforce_tasks', 'updated_at'),
        ('workforce_tasks', 'completed_at'),
        ('workforce_task_attempts', 'attempt_id'),
        ('workforce_task_attempts', 'task_id'),
        ('workforce_task_attempts', 'attempt_number'),
        ('workforce_task_attempts', 'worker_id'),
        ('workforce_task_attempts', 'status'),
        ('workforce_task_attempts', 'billing_currency'),
        ('workforce_task_attempts', 'cost_amount'),
        ('workforce_task_attempts', 'error_code'),
        ('workforce_task_attempts', 'started_at'),
        ('workforce_task_attempts', 'finished_at'),
        ('workforce_task_attempts', 'created_at'),
        ('workforce_activity_events', 'event_id'),
        ('workforce_activity_events', 'event_type'),
        ('workforce_activity_events', 'occurred_at'),
        ('workforce_activity_events', 'worker_id'),
        ('workforce_activity_events', 'task_id'),
        ('workforce_activity_events', 'attempt_id'),
        ('workforce_activity_events', 'correlation_id')
),
view_rows AS (
    SELECT
        c.relname,
        owner_role.rolname AS owner_name,
        owner_role.rolcanlogin AS owner_can_login,
        owner_role.rolsuper AS owner_is_superuser,
        owner_role.rolinherit AS owner_inherits,
        owner_role.rolcreatedb AS owner_can_createdb,
        owner_role.rolcreaterole AS owner_can_createrole,
        owner_role.rolreplication AS owner_is_replication,
        owner_role.rolbypassrls AS owner_bypasses_rls,
        has_table_privilege(current_user, c.oid, 'SELECT') AS api_can_select
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    JOIN pg_roles owner_role ON owner_role.oid = c.relowner
    WHERE n.nspname = 'public'
      AND c.relkind = 'v'
      AND c.relname IN (SELECT view_name FROM view_names)
),
api_view_write_access AS (
    SELECT EXISTS (
        SELECT 1
        FROM view_names v
        CROSS JOIN table_privileges p
        WHERE p.privilege_name <> 'SELECT'
          AND has_table_privilege(
              current_user,
              format('public.%I', v.view_name),
              p.privilege_name
          )
    ) AS has_any
),
api_view_column_write_access AS (
    SELECT EXISTS (
        SELECT 1
        FROM view_names v
        JOIN pg_class c ON c.relname = v.view_name
        JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = 'public'
        JOIN pg_attribute a ON a.attrelid = c.oid
        CROSS JOIN (VALUES ('INSERT'), ('UPDATE'), ('REFERENCES')) AS p(privilege_name)
        WHERE a.attnum > 0
          AND NOT a.attisdropped
          AND has_column_privilege(current_user, c.oid, a.attnum, p.privilege_name)
    ) AS has_any
),
api_base_table_access AS (
    SELECT EXISTS (
        SELECT 1
        FROM base_tables b
        CROSS JOIN table_privileges p
        WHERE has_table_privilege(
            current_user,
            format('public.%I', b.table_name),
            p.privilege_name
        )
    ) AS has_any
),
api_base_column_access AS (
    SELECT EXISTS (
        SELECT 1
        FROM pg_class t
        JOIN pg_namespace n ON n.oid = t.relnamespace
        JOIN pg_attribute a ON a.attrelid = t.oid
        CROSS JOIN column_privileges p
        WHERE n.nspname = 'public'
          AND t.relname IN (SELECT table_name FROM base_tables)
          AND a.attnum > 0
          AND NOT a.attisdropped
          AND has_column_privilege(current_user, t.oid, a.attnum, p.privilege_name)
    ) AS has_any
),
api_base_table_maintain_access AS (
    SELECT CASE
        WHEN current_setting('server_version_num')::integer >= 180000 THEN EXISTS (
            SELECT 1
            FROM base_tables b
            WHERE has_table_privilege(
                current_user,
                format('public.%I', b.table_name),
                'MAINTAIN'
            )
        )
        ELSE FALSE
    END AS has_any
),
owner_base_table_access AS (
    SELECT EXISTS (
        SELECT 1
        FROM base_tables b
        CROSS JOIN table_privileges p
        WHERE has_table_privilege(
            'sireader_view_owner',
            format('public.%I', b.table_name),
            p.privilege_name
        )
    ) AS has_any
),
owner_base_table_maintain_access AS (
    SELECT CASE
        WHEN current_setting('server_version_num')::integer >= 180000 THEN EXISTS (
            SELECT 1
            FROM base_tables b
            WHERE has_table_privilege(
                'sireader_view_owner',
                format('public.%I', b.table_name),
                'MAINTAIN'
            )
        )
        ELSE FALSE
    END AS has_any
),
owner_has_role_memberships AS (
    SELECT EXISTS (
        SELECT 1
        FROM pg_auth_members am
        WHERE (
            am.member = (SELECT oid FROM pg_roles WHERE rolname = 'sireader_view_owner')
            OR am.roleid = (SELECT oid FROM pg_roles WHERE rolname = 'sireader_view_owner')
        )
          -- PostgreSQL 16+ may retain an ADMIN-only creator membership with
          -- INHERIT=false and SET=false. It conveys no runtime privileges.
          AND (am.inherit_option OR am.set_option)
    ) AS has_any
),
owner_unapproved_column_access AS (
    SELECT EXISTS (
        SELECT 1
        FROM pg_class t
        JOIN pg_namespace n ON n.oid = t.relnamespace
        JOIN pg_attribute a ON a.attrelid = t.oid
        CROSS JOIN column_privileges p
        WHERE n.nspname = 'public'
          AND t.relname IN (SELECT table_name FROM base_tables)
          AND a.attnum > 0
          AND NOT a.attisdropped
          AND has_column_privilege('sireader_view_owner', t.oid, a.attnum, p.privilege_name)
          AND NOT (
              p.privilege_name = 'SELECT'
              AND EXISTS (
                  SELECT 1
                  FROM view_owner_columns expected
                  WHERE expected.table_name = t.relname
                    AND expected.column_name = a.attname
              )
          )
    ) AS has_any
),
owner_column_access AS (
    SELECT
        COUNT(*) AS expected_columns,
        COUNT(*) FILTER (
            WHERE t.oid IS NOT NULL
              AND a.attnum IS NOT NULL
              AND has_column_privilege('sireader_view_owner', t.oid, a.attnum, 'SELECT')
        ) AS granted_columns
    FROM view_owner_columns expected
    LEFT JOIN pg_namespace n ON n.nspname = 'public'
    LEFT JOIN pg_class t ON t.relnamespace = n.oid AND t.relname = expected.table_name
    LEFT JOIN pg_attribute a ON a.attrelid = t.oid
                           AND a.attname = expected.column_name
                           AND a.attnum > 0
                           AND NOT a.attisdropped
),
view_stats AS (
    SELECT
        COUNT(*) AS view_count,
        COALESCE(BOOL_AND(owner_name = 'sireader_view_owner'), FALSE) AS owners_ok,
        COALESCE(BOOL_AND(api_can_select), FALSE) AS api_select_ok,
        COALESCE(BOOL_AND(
            NOT owner_can_login
            AND NOT owner_is_superuser
            AND NOT owner_inherits
            AND NOT owner_can_createdb
            AND NOT owner_can_createrole
            AND NOT owner_is_replication
            AND NOT owner_bypasses_rls
        ), FALSE) AS owner_attributes_ok
    FROM view_rows
)
SELECT
    has_schema_privilege(current_user, 'public', 'USAGE') AS api_schema_usage,
    has_schema_privilege(current_user, 'public', 'CREATE') AS api_schema_create,
    EXISTS (
        SELECT 1
        FROM pg_auth_members am
        WHERE (
            am.member = (SELECT oid FROM pg_roles WHERE rolname = current_user)
            OR am.roleid = (SELECT oid FROM pg_roles WHERE rolname = current_user)
        )
          -- Ignore only the non-inheriting, non-SET creator admin linkage.
          AND (am.inherit_option OR am.set_option)
    ) AS api_has_role_memberships,
    (SELECT has_any FROM api_view_write_access) AS api_view_write_access,
    (SELECT has_any FROM api_view_column_write_access) AS api_view_column_write_access,
    (SELECT has_any FROM api_base_table_access) AS api_base_table_access,
    (SELECT has_any FROM api_base_table_maintain_access) AS api_base_table_maintain,
    (SELECT has_any FROM api_base_column_access) AS api_base_column_access,
    (SELECT has_any FROM public_table_grants) AS public_table_grants,
    (SELECT has_any FROM public_column_grants) AS public_column_grants,
    has_schema_privilege('sireader_view_owner', 'public', 'CREATE') AS owner_schema_create,
    (SELECT has_any FROM owner_base_table_access) AS owner_base_table_access,
    (SELECT has_any FROM owner_base_table_maintain_access) AS owner_base_table_maintain,
    (SELECT has_any FROM owner_has_role_memberships) AS owner_has_role_memberships,
    (SELECT has_any FROM owner_unapproved_column_access) AS owner_unapproved_column_access,
    (SELECT granted_columns = expected_columns FROM owner_column_access) AS owner_column_allowlist_ok,
    (SELECT view_count FROM view_stats) AS approved_view_count,
    (SELECT owners_ok FROM view_stats) AS approved_view_owners_ok,
    (SELECT api_select_ok FROM view_stats) AS approved_view_select_ok,
    (SELECT owner_attributes_ok FROM view_stats) AS view_owner_attributes_ok
"""


def validate_local_bind_host(host: str) -> str:
    if host != "127.0.0.1":
        raise RuntimeError("API Gateway lokal hanya boleh bind ke 127.0.0.1.")
    return host


def validate_ro_database_identity(identity: Mapping[str, Any] | None) -> None:
    """Fail closed unless the DB connection uses the dedicated least-privilege role."""
    if identity is None:
        raise RuntimeError("Cannot identify the API database role.")
    if identity.get("role_name") != "sireader_api_ro":
        raise RuntimeError(
            "SIREADER_API_DATABASE_URL must authenticate specifically as sireader_api_ro."
        )
    if identity.get("transaction_read_only") not in {"on", True}:
        raise RuntimeError("PostgreSQL connection is not in default read-only transaction mode.")

    required_false = (
        "rolsuper", "rolinherit", "rolcreatedb",
        "rolcreaterole", "rolreplication", "rolbypassrls",
    )
    if identity.get("rolcanlogin") is not True:
        raise RuntimeError("sireader_api_ro must have LOGIN.")
    if any(identity.get(field) is not False for field in required_false):
        raise RuntimeError(
            "sireader_api_ro role attributes violate the required least-privilege policy."
        )


def validate_ro_database_privileges(snapshot: Mapping[str, Any] | None) -> None:
    """Fail closed unless the six-view / zero-base-table access boundary is proven."""
    if snapshot is None:
        raise RuntimeError("Cannot verify effective API database privileges.")

    if snapshot.get("api_schema_usage") is not True:
        raise RuntimeError("sireader_api_ro requires USAGE on schema public.")
    if snapshot.get("api_schema_create") is not False:
        raise RuntimeError("sireader_api_ro must not have CREATE on schema public.")
    if snapshot.get("api_view_write_access") is not False:
        raise RuntimeError("sireader_api_ro must not have table-level write privileges on approved views.")
    if snapshot.get("api_view_column_write_access") is not False:
        raise RuntimeError("sireader_api_ro must not have column-level write privileges on approved views.")
    if snapshot.get("api_has_role_memberships") is not False:
        raise RuntimeError("sireader_api_ro must not have memberships that grant INHERIT or SET ROLE.")
    if snapshot.get("api_base_table_access") is not False:
        raise RuntimeError("sireader_api_ro has an effective privilege on a protected base table.")
    if snapshot.get("api_base_table_maintain") is not False:
        raise RuntimeError("sireader_api_ro must not have MAINTAIN on protected base tables.")
    if snapshot.get("api_base_column_access") is not False:
        raise RuntimeError("sireader_api_ro has an effective privilege on a protected base-table column.")
    if snapshot.get("public_table_grants") is not False:
        raise RuntimeError("PUBLIC has table-level privileges on a protected base table or API view.")
    if snapshot.get("public_column_grants") is not False:
        raise RuntimeError("PUBLIC has column-level privileges on a protected base table or API view.")
    if snapshot.get("owner_schema_create") is not False:
        raise RuntimeError("sireader_view_owner must not retain CREATE on schema public.")
    if snapshot.get("owner_base_table_access") is not False:
        raise RuntimeError("sireader_view_owner must use only column-level SELECT, not table-level privileges.")
    if snapshot.get("owner_base_table_maintain") is not False:
        raise RuntimeError("sireader_view_owner must not have MAINTAIN on protected base tables.")
    if snapshot.get("owner_has_role_memberships") is not False:
        raise RuntimeError("sireader_view_owner must not have role memberships that grant INHERIT or SET ROLE.")
    if snapshot.get("owner_unapproved_column_access") is not False:
        raise RuntimeError("sireader_view_owner has privileges on unapproved base-table columns.")
    if snapshot.get("owner_column_allowlist_ok") is not True:
        raise RuntimeError("sireader_view_owner lacks one or more approved base-table column grants.")
    if snapshot.get("approved_view_count") != 11:
        raise RuntimeError("All eleven approved API views must exist before the API can start.")
    if snapshot.get("approved_view_owners_ok") is not True:
        raise RuntimeError("All approved API views must be owned by sireader_view_owner.")
    if snapshot.get("approved_view_select_ok") is not True:
        raise RuntimeError("sireader_api_ro must have SELECT on all six approved API views.")
    if snapshot.get("view_owner_attributes_ok") is not True:
        raise RuntimeError("sireader_view_owner role attributes violate the least-privilege policy.")


def validate_approved_views_executable(session: Any) -> None:
    """Execute bounded read-only probes against all approved views as the API login role."""
    for view_name in APPROVED_VIEW_NAMES:
        # view_name is selected only from the module-level allowlist above.
        session.execute(text(f"SELECT 1 FROM public.{view_name} LIMIT 1")).first()


def _validate_live_database_session() -> None:
    """Verify role attributes, connection mode, effective grants and view ownership before serving requests."""

    session = ro_session_factory.get_session()
    try:
        identity = session.execute(
            text(
                """
                SELECT
                    current_user AS role_name,
                    current_setting('transaction_read_only') AS transaction_read_only,
                    r.rolcanlogin,
                    r.rolsuper,
                    r.rolinherit,
                    r.rolcreatedb,
                    r.rolcreaterole,
                    r.rolreplication,
                    r.rolbypassrls
                FROM pg_roles AS r
                WHERE r.rolname = current_user
                """
            )
        ).mappings().one_or_none()
        validate_ro_database_identity(identity)
        privileges = session.execute(text(PRIVILEGE_AUDIT_SQL)).mappings().one_or_none()
        validate_ro_database_privileges(privileges)
        validate_approved_views_executable(session)
    finally:
        session.close()


def create_app(*, manage_database_lifecycle: bool = True) -> FastAPI:
    """Lifecycle dinonaktifkan hanya untuk test yang mengganti dependency DB."""

    event_hub = EventHub()
    listener: PostgresNotifyListener | None = None

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        nonlocal listener
        initialized = False
        try:
            if manage_database_lifecycle:
                validate_local_bind_host(api_settings.API_HOST)
                ro_session_factory.initialize()
                initialized = True
                _validate_live_database_session()
                listener = PostgresNotifyListener(
                    event_hub,
                    api_settings.get_api_database_url,
                )
                listener.start()
            yield
        finally:
            if listener is not None:
                await listener.stop()
                listener = None
            else:
                event_hub.close()
            if manage_database_lifecycle and initialized:
                ro_session_factory.close()

    app = FastAPI(
        title="SIREADER Read-Only Observability API",
        version="3.1.0",
        docs_url="/docs",
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.event_hub = event_hub
    app.add_middleware(
        CORSMiddleware,
        allow_origins=api_settings.cors_origins_list,
        allow_credentials=False,
        allow_methods=["GET"],
        allow_headers=["Authorization", "Content-Type"],
    )

    @app.exception_handler(SQLAlchemyError)
    def database_error_handler(_: Request, exc: SQLAlchemyError) -> JSONResponse:
        logger.error("SIREADER API database operation failed: %s", type(exc).__name__)
        return JSONResponse(status_code=503, content={"detail": "Read-only database is unavailable."})

    @app.exception_handler(RuntimeError)
    def configuration_error_handler(_: Request, exc: RuntimeError) -> JSONResponse:
        logger.error("SIREADER API configuration/lifecycle error: %s", str(exc))
        return JSONResponse(status_code=503, content={"detail": "API Gateway configuration is unavailable."})

    for route in (
        system_router,
        telemetry_router,
        pipeline_router,
        market_watches_router,
        audit_router,
        events_router,
        workforce_router,
    ):
        app.include_router(route, dependencies=[Depends(verify_bearer_token)])
    return app


app = create_app()


def run() -> None:
    host = validate_local_bind_host(api_settings.API_HOST)
    if not 1 <= api_settings.API_PORT <= 65535:
        raise RuntimeError("API_PORT harus berada pada rentang 1..65535.")

    if sys.platform == "win32":
        # Run Uvicorn on an explicitly-created SelectorEventLoop. Setting only
        # the global policy is not sufficient when a runner selects its own loop.
        loop = asyncio.SelectorEventLoop()
        asyncio.set_event_loop(loop)
        logger.warning(
            "SSE_LOOP_SELECTED loop_type=%s",
            type(loop).__name__,
        )
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host=host,
                port=api_settings.API_PORT,
                workers=1,
                loop="asyncio",
            )
        )
        try:
            loop.run_until_complete(server.serve())
        finally:
            if not loop.is_closed():
                loop.run_until_complete(loop.shutdown_asyncgens())
                loop.close()
            asyncio.set_event_loop(None)
        return

    uvicorn.run(app, host=host, port=api_settings.API_PORT, workers=1)


if __name__ == "__main__":
    run()
