"""PostgreSQL integration test for the Phase 3.3 notification migration.

Runs after test_postgres_privileges.py has provisioned its disposable CI schema.
It never connects to Supabase or the user's production database.
"""
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
        missing = [
            table for table in tables
            if admin.execute("SELECT to_regclass(%s)", (f"public.{table}",)).fetchone()[0] is None
        ]
        if missing:
            pytest.skip("Run after the disposable PostgreSQL privilege fixture; missing base tables.")

        try:
            admin.execute(migration, prepare=False)
            admin.execute(migration, prepare=False)  # DROP + CREATE proves idempotency.

            inventory = admin.execute(
                """
                SELECT
                    count(DISTINCT c.relname) FILTER (
                        WHERE t.tgname = 'sireader_observability_notify'
                    )::int,
                    count(DISTINCT c.relname) FILTER (
                        WHERE t.tgname = 'sireader_observability_truncate'
                    )::int,
                    count(*) FILTER (WHERE p.prosecdef)::int,
                    count(*) FILTER (
                        WHERE p.proconfig IS NULL
                           OR NOT (p.proconfig @> ARRAY['search_path=pg_catalog']::text[])
                    )::int
                FROM pg_catalog.pg_trigger AS t
                JOIN pg_catalog.pg_class AS c ON c.oid = t.tgrelid
                JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
                JOIN pg_catalog.pg_proc AS p ON p.oid = t.tgfoid
                WHERE n.nspname = 'public'
                  AND NOT t.tgisinternal
                  AND c.relname = ANY (ARRAY[
                      'runtime_control_state', 'ai_usage_logs', 'ai_analysis_logs',
                      'ai_pending_messages', 'trade_decision_watches', 'approval_decisions'
                  ])
                  AND t.tgname IN (
                      'sireader_observability_notify', 'sireader_observability_truncate'
                  )
                """
            ).fetchone()
            assert inventory == (6, 6, 0, 0)

            with psycopg.connect(PSYCOPG_URL, autocommit=True) as listener:
                listener.execute("SET default_transaction_read_only = on")
                assert listener.execute("SHOW transaction_read_only").fetchone()[0] == "on"
                listener.execute("LISTEN sireader_observability_v1")

                writer_sql = [
                    """
                    INSERT INTO public.runtime_control_state
                    (id, execution_mode, trading_mode, auto_execution_enabled, auto_paused, updated_at)
                    VALUES (2, 'APPROVAL', 'DRY_RUN', false, true, now())
                    """,
                    """
                    INSERT INTO public.ai_usage_logs
                    (usage_date, provider, model, tier, input_tokens, output_tokens,
                     cache_read_tokens, cache_write_tokens, thinking_tokens, billing_currency,
                     cost_rp, reserved_rp, cost_usd, reserved_usd, status, created_at, completed_at)
                    VALUES (current_date, 'ci', 'ci-model', 'FAST', 5, 7, 0, 0, 0, 'IDR',
                            100, 20, 900, 900, 'COMPLETED', now(), now())
                    """,
                    """
                    INSERT INTO public.ai_analysis_logs (chat_id, target_message_id, status, created_at)
                    VALUES (123, 457, 'PROCESSING', now())
                    """,
                    """
                    INSERT INTO public.ai_pending_messages
                    (dedup_key, chat_id, telegram_message_id, status, reason, attempts, created_at, updated_at)
                    VALUES ('ci-sse-dedup', 123, 457, 'PENDING', 'BLOCKED_HARD_BUDGET', 1, now(), now())
                    """,
                    """
                    INSERT INTO public.trade_decision_watches
                    (symbol, broker_server, status, spread_pips, maximum_spread_pips,
                     check_count, next_check_at, last_reason, created_at, updated_at)
                    VALUES ('XAUUSD', 'ci', 'WAITING_FOR_SPREAD', 5, 30,
                            1, now(), 'ci', now(), now())
                    """,
                    """
                    INSERT INTO public.approval_decisions
                    (analysis_log_id, previous_status, decision, new_status, reviewer, note, created_at)
                    VALUES (1, 'MANUAL_REVIEW', 'APPROVED', 'APPROVED', 'ci', 'ci', now())
                    """,
                ]
                with psycopg.connect(PSYCOPG_URL) as writer:
                    for statement in writer_sql:
                        writer.execute(statement)
                        writer.commit()

                notifications = list(listener.notifies(timeout=2.0, stop_after=6))
                assert [item.payload for item in notifications] == expected

                with psycopg.connect(PSYCOPG_URL) as writer:
                    writer.execute(
                        """
                        INSERT INTO public.runtime_control_state
                        (id, execution_mode, trading_mode, auto_execution_enabled, auto_paused, updated_at)
                        VALUES (3, 'APPROVAL', 'DRY_RUN', false, true, now())
                        """
                    )
                    writer.rollback()
                rolled_back = list(listener.notifies(timeout=0.2))
                assert rolled_back == []

                with psycopg.connect(PSYCOPG_URL) as writer:
                    writer.execute(
                        """
                        INSERT INTO public.runtime_control_state
                        (id, execution_mode, trading_mode, auto_execution_enabled, auto_paused, updated_at)
                        VALUES (4, 'APPROVAL', 'DRY_RUN', false, true, now())
                        """
                    )
                    writer.commit()
                one = list(listener.notifies(timeout=2.0, stop_after=1))
                assert [item.payload for item in one] == ["system"]

                admin.execute("TRUNCATE public.approval_decisions")
                truncated = list(listener.notifies(timeout=2.0, stop_after=1))
                assert [item.payload for item in truncated] == ["audit"]

            admin.execute(rollback, prepare=False)
        finally:
            admin.execute(rollback, prepare=False)
