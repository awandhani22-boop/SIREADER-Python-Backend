"""Endpoint tests with SQLite stand-ins; not evidence of PostgreSQL/RLS grants."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, insert
from sqlalchemy.orm import sessionmaker
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import StaticPool

from app.api.config import api_settings
from app.api.dependencies import get_ro_session
from app.api.server import (
    create_app,
    validate_local_bind_host,
    validate_ro_database_identity,
    validate_ro_database_privileges,
    validate_approved_views_executable,
)
from app.api.views import (
    metadata,
    v_api_active_in_flight_tasks,
    v_api_active_market_watches,
    v_api_audit_decisions,
    v_api_queued_pending_messages,
    v_api_system_runtime_state,
    v_api_telemetry_usage,
    v_api_workforce_runtime,
    v_api_workforce_workers,
    v_api_workforce_ai_usage,
)

SECRET = "test-only-api-secret"
NOW = datetime.now(timezone.utc)


@pytest.fixture
def api_client(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def override_session():
        session = sessions()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr(api_settings, "SIREADER_API_AUTH_TOKEN", SECRET)
    app = create_app(manage_database_lifecycle=False)
    app.dependency_overrides[get_ro_session] = override_session
    with TestClient(app) as client:
        yield client, sessions
    engine.dispose()


def _headers():
    return {"Authorization": f"Bearer {SECRET}"}


def test_all_observability_endpoints_require_bearer(api_client):
    client, _ = api_client
    paths = [
        "/api/v1/system/status",
        "/api/v1/telemetry/usage",
        "/api/v1/pipeline/active",
        "/api/v1/market-watches/active",
        "/api/v1/audit/decisions",
        "/api/v1/workforce/runtime",
        "/api/v1/workforce/tasks",
        "/api/v1/workforce/attempts?task_id=task_12345678",
        "/api/v1/workforce/activity",
    ]
    for path in paths:
        response = client.get(path)
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"


def test_business_endpoints_are_get_only(api_client):
    client, _ = api_client
    for path in [
        "/api/v1/system/status",
        "/api/v1/telemetry/usage",
        "/api/v1/pipeline/active",
        "/api/v1/market-watches/active",
        "/api/v1/audit/decisions",
        "/api/v1/workforce/runtime",
        "/api/v1/workforce/tasks",
        "/api/v1/workforce/attempts?task_id=task_12345678",
        "/api/v1/workforce/activity",
    ]:
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            assert client.request(method, path, headers=_headers()).status_code == 405



def test_workforce_runtime_endpoint_returns_authenticated_worker_snapshot(api_client):
    client, sessions = api_client
    with sessions.begin() as session:
        session.execute(insert(v_api_workforce_runtime).values(
            observed_at=NOW, configured_workers=7, ready_workers=1,
            stale_workers=0, offline_workers=6, active_tasks=0,
            queued_tasks=2, waiting_review_tasks=0, dead_lettered_tasks=0,
            completed_last_24h=3,
        ))
        session.execute(insert(v_api_workforce_workers).values(
            worker_id="haiku", display_name="Haiku Worker",
            role="Fast Research & Intake Specialist", department="Research",
            enabled=True, accepting_tasks=True, max_concurrency=2, model_tier="FAST",
            capability_ids=["quick_research", "summarization"],
            process_status="READY", heartbeat_at=NOW, active_task_count=0,
            in_flight_provider_requests=0, schema_version=1,
            created_at=NOW, updated_at=NOW,
        ))
    response = client.get("/api/v1/workforce/runtime", headers=_headers())
    assert response.status_code == 200
    payload = response.json()
    assert payload["runtime"]["configured_workers"] == 7
    assert payload["runtime"]["ready_workers"] == 1
    assert payload["workers"][0]["worker_id"] == "haiku"
    assert payload["workers"][0]["process_status"] == "READY"
    assert payload["workers"][0]["capability_ids"] == ["quick_research", "summarization"]
    assert "input_payload" not in payload["workers"][0]


def test_workforce_read_endpoints_return_bounded_empty_collections(api_client):
    client, _ = api_client
    tasks = client.get("/api/v1/workforce/tasks?limit=10&offset=0", headers=_headers())
    activity = client.get("/api/v1/workforce/activity?limit=10&offset=0", headers=_headers())
    attempts = client.get(
        "/api/v1/workforce/attempts?task_id=task_12345678",
        headers=_headers(),
    )
    assert tasks.status_code == 200 and tasks.json()["total_count"] == 0
    assert activity.status_code == 200 and activity.json()["total_count"] == 0
    assert attempts.status_code == 404


def test_system_status_returns_none_when_runtime_view_empty(api_client):
    client, _ = api_client
    response = client.get("/api/v1/system/status", headers=_headers())
    assert response.status_code == 200
    payload = response.json()
    assert payload["database_status"] == "CONNECTED"
    assert payload["runtime_control"] is None
    assert "heartbeat" in payload["process_monitoring_note"].lower()


def test_system_status_reads_runtime_view(api_client):
    client, sessions = api_client
    with sessions.begin() as session:
        session.execute(insert(v_api_system_runtime_state).values(
            execution_mode="APPROVAL",
            trading_mode="DRY_RUN",
            auto_execution_enabled=False,
            auto_paused=True,
            updated_at=NOW,
        ))
    response = client.get("/api/v1/system/status", headers=_headers())
    assert response.status_code == 200
    assert response.json()["runtime_control"]["trading_mode"] == "DRY_RUN"


def test_telemetry_separates_currencies_for_totals_reservations_and_models(api_client):
    client, sessions = api_client
    usage_date = date.today()
    common = {
        "usage_date": usage_date,
        "provider": "test-provider",
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_read_tokens": 10,
        "cache_write_tokens": 5,
        "thinking_tokens": 0,
        "created_at": NOW,
        "completed_at": NOW,
    }
    with sessions.begin() as session:
        session.execute(insert(v_api_telemetry_usage).values(
            **common, model="haiku-test", tier="FAST", billing_currency="IDR",
            cost_rp=Decimal("1000"), reserved_rp=Decimal("200"),
            cost_usd=Decimal("999"), reserved_usd=Decimal("999"), status="COMPLETED",
        ))
        session.execute(insert(v_api_telemetry_usage).values(
            **common, model="sonnet-test", tier="REASONING", billing_currency="USD",
            cost_rp=Decimal("888"), reserved_rp=Decimal("888"),
            cost_usd=Decimal("0.50"), reserved_usd=Decimal("0.20"), status="RESERVED",
        ))

    response = client.get(
        f"/api/v1/telemetry/usage?start_date={usage_date.isoformat()}&end_date={usage_date.isoformat()}",
        headers=_headers(),
    )
    assert response.status_code == 200
    payload = response.json()
    currencies = {item["currency"]: item for item in payload["currencies"]}
    assert Decimal(currencies["IDR"]["total_cost"]) == Decimal("1000")
    assert Decimal(currencies["IDR"]["total_reserved"]) == Decimal("200")
    assert Decimal(currencies["IDR"]["total_projected"]) == Decimal("1200")
    assert Decimal(currencies["USD"]["total_cost"]) == Decimal("0.5")
    assert Decimal(currencies["USD"]["total_reserved"]) == Decimal("0.2")
    assert Decimal(currencies["USD"]["total_projected"]) == Decimal("0.7")
    models = {item["model"]: item for item in payload["models"]}
    assert Decimal(models["haiku-test"]["cost"]) == Decimal("1000")
    assert Decimal(models["haiku-test"]["reserved"]) == Decimal("200")
    assert Decimal(models["sonnet-test"]["cost"]) == Decimal("0.5")
    assert Decimal(models["sonnet-test"]["reserved"]) == Decimal("0.2")


def test_telemetry_budget_observability_preserves_currency_and_links_workforce_costs(api_client, monkeypatch):
    client, sessions = api_client
    from app.api.config import api_settings as settings
    monkeypatch.setattr(settings, "AI_ANTHROPIC_DAILY_BUDGET_USD", Decimal("5.00"))

    from zoneinfo import ZoneInfo
    today = datetime.now(ZoneInfo("Asia/Jakarta")).date()
    common = {
        "usage_date": today,
        "provider": "tokenmurah",
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "thinking_tokens": 0,
        "created_at": NOW,
        "completed_at": NOW,
    }
    with sessions.begin() as session:
        session.execute(insert(v_api_telemetry_usage).values(
            **common, model="haiku-test", tier="FAST", billing_currency="IDR",
            cost_rp=Decimal("7000"), reserved_rp=Decimal("1000"),
            cost_usd=Decimal("0"), reserved_usd=Decimal("0"), status="COMPLETED",
        ))
        session.execute(insert(v_api_telemetry_usage).values(
            **common, model="anthropic-haiku", tier="FAST", billing_currency="USD",
            cost_rp=Decimal("0"), reserved_rp=Decimal("0"),
            cost_usd=Decimal("3.50"), reserved_usd=Decimal("0.50"), status="RESERVED",
        ))
        session.execute(insert(v_api_workforce_ai_usage).values(
            usage_date=today, worker_id="haiku", task_id="task_12345678",
            attempt_id="attempt_12345678", provider="tokenmurah",
            model="haiku-test", tier="FAST", billing_currency="IDR",
            cost_rp=Decimal("7000"), reserved_rp=Decimal("1000"),
            cost_usd=Decimal("0"), reserved_usd=Decimal("0"), status="COMPLETED",
        ))
    response = client.get("/api/v1/telemetry/usage", headers=_headers())
    assert response.status_code == 200
    budget = response.json()["budget"]
    assert budget["daily_idr"]["currency"] == "IDR"
    assert Decimal(budget["daily_idr"]["actual_cost"]) == Decimal("7000")
    assert Decimal(budget["daily_idr"]["reserved_cost"]) == Decimal("1000")
    assert Decimal(budget["daily_idr"]["budget_amount"]) == Decimal(str(settings.AI_TARGET_DAILY_BUDGET_RP))
    assert Decimal(budget["daily_idr"]["hard_limit_amount"]) == Decimal(str(settings.AI_HARD_DAILY_LIMIT_RP))
    assert budget["daily_idr"]["warning_level"] == "SOFT"
    assert budget["daily_anthropic_usd"]["currency"] == "USD"
    assert budget["daily_anthropic_usd"]["window"] == "DAILY"
    assert Decimal(budget["daily_anthropic_usd"]["actual_cost"]) == Decimal("3.50")
    assert Decimal(budget["daily_anthropic_usd"]["reserved_cost"]) == Decimal("0.50")
    assert Decimal(budget["daily_anthropic_usd"]["projected_cost"]) == Decimal("4.00")
    assert Decimal(budget["daily_anthropic_usd"]["hard_limit_amount"]) == Decimal("5.00")
    assert budget["daily_anthropic_usd"]["warning_level"] == "SOFT"
    assert Decimal(budget["monthly_anthropic_usd"]["projected_cost"]) == Decimal("4.00")
    assert budget["monthly_anthropic_usd"]["warning_level"] == "SOFT"
    tiers = {item["tier"]: item for item in budget["idr_tiers"]}
    assert Decimal(tiers["FAST"]["projected_cost"]) == Decimal("8000")
    assert tiers["FAST"]["budget_reached"] is False
    assert budget["worker_breakdown"][0]["worker_id"] == "haiku"
    assert budget["recent_task_costs"][0]["task_id"] == "task_12345678"
    assert budget["recent_task_costs"][0]["worker_id"] == "haiku"


def test_telemetry_rejects_unknown_currency(api_client):
    client, sessions = api_client
    with sessions.begin() as session:
        session.execute(insert(v_api_telemetry_usage).values(
            usage_date=date.today(), provider="test", model="unknown", tier="FAST",
            input_tokens=0, output_tokens=0, cache_read_tokens=0, cache_write_tokens=0,
            thinking_tokens=0, billing_currency="EUR", cost_rp=Decimal("1000"),
            reserved_rp=Decimal("0"), cost_usd=Decimal("0"), reserved_usd=Decimal("0"),
            status="COMPLETED", created_at=NOW, completed_at=NOW,
        ))
    response = client.get("/api/v1/telemetry/usage", headers=_headers())
    assert response.status_code == 500
    assert "Unsupported billing currency" in response.json()["detail"]


def test_telemetry_rejects_invalid_date_range(api_client):
    client, _ = api_client
    response = client.get(
        "/api/v1/telemetry/usage?start_date=2026-11-10&end_date=2026-11-01",
        headers=_headers(),
    )
    assert response.status_code == 422


def test_pipeline_reports_unconfirmed_liveness(api_client):
    client, sessions = api_client
    created = NOW - timedelta(seconds=12)
    with sessions.begin() as session:
        session.execute(insert(v_api_active_in_flight_tasks).values(
            task_id=11, status="PROCESSING", created_at=created, elapsed_seconds=12.0,
        ))
        session.execute(insert(v_api_queued_pending_messages).values(
            queue_item_id=21, status="PENDING", queue_reason="OTHER", attempts=2,
            claimed_at=None, created_at=created, updated_at=NOW,
        ))
    response = client.get("/api/v1/pipeline/active", headers=_headers())
    assert response.status_code == 200
    payload = response.json()
    assert payload["in_flight_tasks"][0]["liveness_status"] == "IN_FLIGHT_UNCONFIRMED"
    assert payload["queued_messages"][0]["queue_reason"] == "OTHER"
    assert "claimed_by" not in payload["queued_messages"][0]
    assert payload["total_in_flight"] == 1
    assert payload["total_queued"] == 1


def test_market_watches_returns_only_view_fields(api_client):
    client, sessions = api_client
    with sessions.begin() as session:
        session.execute(insert(v_api_active_market_watches).values(
            watch_id=31, symbol="XAUUSD", status="WAITING_FOR_SPREAD",
            spread_pips=Decimal("8.5"), maximum_spread_pips=Decimal("30"),
            last_bid=Decimal("4000.10"), last_ask=Decimal("4000.18"),
            check_count=3, last_checked_at=NOW, next_check_at=NOW + timedelta(seconds=5),
            created_at=NOW - timedelta(minutes=1), updated_at=NOW,
        ))
    response = client.get("/api/v1/market-watches/active", headers=_headers())
    assert response.status_code == 200
    payload = response.json()
    assert payload["total_active_watches"] == 1
    assert payload["watches"][0]["symbol"] == "XAUUSD"
    assert "broker_server" not in payload["watches"][0]
    assert "last_reason" not in payload["watches"][0]


def test_audit_endpoint_returns_sanitized_decision(api_client):
    client, sessions = api_client
    with sessions.begin() as session:
        session.execute(insert(v_api_audit_decisions).values(
            audit_id=41, decision="APPROVED", previous_status="MANUAL_REVIEW",
            new_status="APPROVED", reviewer_role="Authorized Approver", decided_at=NOW,
        ))
    response = client.get("/api/v1/audit/decisions?limit=10&offset=0", headers=_headers())
    assert response.status_code == 200
    item = response.json()["decisions"][0]
    assert item["reviewer_role"] == "Authorized Approver"
    assert not {"analysis_log_id", "note", "reviewer"} & set(item)


def test_local_bind_rejects_public_interface():
    with pytest.raises(RuntimeError):
        validate_local_bind_host("0.0.0.0")
    assert validate_local_bind_host("127.0.0.1") == "127.0.0.1"


@pytest.mark.parametrize("identity", [
    {
        "role_name": "postgres",
        "transaction_read_only": "on",
        "rolcanlogin": True,
        "rolsuper": False,
        "rolinherit": False,
        "rolcreatedb": False,
        "rolcreaterole": False,
        "rolreplication": False,
        "rolbypassrls": False,
    },
    {
        "role_name": "sireader_api_ro",
        "transaction_read_only": "off",
        "rolcanlogin": True,
        "rolsuper": False,
        "rolinherit": False,
        "rolcreatedb": False,
        "rolcreaterole": False,
        "rolreplication": False,
        "rolbypassrls": False,
    },
    {
        "role_name": "sireader_api_ro",
        "transaction_read_only": "on",
        "rolcanlogin": True,
        "rolsuper": True,
        "rolinherit": False,
        "rolcreatedb": False,
        "rolcreaterole": False,
        "rolreplication": False,
        "rolbypassrls": False,
    },
])
def test_database_identity_guard_rejects_unsafe_connections(identity):
    with pytest.raises(RuntimeError):
        validate_ro_database_identity(identity)


def test_database_identity_guard_accepts_dedicated_readonly_role():
    validate_ro_database_identity({
        "role_name": "sireader_api_ro",
        "transaction_read_only": "on",
        "rolcanlogin": True,
        "rolsuper": False,
        "rolinherit": False,
        "rolcreatedb": False,
        "rolcreaterole": False,
        "rolreplication": False,
        "rolbypassrls": False,
    })


def test_database_identity_guard_rejects_missing_identity():
    with pytest.raises(RuntimeError):
        validate_ro_database_identity(None)


def _safe_privilege_snapshot():
    return {
        "api_schema_usage": True,
        "api_schema_create": False,
        "api_view_write_access": False,
        "api_view_column_write_access": False,
        "api_has_role_memberships": False,
        "api_base_table_access": False,
        "api_base_table_maintain": False,
        "api_base_column_access": False,
        "public_table_grants": False,
        "public_column_grants": False,
        "owner_schema_create": False,
        "owner_base_table_access": False,
        "owner_base_table_maintain": False,
        "owner_has_role_memberships": False,
        "owner_unapproved_column_access": False,
        "owner_column_allowlist_ok": True,
        "approved_view_count": 14,
        "approved_view_owners_ok": True,
        "approved_view_select_ok": True,
        "view_owner_attributes_ok": True,
        "approved_view_security_barrier_ok": True,
    }


def test_database_privilege_guard_accepts_strict_view_only_boundary():
    validate_ro_database_privileges(_safe_privilege_snapshot())


@pytest.mark.parametrize("key,value", [
    ("api_schema_usage", False),
    ("api_schema_create", True),
    ("api_view_write_access", True),
    ("api_view_column_write_access", True),
    ("api_has_role_memberships", True),
    ("api_base_table_access", True),
    ("api_base_table_maintain", True),
    ("api_base_column_access", True),
    ("public_table_grants", True),
    ("public_column_grants", True),
    ("owner_schema_create", True),
    ("owner_base_table_access", True),
    ("owner_base_table_maintain", True),
    ("owner_has_role_memberships", True),
    ("owner_unapproved_column_access", True),
    ("owner_column_allowlist_ok", False),
    ("approved_view_count", 5),
    ("approved_view_owners_ok", False),
    ("approved_view_select_ok", False),
    ("view_owner_attributes_ok", False),
    ("approved_view_security_barrier_ok", False),
])
def test_database_privilege_guard_rejects_unsafe_boundary(key, value):
    snapshot = _safe_privilege_snapshot()
    snapshot[key] = value
    with pytest.raises(RuntimeError):
        validate_ro_database_privileges(snapshot)


def test_database_privilege_guard_rejects_missing_snapshot():
    with pytest.raises(RuntimeError):
        validate_ro_database_privileges(None)


def test_approved_views_execution_probe_checks_exact_allowlist():
    class FakeResult:
        def first(self):
            return None

    class FakeSession:
        def __init__(self):
            self.statements = []

        def execute(self, statement):
            self.statements.append(str(statement))
            return FakeResult()

    session = FakeSession()
    validate_approved_views_executable(session)
    assert len(session.statements) == 14
    assert all(statement.startswith("SELECT 1 FROM public.") for statement in session.statements)
    assert all(statement.endswith("LIMIT 1") for statement in session.statements)
    assert {statement.split("FROM public.", 1)[1].split()[0] for statement in session.statements} == {
        "v_api_system_runtime_state",
        "v_api_telemetry_usage",
        "v_api_active_in_flight_tasks",
        "v_api_queued_pending_messages",
        "v_api_active_market_watches",
        "v_api_audit_decisions",
        "v_api_workforce_workers",
        "v_api_workforce_tasks",
        "v_api_workforce_attempts",
        "v_api_workforce_activity",
        "v_api_workforce_runtime",
        "v_api_workforce_ai_usage",
        "v_api_workforce_task_reviews",
        "v_api_workforce_task_timeline",
    }


def test_approved_views_execution_probe_propagates_view_failure():
    class FailingSession:
        def execute(self, statement):
            raise RuntimeError("view query failed")

    with pytest.raises(RuntimeError, match="view query failed"):
        validate_approved_views_executable(FailingSession())


def test_database_failure_returns_503_without_leaking_connection_details(monkeypatch):
    class BrokenSession:
        def execute(self, statement):
            raise OperationalError("SELECT runtime state", {}, OSError("private-db-host password=hidden"))

        def close(self):
            pass

    def broken_dependency():
        yield BrokenSession()

    monkeypatch.setattr(api_settings, "SIREADER_API_AUTH_TOKEN", SECRET)
    app = create_app(manage_database_lifecycle=False)
    app.dependency_overrides[get_ro_session] = broken_dependency

    with TestClient(app) as client:
        response = client.get("/api/v1/system/status", headers=_headers())

    assert response.status_code == 503
    assert response.json() == {"detail": "Read-only database is unavailable."}
    assert "private-db-host" not in response.text
    assert "password=hidden" not in response.text


def test_telemetry_budget_warning_levels_follow_cost_guard_configuration(monkeypatch):
    from app.api.routes import telemetry

    monkeypatch.setattr(
        telemetry.api_settings,
        "AI_ANTHROPIC_WARNING_THRESHOLDS",
        "0.65,0.82,0.93",
    )
    assert telemetry._warning_level(Decimal("64"), Decimal("100")) == ("NONE", None)
    assert telemetry._warning_level(Decimal("65"), Decimal("100")) == ("SOFT", 65)
    assert telemetry._warning_level(Decimal("82"), Decimal("100")) == ("WARNING", 82)
    assert telemetry._warning_level(Decimal("93"), Decimal("100")) == ("CRITICAL", 93)
    assert telemetry._warning_level(Decimal("101"), Decimal("100"), Decimal("500")) == ("CRITICAL", 93)
    assert telemetry._warning_level(Decimal("500"), Decimal("100"), Decimal("500")) == ("HARD_LIMIT", 100)
