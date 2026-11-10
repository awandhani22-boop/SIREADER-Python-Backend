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

from sqlalchemy import create_engine
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
            "DATABASE_URL is not configured; workforce maintenance is disabled."
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
    if session_factory is None:
        # Use the same .env precedence as the application, but never import its
        # shared Engine/SessionLocal. The maintenance engine is separate and local-only.
        from dotenv import load_dotenv

        load_dotenv(encoding="utf-8-sig", override=False)
        raw_url = os.getenv("DATABASE_URL")
        validate_maintenance_database_url(raw_url)
        assert raw_url is not None
        maintenance_engine, session_factory = _create_maintenance_session_factory(raw_url)

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


if __name__ == "__main__":
    raise SystemExit(main())
