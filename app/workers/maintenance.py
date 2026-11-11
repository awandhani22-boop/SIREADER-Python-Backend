"""One-shot workforce retention/expiry maintenance command.

This command never imports or starts a worker provider. It uses a dedicated,
short-lived SQLAlchemy engine and fails closed unless the target is loopback
PostgreSQL with no libpq host/service override.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import datetime
from typing import Any, Callable, Mapping

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.workers.repository import (
    expire_deadline_tasks,
    expire_stale_reviews,
    purge_expired_review_payloads,
    purge_expired_task_payloads,
    recover_expired_leases,
)

logger = logging.getLogger(__name__)

# libpq environment variables that can redirect a connection or select a
# service/target independently of the URL. Refuse all of them for destructive
# maintenance, even when the URL itself names localhost.
BLOCKED_LIBPQ_OVERRIDE_ENV_VARS = frozenset({
    "PGHOST",
    "PGHOSTADDR",
    "PGPORT",
    "PGSERVICE",
    "PGSERVICEFILE",
    "PGLOADBALANCEHOSTS",
    "PGTARGETSESSIONATTRS",
    "PGSSLMODE",
})


def validate_maintenance_database_url(
    raw_url: str | None,
    *,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Allow maintenance only for an explicit, unambiguous loopback PostgreSQL URL.

    Query parameters are rejected because libpq options such as host, hostaddr,
    service, servicefile, target_session_attrs, or load_balance_hosts can alter
    where a URL that appears local actually connects. Error messages never
    include connection strings or credentials.
    """
    if not raw_url or not raw_url.strip():
        raise RuntimeError(
            "SIREADER_WORKFORCE_MAINTENANCE_DATABASE_URL is not configured; workforce maintenance is disabled."
        )

    environment = os.environ if environ is None else environ
    normalized_environment = {str(key).upper(): value for key, value in environment.items()}
    active_overrides = sorted(
        name
        for name in BLOCKED_LIBPQ_OVERRIDE_ENV_VARS
        if normalized_environment.get(name)
    )
    if active_overrides:
        raise RuntimeError(
            "PostgreSQL connection override environment variables are set; "
            "workforce maintenance is disabled."
        )

    candidate = raw_url.strip()
    if "#" in candidate:
        raise RuntimeError(
            "DATABASE_URL fragments are not supported; workforce maintenance is disabled."
        )
    try:
        parsed = make_url(candidate)
    except Exception as exc:
        raise RuntimeError(
            "DATABASE_URL could not be parsed; workforce maintenance is disabled."
        ) from exc

    driver = parsed.drivername.split("+", 1)[0].lower()
    host = (parsed.host or "").strip("[]").lower()
    if driver not in {"postgres", "postgresql"}:
        raise RuntimeError(
            "Workforce maintenance requires PostgreSQL on a local loopback host."
        )
    if parsed.query:
        raise RuntimeError(
            "DATABASE_URL query parameters are not supported for workforce maintenance."
        )
    if not parsed.database:
        raise RuntimeError(
            "DATABASE_URL must name an explicit local database; workforce maintenance is disabled."
        )
    if host not in {"localhost", "127.0.0.1", "::1"}:
        raise RuntimeError(
            "Workforce maintenance is restricted to localhost/loopback PostgreSQL. "
            "Remote, Supabase, and production databases are blocked."
        )


def validate_maintenance_database_identity(identity: Mapping[str, Any]) -> None:
    """Require the dedicated, non-privileged role on the exact local disposable DB."""
    expected = {
        "database_name": "sireader_disposable",
        "role_name": "sireader_workforce_maint",
        "server_host": "127.0.0.1",
        "server_port": 5432,
    }
    for key, value in expected.items():
        if identity.get(key) != value:
            raise RuntimeError(
                "Workforce maintenance requires the dedicated maintenance role "
                "on loopback sireader_disposable."
            )
    forbidden_flags = (
        "is_superuser",
        "can_create_db",
        "can_create_role",
        "can_replicate",
        "bypasses_rls",
        "inherits_roles",
    )
    if any(bool(identity.get(flag)) for flag in forbidden_flags):
        raise RuntimeError(
            "Workforce maintenance role violates the non-privileged role contract."
        )


