"""Local PG17-only adapter integration smoke. Transaction is always rolled back."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sys
from uuid import uuid4

# Make the backend root importable when this PG17-only script is invoked by path.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.workers.database_commands import (
    claim_next_task,
    complete_task,
    heartbeat_worker,
    start_task,
    worker_get_contract,
)
from app.workers.runtime_contract import WorkerHeartbeatContract, WorkerProcessState


URL = "postgresql+psycopg://postgres@127.0.0.1:55439/sireader_stage?sslmode=require"


def main() -> None:
    engine = create_engine(URL, pool_pre_ping=True, connect_args={"connect_timeout": 5})
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        with Session() as db:
            try:
                db.begin()
                identity = db.execute(text("""
                    SELECT current_database() AS database_name,
                           host(inet_server_addr()) AS server_host,
                           inet_server_port() AS server_port,
                           session_user AS session_role,
                           ssl, version AS tls_version
                    FROM pg_stat_ssl WHERE pid=pg_backend_pid()
                """)).mappings().one()
                if (
                    identity["database_name"] != "sireader_stage"
                    or identity["server_host"] != "127.0.0.1"
                    or identity["server_port"] != 55439
                    or identity["session_role"] != "postgres"
                    or identity["ssl"] is not True
                    or identity["tls_version"] != "TLSv1.3"
                ):
                    raise RuntimeError("Refusing non-local or non-TLS staging integration target.")

                suffix = uuid4().hex[:12]
                task_id = f"stage-adapter-{suffix}"
                now = datetime.now(timezone.utc)
                db.execute(text("""
                    INSERT INTO public.workforce_tasks(
                        task_id,idempotency_key,correlation_id,task_type,title,
                        required_capability,requested_worker_id,priority,status,
                        input_payload,attempts,max_attempts,not_before,created_at,updated_at
                    ) VALUES (
                        :task_id,:idempotency_key,:correlation_id,'DATA_ARCHIVING',
                        'Python adapter PG17 smoke','data_archiving','data_archivist',
                        90,'QUEUED','{"source":"SIMULATED"}'::jsonb,
                        0,1,clock_timestamp(),clock_timestamp(),clock_timestamp()
                    )
                """), {
                    "task_id": task_id,
                    "idempotency_key": f"stage-adapter-idem-{suffix}",
                    "correlation_id": f"stage-adapter-corr-{suffix}",
                })

                db.execute(text("SET SESSION AUTHORIZATION sireader_worker_data_archivist"))
                contract = worker_get_contract(db, worker_id="data_archivist")
                if contract.worker_id != "data_archivist":
                    raise RuntimeError("Worker contract did not map the authenticated database identity.")

                instance_id = f"adapter-smoke-{suffix}"
                heartbeat_time = datetime.now(timezone.utc)
                heartbeat_worker(db, WorkerHeartbeatContract(
                    worker_id="data_archivist",
                    instance_id=instance_id,
                    process_state=WorkerProcessState.READY,
                    process_started_at=heartbeat_time,
                    heartbeat_at=heartbeat_time,
                    active_task_count=0,
                    in_flight_provider_requests=0,
                    observed_at=heartbeat_time,
                ))
                claimed = claim_next_task(
                    db,
                    worker_id="data_archivist",
                    instance_id=instance_id,
                    lease_seconds=45,
                    heartbeat_timeout_seconds=60,
                )
                if claimed is None or claimed[0].task_id != task_id:
                    raise RuntimeError("Python adapter did not claim its synthetic task.")

                lease_token = claimed[1].lease_token
                start_task(
                    db,
                    task_id=task_id,
                    lease_token=lease_token,
                    worker_id="data_archivist",
                    instance_id=instance_id,
                )
                completion = complete_task(
                    db,
                    task_id=task_id,
                    lease_token=lease_token,
                    worker_id="data_archivist",
                    instance_id=instance_id,
                    result_payload={"source": "SIMULATED", "summary": "adapter integration passed"},
                    policy_context={
                        "policy_version": "2026.11.10-harness.1",
                        "sensitive_action": "ALLOW",
                        "model_tier": "NONE",
                        "reason_code": "POLICY_ALLOWED",
                        "redacted_fields": 0,
                    },
                )
                if completion.status != "COMPLETED" or completion.requires_review:
                    raise RuntimeError("Python adapter completion did not return a normal completed task.")

                db.execute(text("RESET SESSION AUTHORIZATION"))
                restored = db.execute(text("SELECT session_user")).scalar_one()
                if restored != "postgres":
                    raise RuntimeError("Staging superuser identity was not restored.")

                print("PYTHON_DATABASE_COMMAND_ADAPTER_PG17_PASS")
                print("worker_identity=data_archivist")
                print("commands=contract,heartbeat,claim,start,complete")
                print("synthetic_task_transaction=ROLLED_BACK")
            finally:
                if db.in_transaction():
                    db.rollback()
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
