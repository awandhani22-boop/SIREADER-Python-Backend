"""One-shot workforce retention/expiry maintenance command.

This command never imports or starts a worker provider. Run it from an external
scheduler when the normal worker runtime is disabled.
"""
from __future__ import annotations

import argparse
import json
import logging
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


def run_maintenance_once(
    *,
    session_factory: Callable[[], Any] | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    """Execute one transaction of recovery, expiry, and retention operations."""
    if session_factory is None:
        # Importing the DB factory here keeps module import safe for API/test tooling.
        # It uses DATABASE_URL only when the maintenance command is actually invoked.
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