def _create_maintenance_session_factory(raw_url: str):
    """Create an isolated one-connection engine for the validated local target."""
    validate_maintenance_database_url(raw_url)
    database_url = make_url(raw_url.strip())
    engine = create_engine(
        database_url,
        pool_pre_ping=True,
        pool_size=1,
        max_overflow=0,
        pool_timeout=5,
        connect_args={"connect_timeout": 5},
    )
    try:
        with engine.connect() as connection:
            identity = connection.execute(text("""
                SELECT
                    current_database() AS database_name,
                    current_user AS role_name,
                    host(inet_server_addr()) AS server_host,
                    inet_server_port() AS server_port,
                    r.rolsuper AS is_superuser,
                    r.rolcreatedb AS can_create_db,
                    r.rolcreaterole AS can_create_role,
                    r.rolreplication AS can_replicate,
                    r.rolbypassrls AS bypasses_rls,
                    r.rolinherit AS inherits_roles
                FROM pg_catalog.pg_roles r
                WHERE r.rolname = current_user
            """)).mappings().one()
            validate_maintenance_database_identity(identity)
    except Exception:
        engine.dispose()
        raise
    factory = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )
    return engine, factory


def run_maintenance_once(
    *,
    session_factory: Callable[[], Any] | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    """Execute one transaction of recovery, expiry, and retention operations."""
    maintenance_engine = None
    function_result = None
    if session_factory is None:
        # Function mode is selected only by its separate, dedicated URL.
        # If present, it never falls back to the legacy ORM/local URL.
        from dotenv import load_dotenv

        load_dotenv(encoding="utf-8-sig", override=False)
        function_url = os.getenv("SIREADER_WORKFORCE_MAINTENANCE_FUNCTION_DATABASE_URL")
        if function_url:
            function_result = _run_database_maintenance_function(function_url, now=now)
        else:
            # The legacy path remains strictly local-only and is never allowed
            # to use a remote Supabase or production database.
            raw_url = os.getenv("SIREADER_WORKFORCE_MAINTENANCE_DATABASE_URL")
            validate_maintenance_database_url(raw_url)
            assert raw_url is not None
            maintenance_engine, session_factory = _create_maintenance_session_factory(raw_url)

    if function_result is None:
        try:
            with session_factory() as db:
                with db.begin():
                    recovery = recover_expired_leases(db, now=now)
                    deadlines = expire_deadline_tasks(db, now=now)
                    stale_reviews = expire_stale_reviews(db, now=now)
                    task_payloads = purge_expired_task_payloads(db, now=now)
                    review_payloads = purge_expired_review_payloads(db, now=now)
        finally:
            if maintenance_engine is not None:
                maintenance_engine.dispose()

        result = {
            "leases_recovered_requeued": int(recovery.get("requeued", 0)),
            "leases_recovered_waiting_review": int(recovery.get("waiting_review", 0)),
            "leases_recovered_dead_lettered": int(recovery.get("dead_lettered", 0)),
            "deadline_tasks_expired": int(deadlines),
            "stale_reviews_expired": int(stale_reviews),
            "task_payloads_purged": int(task_payloads),
            "review_payloads_purged": int(review_payloads),
        }
    else:
        result = function_result
    logger.info(
        "WORKFORCE_MAINTENANCE_COMPLETED counts=%s",
        json.dumps(result, sort_keys=True),
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one safe SIREADER workforce maintenance cycle.")
    parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run_maintenance_once()
    return 0

def validate_function_maintenance_database_url(
    raw_url: str | None, *, environ: Mapping[str, str] | None = None
):
    """Validate and normalize the dedicated database-function maintenance URL."""
    if not raw_url or not raw_url.strip():
        raise RuntimeError("SIREADER_WORKFORCE_MAINTENANCE_FUNCTION_DATABASE_URL is not configured.")
    environment = os.environ if environ is None else environ
    normalized_environment = {str(key).upper(): value for key, value in environment.items()}
    if any(normalized_environment.get(name) for name in BLOCKED_LIBPQ_OVERRIDE_ENV_VARS):
        raise RuntimeError("PostgreSQL connection override environment variables are set; function maintenance is disabled.")
    candidate = raw_url.strip()
    if "#" in candidate:
        raise RuntimeError("Maintenance database URL fragments are not supported.")
    try:
        parsed = make_url(candidate)
    except Exception as exc:
        raise RuntimeError("Maintenance function database URL is invalid.") from exc
    if parsed.drivername == "postgresql":
        parsed = parsed.set(drivername="postgresql+psycopg")
    if parsed.drivername != "postgresql+psycopg":
        raise RuntimeError("Maintenance function database requires PostgreSQL with psycopg 3.")
    username = parsed.username or ""
    if not (username == "sireader_workforce_maint" or username.startswith("sireader_workforce_maint.")):
        raise RuntimeError("Maintenance function database must authenticate as sireader_workforce_maint.")
    if not parsed.password or not parsed.database:
        raise RuntimeError("Maintenance function database requires a password and explicit database.")
    forbidden = {
        "host", "hostaddr", "service", "servicefile",
        "target_session_attrs", "load_balance_hosts",
    }
    if forbidden.intersection(str(key).lower() for key in parsed.query):
        raise RuntimeError("Maintenance function database URL contains a forbidden connection override.")
    if set(str(key).lower() for key in parsed.query) - {"sslmode"}:
        raise RuntimeError("Only the sslmode URL option is permitted for maintenance function connections.")
    host = (parsed.host or "").strip("[]").lower()
    if host not in {"localhost", "127.0.0.1", "::1"}:
        if str(parsed.query.get("sslmode", "")).lower() not in {"require", "verify-ca", "verify-full"}:
            raise RuntimeError("Remote maintenance function connections require TLS.")
    return parsed


def _create_function_maintenance_engine(raw_url: str):
    """Create a distinct engine for the bounded database-function maintenance API."""
    parsed = validate_function_maintenance_database_url(raw_url)
    engine = create_engine(
        parsed,
        pool_pre_ping=True,
        pool_size=1,
        max_overflow=0,
        pool_timeout=5,
        connect_args={"connect_timeout": 5},
    )
    try:
        with engine.connect() as connection:
            identity = connection.execute(text('''
                SELECT
                    current_database() AS database_name,
                    current_user AS role_name,
                    pg_catalog.split_part(session_user::text,'.',1) AS normalized_session_role,
                    r.rolcanlogin,r.rolsuper,r.rolinherit,r.rolcreatedb,
                    r.rolcreaterole,r.rolreplication,r.rolbypassrls,
                    has_schema_privilege(current_user,'sireader_workforce_private','USAGE') AS private_usage,
                    has_schema_privilege(current_user,'sireader_workforce_private','CREATE') AS private_create,
                    (SELECT count(*) FROM pg_catalog.pg_auth_members m WHERE m.member=r.oid) AS membership_count,
                    (SELECT count(*) FROM pg_catalog.pg_class c
                     JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
                     WHERE n.nspname='public' AND c.relname LIKE 'workforce_%'
                       AND c.relkind IN ('r','p') AND NOT c.relrowsecurity) AS workforce_rls_missing,
                    EXISTS (
                      SELECT 1 FROM pg_catalog.pg_class c
                      JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
                      WHERE n.nspname='public' AND c.relname LIKE 'workforce_%'
                        AND c.relkind IN ('r','p')
                        AND (
                          has_table_privilege(current_user,c.oid,'SELECT')
                          OR has_table_privilege(current_user,c.oid,'INSERT')
                          OR has_table_privilege(current_user,c.oid,'UPDATE')
                          OR has_table_privilege(current_user,c.oid,'DELETE')
                          OR has_table_privilege(current_user,c.oid,'TRUNCATE')
                          OR has_table_privilege(current_user,c.oid,'REFERENCES')
                          OR has_table_privilege(current_user,c.oid,'TRIGGER')
                        )
                    ) AS direct_workforce_access,
                    to_regprocedure('sireader_workforce_private.maintenance_run_once_v1(integer,timestamptz)') IS NOT NULL AS function_present,
                    has_function_privilege(current_user,
                      'sireader_workforce_private.maintenance_run_once_v1(integer,timestamptz)','EXECUTE') AS function_execute,
                    EXISTS (
                      SELECT 1
                      FROM pg_catalog.pg_proc p
                      JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace
                      JOIN pg_catalog.pg_roles owner ON owner.oid=p.proowner
                      WHERE n.nspname='sireader_workforce_private'
                        AND p.oid='sireader_workforce_private.maintenance_run_once_v1(integer,timestamptz)'::regprocedure
                        AND p.prosecdef
                        AND 'search_path=pg_catalog'=ANY(COALESCE(p.proconfig,'{}'::text[]))
                        AND owner.rolcanlogin=false AND owner.rolsuper=false
                        AND owner.rolinherit=false AND owner.rolbypassrls=false
                        AND NOT EXISTS (
                          SELECT 1 FROM pg_catalog.aclexplode(
                            COALESCE(p.proacl,pg_catalog.acldefault('f',p.proowner))
                          ) acl WHERE acl.grantee=0 AND acl.privilege_type='EXECUTE'
                        )
                    ) AS function_definition_safe
                FROM pg_catalog.pg_roles r
                WHERE r.rolname=current_user
            ''')).mappings().one_or_none()
            safe = identity is not None and (
                identity["role_name"] == "sireader_workforce_maint"
                and identity["normalized_session_role"] == "sireader_workforce_maint"
                and identity["rolcanlogin"] is True
                and identity["rolsuper"] is False
                and identity["rolinherit"] is False
                and identity["rolcreatedb"] is False
                and identity["rolcreaterole"] is False
                and identity["rolreplication"] is False
                and identity["rolbypassrls"] is False
                and identity["private_usage"] is True
                and identity["private_create"] is False
                and int(identity["membership_count"]) == 0
                and int(identity["workforce_rls_missing"]) == 0
                and identity["direct_workforce_access"] is False
                and identity["function_present"] is True
                and identity["function_execute"] is True
                and identity["function_definition_safe"] is True
            )
            if not safe:
                raise RuntimeError("Maintenance function identity violates its database role/function-only contract.")
    except Exception:
        engine.dispose()
        raise
    return engine


def _run_database_maintenance_function(
    raw_url: str,
    *,
    now: datetime | None = None,
    batch_size: int = 100,
) -> dict[str, int]:
    if now is not None:
        raise ValueError("Database-function maintenance uses database time; caller-supplied timestamps are forbidden.")
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or not 1 <= batch_size <= 500:
        raise ValueError("Workforce maintenance function batch size must be between 1 and 500.")
    engine = _create_function_maintenance_engine(raw_url)
    try:
        with engine.begin() as connection:
            raw_result = connection.execute(text('''
                SELECT sireader_workforce_private.maintenance_run_once_v1(:batch_size,NULL)
            '''), {"batch_size": batch_size}).scalar_one()
        result = json.loads(raw_result) if isinstance(raw_result, str) else dict(raw_result)
    finally:
        engine.dispose()
    return {
        "leases_recovered_requeued": int(result.get("leases_recovered_requeued", 0)),
        "leases_recovered_waiting_review": int(result.get("leases_recovered_waiting_review", 0)),
        "leases_recovered_dead_lettered": int(result.get("leases_recovered_dead_lettered", 0)),
        "deadline_tasks_expired": int(result.get("deadline_tasks_expired", 0)),
        "stale_reviews_expired": int(result.get("stale_reviews_expired", 0)),
        "task_payloads_purged": int(result.get("task_payloads_purged", 0)),
        "review_payloads_purged": int(result.get("review_payloads_purged", 0)),
    }


if __name__ == "__main__":
    raise SystemExit(main())
