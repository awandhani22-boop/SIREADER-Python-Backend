"""One-shot workforce retention/expiry maintenance command.

This command never imports or starts a worker provider. Run it from an external
scheduler when the normal worker runtime is disabled.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import datetime
from typing import Any, Callable

from app.workers.repository import (
    expire_deadline_tasks,
    expire_stale_reviews,
    purge_expired_review_payloads,
    purge_expired_task_payloads,
    recover_expired_leases,
)

logger = logging.getLogger(__name__)


def validate_maintenance_database_url(raw_url: str | None) -> None:
    """Allow destructive retention maintenance only against a loopback PostgreSQL DB.

    This intentionally blocks Supabase, production, and all remote database hosts.
    Error messages never include the connection string or credentials.
    """
    if not raw_url or not raw_url.strip():
        raise RuntimeError(
            "DATABASE_URL is not configured; workforce maintenance is disabled."
        )
    try:
        from sqlalchemy.engine import make_url
        parsed = make_url(raw_url.strip())
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
    if host not in {"localhost", "127.0.0.1", "::1"}:
        raise RuntimeError(
            "Workforce maintenance is restricted to localhost/loopback PostgreSQL. "
            "Remote, Supabase, and production databases are blocked."
        )


def run_maintenance_once(
    *,
    session_factory: Callable[[], Any] | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    """Execute one transaction of recovery, expiry, and retention operations."""
    if session_factory is None:
        # Match the main connection module's .env loading semantics, then fail closed
        # before importing/creating the engine if the target is not loopback PostgreSQL.
        from dotenv import load_dotenv
        load_dotenv(encoding="utf-8-sig", override=False)
        validate_maintenance_database_url(os.getenv("DATABASE_URL"))
        from app.database.connection import SessionLocal
        session_factory = SessionLocal

    with session_factory() as db:
        with db.begin():
            recovery = recover_expired_leases(db, now=now)
            deadlines = expire_deadline_tasks(db, now=now)
            stale_reviews = expire_stale_reviews(db, now=now)
            task_payloads = purge_expired_task_payloads(db, now=now)
            review_payloads = purge_expired_review_payloads(db, now=now)

    result = {
        "leases_recovered_requeued": int(recovery.get("requeued", 0)),
        "leases_recovered_waiting_review": int(recovery.get("waiting_review", 0)),
        "leases_recovered_dead_lettered": int(recovery.get("dead_lettered", 0)),
        "deadline_tasks_expired": int(deadlines),
        "stale_reviews_expired": int(stale_reviews),
        "task_payloads_purged": int(task_payloads),
        "review_payloads_purged": int(review_payloads),
    }
    logger.info("WORKFORCE_MAINTENANCE_COMPLETED counts=%s", json.dumps(result, sort_keys=True))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one safe SIREADER workforce maintenance cycle.")
    parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run_maintenance_once()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
