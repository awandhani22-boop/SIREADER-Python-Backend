"""FastAPI-to-PostgreSQL function-layer smoke test for the isolated PG17 staging clone.

Hard-guarded to local sireader_stage:55439. Uses SESSION AUTHORIZATION only
inside test transactions, does not create LOGIN credentials, and rolls back
all task/review/audit fixtures at the end.
"""
from __future__ import annotations

from sqlalchemy import create_engine, text
from pathlib import Path
import sys

from sqlalchemy.orm import sessionmaker
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.command_api.server import create_command_app

STAGING_URL = "postgresql+psycopg://postgres@127.0.0.1:55439/sireader_stage?sslmode=require"
TOKEN = "SIREADER-STAGING-COMMAND-TOKEN-ONLY-0123456789"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> None:
    engine = create_engine(STAGING_URL, pool_pre_ping=True)
    command_conn = engine.connect()
    review_conn = engine.connect()
    command_tx = command_conn.begin()
    review_tx = review_conn.begin()
    try:
        target = command_conn.execute(text("""
            SELECT current_database() AS db, host(inet_server_addr()) AS host,
                   inet_server_port() AS port, current_setting('server_version_num') AS version_num
        """)).mappings().one()
        require(
            target["db"] == "sireader_stage"
            and target["host"] == "127.0.0.1"
            and int(target["port"]) == 55439
            and str(target["version_num"]).startswith("17"),
            "Refusing to run outside local PostgreSQL 17 staging.",
        )

        command_conn.exec_driver_sql("SET SESSION AUTHORIZATION sireader_workforce_cmd")
        review_conn.exec_driver_sql("SET SESSION AUTHORIZATION sireader_workforce_review")

        command_factory = sessionmaker(bind=command_conn, autoflush=False, expire_on_commit=False)
        review_factory = sessionmaker(bind=review_conn, autoflush=False, expire_on_commit=False)
        app = create_command_app(
            session_factory=command_factory,
            review_session_factory=review_factory,
            token=TOKEN,
            validate_database=False,
        )

        with TestClient(app) as client:
            headers = {"Authorization": f"Bearer {TOKEN}"}
            enqueue_headers = {**headers, "Idempotency-Key": "api-staging-enqueue-0001"}
            body = {
                "task_type": "QUICK_RESEARCH",
                "title": "FastAPI function-layer smoke",
                "required_capability": "quick_research",
                "requested_worker_id": "haiku",
                "input_payload": {"source": "SIMULATED", "text": "synthetic acceptance only"},
                "priority": 50,
                "max_attempts": 1,
                "non_trading_acknowledged": True,
            }
            created = client.post("/api/v1/commands/tasks/enqueue", headers=enqueue_headers, json=body)
            require(created.status_code == 200, f"enqueue HTTP {created.status_code}: {created.text}")
            created_body = created.json()
            task_id = created_body["outcome"]["task_id"]
            require(created_body["outcome"]["created"] is True, "enqueue did not create the synthetic task")

            replayed = client.post("/api/v1/commands/tasks/enqueue", headers=enqueue_headers, json=body)
            require(replayed.status_code == 200, f"enqueue replay HTTP {replayed.status_code}: {replayed.text}")
            require(replayed.json()["replayed"] is True, "database command replay was not detected")

            cancel = client.post(
                "/api/v1/commands/tasks/cancel",
                headers={**headers, "Idempotency-Key": "api-staging-cancel-0001"},
                json={"task_id": task_id},
            )
            require(cancel.status_code == 200, f"cancel HTTP {cancel.status_code}: {cancel.text}")
            require(cancel.json()["outcome"]["status"] == "CANCELLED", "pre-provider task did not cancel")

            pause = client.post(
                "/api/v1/commands/workers/scheduling",
                headers={**headers, "Idempotency-Key": "api-staging-pause-0001"},
                json={"worker_id": "haiku", "accepting_tasks": False},
            )
            require(pause.status_code == 200, f"pause HTTP {pause.status_code}: {pause.text}")
            require(pause.json()["outcome"]["schedule_state"] == "PAUSED", "worker pause command did not persist")

            resume = client.post(
                "/api/v1/commands/workers/scheduling",
                headers={**headers, "Idempotency-Key": "api-staging-resume-0001"},
                json={"worker_id": "haiku", "accepting_tasks": True},
            )
            require(resume.status_code == 200, f"resume HTTP {resume.status_code}: {resume.text}")
            require(resume.json()["outcome"]["schedule_state"] == "ACCEPTING", "worker resume command did not persist")

            # Seed a review through the superuser identity within the same
            # outer transaction; the review API itself receives only its role.
            review_conn.exec_driver_sql("RESET SESSION AUTHORIZATION")
            now = review_conn.execute(text("SELECT clock_timestamp()")).scalar_one()
            review_conn.execute(text("""
                INSERT INTO public.workforce_tasks(
                    task_id,idempotency_key,correlation_id,schema_version,task_type,title,
                    required_capability,requested_worker_id,assigned_worker_id,priority,status,
                    input_payload,attempts,max_attempts,not_before,created_at,updated_at,
                    completed_at,payload_expires_at
                ) VALUES (
                    'stg_api_review_task_01','stg_api_review_idem_01','stg_api_review_corr_01',1,
                    'QUICK_RESEARCH','FastAPI review smoke','quick_research','opus','opus',50,
                    'COMPLETED','{}'::jsonb,1,1,:now,:now,:now,:now,:expiry
                )
            """), {"now": now, "expiry": now})
            review_conn.execute(text("""
                INSERT INTO public.workforce_task_attempts(
                    attempt_id,task_id,attempt_number,worker_id,lease_token,status,
                    started_at,finished_at,created_at
                ) VALUES (
                    'stg_api_review_attempt_01','stg_api_review_task_01',1,'opus',
                    'stg_api_review_lease_01','COMPLETED',:start,:now,:now
                )
            """), {"start": now, "now": now})
            review_conn.execute(text("""
                WITH payload AS (SELECT '{"summary":"FastAPI original review output"}'::jsonb AS value)
                INSERT INTO public.workforce_task_reviews(
                    review_id,task_id,attempt_id,status,original_output_sha256,
                    review_expires_at,created_at,updated_at
                )
                SELECT 'rev_stg_api_review_01','stg_api_review_task_01',
                    'stg_api_review_attempt_01','PENDING',
                    encode(extensions.digest(
                        convert_to(sireader_workforce_private.canonical_json_v1(value),'UTF8'),'sha256'
                    ),'hex'),
                    :expiry,:now,:now
                FROM payload
            """), {"now": now, "expiry": now})
            review_conn.execute(text("""
                INSERT INTO public.workforce_task_review_payloads(
                    review_id,original_output,created_at,updated_at
                ) VALUES (
                    'rev_stg_api_review_01','{"summary":"FastAPI original review output"}'::jsonb,:now,:now
                )
            """), {"now": now})

            expected_hash = review_conn.execute(text("""
                SELECT original_output_sha256::text
                FROM public.workforce_task_reviews WHERE review_id='rev_stg_api_review_01'
            """)).scalar_one()
            review_conn.exec_driver_sql("SET SESSION AUTHORIZATION sireader_workforce_review")
            review_headers = {
                **headers,
                "X-Operator-Id": "untrusted-header-actor",
                "Idempotency-Key": "api-staging-review-approve-0001",
            }
            reviewed = client.post(
                "/api/v1/commands/reviews/adjudicate",
                headers=review_headers,
                json={
                    "review_id": "rev_stg_api_review_01",
                    "decision": "APPROVED",
                    "expected_original_sha256": expected_hash,
                    "adjusted_output": None,
                    "reason_code": None,
                    "notes": "FastAPI staging approval",
                },
            )
            require(reviewed.status_code == 200, f"review HTTP {reviewed.status_code}: {reviewed.text}")
            require(
                reviewed.json()["outcome"]["adjudicated_by"] == "local_operator",
                "review actor was accepted from the untrusted HTTP header instead of the database identity",
            )

        # Both command and review changes, including audits, are discarded.
        print("FASTAPI_PG17_FUNCTION_COMMAND_REVIEW_SMOKE_PASS")
    finally:
        try:
            command_tx.rollback()
        finally:
            review_tx.rollback()
            command_conn.close()
            review_conn.close()
            engine.dispose()


if __name__ == "__main__":
    main()
