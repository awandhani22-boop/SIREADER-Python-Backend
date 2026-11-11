"""PG17-only parity check for Python and database human-review policy.

Refuses every target except the isolated loopback sireader_stage database.
No writes are performed.
"""
from __future__ import annotations

from pathlib import Path
import sys

# Running this staging script by path sets sys.path[0] to staging/, so add the
# backend root explicitly without relying on the caller's PYTHONPATH.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, text

from app.governance.review_policy import (
    POLICY_VERSION,
    REVIEW_POLICY_CATALOG,
    evaluate_review_eligibility,
)
from app.workers.registry import CAPABILITY_CATALOG


URL = "postgresql+psycopg://postgres@127.0.0.1:55439/sireader_stage?sslmode=require"
TASK_IDS = tuple(f"review-policy-parity-task-{index:02d}" for index in range(1, 21))


def main() -> None:
    engine = create_engine(URL, pool_pre_ping=True, connect_args={"connect_timeout": 5})
    pair_checks = 0
    sample_checks = 0
    try:
        with engine.connect() as db:
            identity = db.execute(text("""
                SELECT current_database() AS database_name,
                       host(inet_server_addr()) AS server_host,
                       inet_server_port() AS server_port,
                       session_user AS session_role,
                       ssl, version AS tls_version
                FROM pg_stat_ssl
                WHERE pid = pg_backend_pid()
            """)).mappings().one()
            if (
                identity["database_name"] != "sireader_stage"
                or identity["server_host"] != "127.0.0.1"
                or identity["server_port"] != 55439
                or identity["session_role"] != "postgres"
                or identity["ssl"] is not True
                or not str(identity["tls_version"]).startswith("TLSv1.")
            ):
                raise RuntimeError("Refusing any database other than TLS loopback PG17 sireader_stage.")

            sql_rules = db.execute(text("""
                SELECT capability_id, task_type, policy_version, mode,
                       sample_rate_percent, sample_salt
                FROM sireader_workforce_private.review_rules
            """)).mappings().all()
            sql_by_key = {
                (row["capability_id"], row["task_type"]): (
                    row["policy_version"], row["mode"],
                    int(row["sample_rate_percent"]), row["sample_salt"],
                )
                for row in sql_rules
            }
            python_by_key = {
                key: (
                    POLICY_VERSION, rule.mode.value,
                    int(rule.sample_rate_percent), rule.sample_salt,
                )
                for key, rule in REVIEW_POLICY_CATALOG.items()
            }
            if sql_by_key != python_by_key:
                missing = sorted(set(python_by_key) - set(sql_by_key))
                extra = sorted(set(sql_by_key) - set(python_by_key))
                mismatched = sorted(
                    key for key in set(sql_by_key) & set(python_by_key)
                    if sql_by_key[key] != python_by_key[key]
                )
                raise AssertionError(
                    f"Review policy catalog mismatch: missing={missing}, "
                    f"extra={extra}, mismatched={mismatched}"
                )

            for capability_id, capability in CAPABILITY_CATALOG.items():
                for task_type in capability.task_types:
                    for task_id in TASK_IDS:
                        expected = evaluate_review_eligibility(
                            capability_id, task_type, task_id
                        )
                        actual = db.execute(text("""
                            SELECT sireader_workforce_private.requires_review_v1(
                                :capability_id, :task_type, :task_id,
                                'Synthetic policy parity fixture',
                                '{"source":"SIMULATED","text":"non-sensitive fixture"}'::jsonb
                            )
                        """), {
                            "capability_id": capability_id,
                            "task_type": task_type,
                            "task_id": task_id,
                        }).scalar_one()
                        pair_checks += 1
                        if bool(actual) != expected:
                            raise AssertionError(
                                "Review decision mismatch for "
                                f"{capability_id}/{task_type}/{task_id}: "
                                f"python={expected}, sql={actual}"
                            )
                        sample_checks += 1

            for task_id in TASK_IDS:
                expected = evaluate_review_eligibility(
                    "unknown_capability", "UNKNOWN_TASK", task_id
                )
                actual = db.execute(text("""
                    SELECT sireader_workforce_private.requires_review_v1(
                        'unknown_capability','UNKNOWN_TASK',:task_id,
                        'Synthetic unknown capability',
                        '{"source":"SIMULATED"}'::jsonb
                    )
                """), {"task_id": task_id}).scalar_one()
                if bool(actual) != expected:
                    raise AssertionError("Unknown capability policy did not fail closed.")

        print("PYTHON_POSTGRES_REVIEW_POLICY_PARITY_PASS")
        print(f"catalog_rules={len(python_by_key)}")
        print(f"capability_task_pairs={pair_checks // len(TASK_IDS)}")
        print(f"decision_comparisons={sample_checks}")
        print(f"deterministic_sample_task_ids={len(TASK_IDS)}")
        print("unknown_capability_fail_closed=PASS")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
