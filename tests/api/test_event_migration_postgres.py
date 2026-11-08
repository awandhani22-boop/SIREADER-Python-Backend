"""Disposable PostgreSQL integration test for the Phase 3.3 trigger migration."""
import os
from pathlib import Path

import pytest
from sqlalchemy.engine import make_url

POSTGRES_URL = os.getenv("SIREADER_TEST_POSTGRES_URL")
PSYCOPG_URL = (
    make_url(POSTGRES_URL).set(drivername="postgresql").render_as_string(hide_password=False)
    if POSTGRES_URL
    else None
)
pytestmark = pytest.mark.skipif(not POSTGRES_URL, reason="Disposable PostgreSQL URL is not configured")


def test_observability_migration_is_idempotent_and_transactional():
    psycopg = pytest.importorskip("psycopg")
    root = Path(__file__).resolve().parents[2]
    migration = (root / "migrations" / "2026_11_09_observability_events.sql").read_text()
    rollback = (root / "migrations" / "2026_11_09_observability_events_rollback.sql").read_text()
    tables = (
        "runtime_control_state",
        "ai_usage_logs",
        "ai_analysis_logs",
        "ai_pending_messages",
        "trade_decision_watches",
        "approval_decisions",
    )
    expected = ["system", "telemetry", "pipeline", "pipeline", "market_watches", "audit"]

    with psycopg.connect(PSYCOPG_URL, autocommit=True) as admin:
        for table in tables:
            admin.execute(f'CREATE TABLE public."{table}" (id bigint PRIMARY KEY)')
        try:
            admin.execute(migration, prepare=False)
            admin.execute(migration, prepare=False)  # DROP + CREATE proves idempotency.

            with psycopg.connect(PSYCOPG_URL, autocommit=True) as listener:
                listener.execute("SET default_transaction_read_only = on")
                assert listener.execute("SHOW transaction_read_only").fetchone()[0] == "on"
                listener.execute("LISTEN sireader_observability_v1")
                with psycopg.connect(PSYCOPG_URL) as writer:
                    for table in tables:
                        writer.execute(f'INSERT INTO public."{table}" (id) VALUES (1)')
                        writer.commit()

                notifications = list(listener.notifies(timeout=2.0, stop_after=6))
                assert [item.payload for item in notifications] == expected

                with psycopg.connect(PSYCOPG_URL) as writer:
                    writer.execute('INSERT INTO public."runtime_control_state" (id) VALUES (2)')
                    writer.rollback()
                rolled_back = list(listener.notifies(timeout=0.2))
                assert rolled_back == []

                with psycopg.connect(PSYCOPG_URL) as writer:
                    writer.execute('INSERT INTO public."runtime_control_state" (id) VALUES (3)')
                    writer.commit()
                one = list(listener.notifies(timeout=2.0, stop_after=1))
                assert [item.payload for item in one] == ["system"]

            admin.execute(rollback, prepare=False)
        finally:
            for table in tables:
                admin.execute(f'DROP TABLE IF EXISTS public."{table}" CASCADE')
            admin.execute("DROP FUNCTION IF EXISTS public.sireader_notify_observability_v1()")
