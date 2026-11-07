"""SQLAlchemy Core mappings untuk enam PostgreSQL views yang disetujui."""
from sqlalchemy import Boolean, Column, Date, DateTime, Float, Integer, MetaData, Numeric, String, Table

metadata = MetaData()

v_api_system_runtime_state = Table(
    "v_api_system_runtime_state", metadata,
    Column("execution_mode", String(20), nullable=False),
    Column("trading_mode", String(20), nullable=False),
    Column("auto_execution_enabled", Boolean, nullable=False),
    Column("auto_paused", Boolean, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

v_api_telemetry_usage = Table(
    "v_api_telemetry_usage", metadata,
    Column("usage_date", Date, nullable=False),
    Column("provider", String(50), nullable=False),
    Column("model", String(255), nullable=False),
    Column("tier", String(30), nullable=False),
    Column("input_tokens", Integer, nullable=False),
    Column("output_tokens", Integer, nullable=False),
    Column("cache_read_tokens", Integer, nullable=False),
    Column("cache_write_tokens", Integer, nullable=False),
    Column("thinking_tokens", Integer, nullable=False),
    Column("billing_currency", String(3), nullable=False),
    Column("cost_rp", Numeric(18, 6), nullable=False),
    Column("reserved_rp", Numeric(18, 6), nullable=False),
    Column("cost_usd", Numeric(18, 6), nullable=False),
    Column("reserved_usd", Numeric(18, 6), nullable=False),
    Column("status", String(20), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("completed_at", DateTime(timezone=True), nullable=True),
)

v_api_active_in_flight_tasks = Table(
    "v_api_active_in_flight_tasks", metadata,
    Column("task_id", Integer, primary_key=True),
    Column("status", String(40), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("elapsed_seconds", Float, nullable=False),
)

v_api_queued_pending_messages = Table(
    "v_api_queued_pending_messages", metadata,
    Column("queue_item_id", Integer, primary_key=True),
    Column("status", String(20), nullable=False),
    Column("queue_reason", String(40), nullable=False),
    Column("attempts", Integer, nullable=False),
    Column("claimed_at", DateTime(timezone=True), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

v_api_active_market_watches = Table(
    "v_api_active_market_watches", metadata,
    Column("watch_id", Integer, primary_key=True),
    Column("symbol", String(64), nullable=False),
    Column("status", String(32), nullable=False),
    Column("spread_pips", Numeric(18, 5), nullable=True),
    Column("maximum_spread_pips", Numeric(18, 5), nullable=False),
    Column("last_bid", Numeric(18, 5), nullable=True),
    Column("last_ask", Numeric(18, 5), nullable=True),
    Column("check_count", Integer, nullable=False),
    Column("last_checked_at", DateTime(timezone=True), nullable=True),
    Column("next_check_at", DateTime(timezone=True), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

v_api_audit_decisions = Table(
    "v_api_audit_decisions", metadata,
    Column("audit_id", Integer, primary_key=True),
    Column("decision", String(20), nullable=False),
    Column("previous_status", String(40), nullable=False),
    Column("new_status", String(40), nullable=False),
    Column("reviewer_role", String(50), nullable=False),
    Column("decided_at", DateTime(timezone=True), nullable=False),
)
