"""Real database LOGIN identity smoke test for the local cutover rehearsal.

Hard-pinned to local PG17 port 55450. No password is provisioned: the isolated
loopback-only rehearsal HBA is configured for trust. URL password strings are
placeholders to exercise the application's required-URL validation contract.
All database command and maintenance mutations run inside transactions that
are explicitly rolled back.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
import sys

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.command_api.config import WorkforceCommandSettings
from app.command_api.database_commands import (
    cancel_task,
    enqueue_task,
    get_pending_review,
    set_worker_scheduling,
)
from app.command_api.server import create_command_app
from app.workers import maintenance


DB = "sireader_cutover_rehearsal_20261111"
TOKEN = "SIREADER-CUTOVER-ROLE-SMOKE-TOKEN-0123456789"


def url(role: str) -> str:
    # Placeholder only; the isolated test cluster authenticates via local-only trust.
    return f"postgresql+psycopg://{role}:local-placeholder@127.0.0.1:55450/{DB}"


async def main() -> None:
    settings = WorkforceCommandSettings(
        SIREADER_WORKFORCE_COMMAND_DATABASE_URL=url("sireader_workforce_cmd"),
        SIREADER_WORKFORCE_REVIEW_DATABASE_URL=url("sireader_workforce_review"),
        SIREADER_WORKFORCE_COMMAND_AUTH_TOKEN=TOKEN,
        WORKFORCE_COMMAND_HOST="127.0.0.1",
        WORKFORCE_COMMAND_PORT=8010,
    )
    app = create_command_app(settings=settings, validate_database=True)
    async with app.router.lifespan_context(app):
        command_factory = app.state.session_factory
        review_factory = app.state.review_session_factory
        assert app.state.require_dedicated_review_identity is True

        with command_factory() as db:
            identity = db.execute(text("select current_user,session_user")).one()
            assert identity[0] == "sireader_workforce_cmd"
            assert identity[1] == "sireader_workforce_cmd"

            tx = db.get_transaction()
            assert tx is not None
            created = enqueue_task(
                db,
                task_type="QUICK_RESEARCH",
                title="Actual login identity smoke",
                required_capability="quick_research",
                requested_worker_id="haiku",
                input_payload={"source": "SIMULATED", "text": "synthetic only"},
                priority=50,
                max_attempts=1,
                idempotency_key="real-login-command-enqueue-0001",
            )
            task_id = created["outcome"]["task_id"]
            replayed = enqueue_task(
                db,
                task_type="QUICK_RESEARCH",
                title="Actual login identity smoke",
                required_capability="quick_research",
                requested_worker_id="haiku",
                input_payload={"source": "SIMULATED", "text": "synthetic only"},
                priority=50,
                max_attempts=1,
                idempotency_key="real-login-command-enqueue-0001",
            )
            assert replayed["replayed"] is True
            cancelled = cancel_task(
                db, task_id=task_id, idempotency_key="real-login-command-cancel-0001"
            )
            assert cancelled["outcome"]["status"] == "CANCELLED"
            paused = set_worker_scheduling(
                db, worker_id="haiku", accepting_tasks=False,
                idempotency_key="real-login-command-pause-0001",
            )
            assert paused["outcome"]["schedule_state"] == "PAUSED"
            resumed = set_worker_scheduling(
                db, worker_id="haiku", accepting_tasks=True,
                idempotency_key="real-login-command-resume-0001",
            )
            assert resumed["outcome"]["schedule_state"] == "ACCEPTING"
            tx.rollback()

        with review_factory() as db:
            identity = db.execute(text("select current_user,session_user")).one()
            assert identity[0] == "sireader_workforce_review"
            assert identity[1] == "sireader_workforce_review"
            tx = db.get_transaction()
            assert tx is not None
            pending = get_pending_review(db)
            assert pending is None
            tx.rollback()

        maintenance_engine = maintenance._create_function_maintenance_engine(
            url("sireader_workforce_maint")
        )
        try:
            with maintenance_engine.connect() as connection:
                identity = connection.execute(text("select current_user,session_user")).one()
                assert identity[0] == "sireader_workforce_maint"
                assert identity[1] == "sireader_workforce_maint"
                tx = connection.get_transaction()
                assert tx is not None
                counters = connection.execute(text(
                    "select sireader_workforce_private.maintenance_run_once_v1(10,NULL)"
                )).scalar_one()
                tx.rollback()
                values = counters if isinstance(counters, dict) else __import__("json").loads(counters)
                assert all(int(value) == 0 for value in values.values())
        finally:
            maintenance_engine.dispose()

    print("REAL_LOGIN_COMMAND_REVIEW_MAINTENANCE_PG17_PASS")


if __name__ == "__main__":
    asyncio.run(main())
