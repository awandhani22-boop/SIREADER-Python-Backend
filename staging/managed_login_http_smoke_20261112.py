"""HTTP smoke test using three distinct real LOGIN identities on local PG17.

All targets are hard-pinned to loopback port 55450 and the disposable restored
backup rehearsal database. No passwords are provisioned (the local-only HBA
uses trust). The test inserts only synthetic fixtures. It does not bypass
retention triggers; purge fixtures through the official Maintenance function.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.command_api.config import WorkforceCommandSettings
from app.command_api.server import create_command_app


DB = "sireader_managed_role_rehearsal_20261112"
TOKEN = "SIREADER-MANAGED-LOGIN-HTTP-SMOKE-20261112-0123456789"
PREFIX = "stg_http_real_20261112_" + uuid.uuid4().hex[:8]


def database_url(role: str) -> str:
    return f"postgresql+psycopg://{role}:local-placeholder@127.0.0.1:55450/{DB}"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> None:
    task_id = f"{PREFIX}_task"
    review_task_id = f"{PREFIX}_review_task"
    attempt_id = f"{PREFIX}_review_attempt"
    review_id = "rev_" + f"{PREFIX}_review"
    review_hash = None
    idem = [
        f"{PREFIX}_enqueue",
        f"{PREFIX}_cancel",
        f"{PREFIX}_pause",
        f"{PREFIX}_resume",
        f"{PREFIX}_review_approve",
    ]

    admin_engine = create_engine(database_url("sireader_local_admin"), pool_pre_ping=True)
    # Hard guard the target before making any test fixture.
    with admin_engine.connect() as connection:
        target = connection.execute(text("""
            SELECT current_database() db, host(inet_server_addr()) host,
                   inet_server_port() port, current_setting('server_version_num') version_num
        """)).mappings().one()
        require(
            target["db"] == DB and target["host"] == "127.0.0.1"
            and int(target["port"]) == 55450 and str(target["version_num"]).startswith("17"),
            "Refusing to run outside the loopback PG17 restored-backup rehearsal.",
        )

    settings = WorkforceCommandSettings(
        SIREADER_WORKFORCE_COMMAND_DATABASE_URL=database_url("sireader_workforce_cmd"),
        SIREADER_WORKFORCE_REVIEW_DATABASE_URL=database_url("sireader_workforce_review"),
        SIREADER_WORKFORCE_COMMAND_AUTH_TOKEN=TOKEN,
        WORKFORCE_COMMAND_HOST="127.0.0.1",
        WORKFORCE_COMMAND_PORT=8010,
    )

    try:
        # Seed a synthetic completed task/review as the local admin. The API
        # operation itself will run only as the separate review LOGIN.
        with admin_engine.begin() as connection:
            now = connection.execute(text("SELECT clock_timestamp()")).scalar_one()
            expiry = connection.execute(text("SELECT clock_timestamp()+interval '1 day'")).scalar_one()
            connection.execute(text("""
                INSERT INTO public.workforce_tasks(
                    task_id,idempotency_key,correlation_id,schema_version,task_type,title,
                    required_capability,requested_worker_id,assigned_worker_id,priority,status,
                    input_payload,attempts,max_attempts,not_before,created_at,updated_at,
                    completed_at,payload_expires_at
                ) VALUES (
                    :task_id,:idem,:correlation,1,'QUICK_RESEARCH','Real-login HTTP review smoke',
                    'quick_research','opus','opus',50,'COMPLETED','{}'::jsonb,1,1,
                    :now,:now,:now,:now,:expiry
                )
            """), {
                "task_id": review_task_id, "idem": f"{PREFIX}_review_task_idem",
                "correlation": f"{PREFIX}_corr", "now": now, "expiry": expiry,
            })
            connection.execute(text("""
                INSERT INTO public.workforce_task_attempts(
                    attempt_id,task_id,attempt_number,worker_id,lease_token,status,
                    started_at,finished_at,created_at
                ) VALUES (
                    :attempt,:task_id,1,'opus',:lease,'COMPLETED',:start,:now,:now
                )
            """), {
                "attempt": attempt_id, "task_id": review_task_id,
                "lease": f"{PREFIX}_review_lease", "start": now, "now": now,
            })
            output = {"summary": "Original synthetic review output"}
            encoded = json.dumps(output,ensure_ascii=False,separators=(",",":"),allow_nan=False)
            digest = connection.execute(text("""
                SELECT encode(extensions.digest(
                    convert_to(sireader_workforce_private.canonical_json_v1(CAST(:payload AS jsonb)),'UTF8'),
                    'sha256'
                ),'hex')
            """), {"payload": encoded}).scalar_one()
            review_hash = digest
            connection.execute(text("""
                INSERT INTO public.workforce_task_reviews(
                    review_id,task_id,attempt_id,status,original_output_sha256,
                    review_expires_at,created_at,updated_at
                ) VALUES (
                    :review_id,:task_id,:attempt,'PENDING',:hash,:expiry,:now,:now
                )
            """), {
                "review_id": review_id, "task_id": review_task_id,
                "attempt": attempt_id, "hash": digest, "expiry": expiry, "now": now,
            })
            connection.execute(text("""
                INSERT INTO public.workforce_task_review_payloads(
                    review_id,original_output,created_at,updated_at
                ) VALUES (:review_id,CAST(:payload AS jsonb),:now,:now)
            """), {"review_id": review_id, "payload": encoded, "now": now})

        app = create_command_app(settings=settings,validate_database=True)
        with TestClient(app, client=("127.0.0.1", 45555)) as client:
            headers = {
                "Authorization": f"Bearer {TOKEN}",
                "Host": "127.0.0.1:8010",
            }
            enqueue = client.post(
                "/api/v1/commands/tasks/enqueue",
                headers={**headers,"Idempotency-Key":idem[0]},
                json={
                    "task_type":"QUICK_RESEARCH",
                    "title":"Real LOGIN command API smoke",
                    "required_capability":"quick_research",
                    "requested_worker_id":"haiku",
                    "input_payload":{"source":"SIMULATED","text":"synthetic smoke only"},
                    "priority":50,
                    "max_attempts":1,
                    "non_trading_acknowledged":True,
                },
            )
            require(enqueue.status_code==200,f"enqueue HTTP {enqueue.status_code}: {enqueue.text}")
            enqueued=enqueue.json()
            require(bool(enqueued["outcome"].get("task_id")),"task id missing")
            require(enqueued["outcome"]["created"] is True,"task wasn't created")
            require(enqueued["outcome"]["task_id"],"task id is empty")
            task_id=enqueued["outcome"]["task_id"]

            replay=client.post(
                "/api/v1/commands/tasks/enqueue",
                headers={**headers,"Idempotency-Key":idem[0]},
                json={
                    "task_type":"QUICK_RESEARCH",
                    "title":"Real LOGIN command API smoke",
                    "required_capability":"quick_research",
                    "requested_worker_id":"haiku",
                    "input_payload":{"source":"SIMULATED","text":"synthetic smoke only"},
                    "priority":50,
                    "max_attempts":1,
                    "non_trading_acknowledged":True,
                },
            )
            require(replay.status_code==200 and replay.json()["replayed"] is True,
                    f"enqueue replay failed: {replay.status_code} {replay.text}")

            cancel=client.post(
                "/api/v1/commands/tasks/cancel",
                headers={**headers,"Idempotency-Key":idem[1]},
                json={"task_id":task_id},
            )
            require(cancel.status_code==200 and cancel.json()["outcome"]["status"]=="CANCELLED",
                    f"cancel failed: {cancel.status_code} {cancel.text}")

            pause=client.post(
                "/api/v1/commands/workers/scheduling",
                headers={**headers,"Idempotency-Key":idem[2]},
                json={"worker_id":"haiku","accepting_tasks":False},
            )
            require(pause.status_code==200 and pause.json()["outcome"]["schedule_state"]=="PAUSED",
                    f"pause failed: {pause.status_code} {pause.text}")
            resume=client.post(
                "/api/v1/commands/workers/scheduling",
                headers={**headers,"Idempotency-Key":idem[3]},
                json={"worker_id":"haiku","accepting_tasks":True},
            )
            require(resume.status_code==200 and resume.json()["outcome"]["schedule_state"]=="ACCEPTING",
                    f"resume failed: {resume.status_code} {resume.text}")

            # Deliberately lie in the legacy header. The actor must still be the
            # identity registered by the authenticated Review database login.
            reviewed=client.post(
                "/api/v1/commands/reviews/adjudicate",
                headers={
                    **headers,
                    "X-Operator-Id":"untrusted-header-actor",
                    "Idempotency-Key":idem[4],
                },
                json={
                    "review_id":review_id,
                    "decision":"APPROVED",
                    "expected_original_sha256":review_hash,
                    "adjusted_output":None,
                    "reason_code":None,
                    "notes":"approved in actual review-login HTTP smoke",
                },
            )
            require(reviewed.status_code==200,f"review HTTP {reviewed.status_code}: {reviewed.text}")
            require(reviewed.json()["outcome"]["adjudicated_by"]=="local_operator",
                    "review actor came from an untrusted HTTP header, not the DB identity")

        print("REAL_LOGIN_FASTAPI_COMMAND_REVIEW_PG17_PASS")
    finally:
        # This is a disposable restored-backup rehearsal database. Synthetic
        # review payloads are intentionally protected from direct DELETE by
        # retention triggers; the whole database is dropped after the complete
        # test suite, rather than bypassing retention policy to remove them.
        admin_engine.dispose()


if __name__=="__main__":
    main()
