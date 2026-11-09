"""SQLAlchemy Core mappings untuk enam PostgreSQL views yang disetujui."""
from sqlalchemy import Boolean, Column, Date, DateTime, Float, Integer, JSON, MetaData, Numeric, String, Table

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


# Phase 4.4 — worker runtime views (strict metadata allowlist; no task input/result payloads).
v_api_workforce_workers = Table(
    "v_api_workforce_workers", metadata,
    Column("worker_id", String(64), primary_key=True),
    Column("display_name", String(100), nullable=False),
    Column("role", String(160), nullable=False),
    Column("department", String(80), nullable=False),
    Column("enabled", Boolean, nullable=False),
    Column("max_concurrency", Integer, nullable=False),
    Column("model_tier", String(20), nullable=False),
    Column("capability_ids", JSON, nullable=False),
    Column("process_status", String(20), nullable=False),
    Column("heartbeat_at", DateTime(timezone=True), nullable=True),
    Column("active_task_count", Integer, nullable=False),
    Column("in_flight_provider_requests", Integer, nullable=False),
    Column("schema_version", Integer, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

v_api_workforce_tasks = Table(
    "v_api_workforce_tasks", metadata,
    Column("task_id", String(64), primary_key=True),
    Column("correlation_id", String(64), nullable=False),
    Column("schema_version", Integer, nullable=False),
    Column("task_type", String(64), nullable=False),
    Column("title", String(180), nullable=False),
    Column("required_capability", String(64), nullable=False),
    Column("requested_worker_id", String(64), nullable=True),
    Column("assigned_worker_id", String(64), nullable=True),
    Column("priority", Integer, nullable=False),
    Column("status", String(24), nullable=False),
    Column("attempts", Integer, nullable=False),
    Column("max_attempts", Integer, nullable=False),
    Column("not_before", DateTime(timezone=True), nullable=False),
    Column("deadline_at", DateTime(timezone=True), nullable=True),
    Column("lease_expires_at", DateTime(timezone=True), nullable=True),
    Column("failure_code", String(80), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("completed_at", DateTime(timezone=True), nullable=True),
)

v_api_workforce_attempts = Table(
    "v_api_workforce_attempts", metadata,
    Column("attempt_id", String(64), primary_key=True),
    Column("task_id", String(64), nullable=False),
    Column("attempt_number", Integer, nullable=False),
    Column("worker_id", String(64), nullable=False),
    Column("status", String(24), nullable=False),
    Column("billing_currency", String(3), nullable=True),
    Column("cost_amount", Numeric(18, 6), nullable=True),
    Column("error_code", String(80), nullable=True),
    Column("started_at", DateTime(timezone=True), nullable=True),
    Column("finished_at", DateTime(timezone=True), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

v_api_workforce_activity = Table(
    "v_api_workforce_activity", metadata,
    Column("event_id", String(64), primary_key=True),
    Column("event_type", String(40), nullable=False),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    Column("worker_id", String(64), nullable=True),
    Column("task_id", String(64), nullable=True),
    Column("attempt_id", String(64), nullable=True),
    Column("correlation_id", String(64), nullable=False),
)

v_api_workforce_runtime = Table(
    "v_api_workforce_runtime", metadata,
    Column("observed_at", DateTime(timezone=True), nullable=False),
    Column("configured_workers", Integer, nullable=False),
    Column("ready_workers", Integer, nullable=False),
    Column("stale_workers", Integer, nullable=False),
    Column("offline_workers", Integer, nullable=False),
    Column("active_tasks", Integer, nullable=False),
    Column("queued_tasks", Integer, nullable=False),
    Column("waiting_review_tasks", Integer, nullable=False),
    Column("dead_lettered_tasks", Integer, nullable=False),
    Column("completed_last_24h", Integer, nullable=False),
)
