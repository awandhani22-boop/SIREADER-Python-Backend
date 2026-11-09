"""Comprehensive tests for Governance & Workforce Review Read API (Port 8000)."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, insert
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.config import api_settings
from app.api.dependencies import get_ro_session
from app.api.server import create_app
from app.api.views import metadata, v_api_workforce_task_reviews

AUTH_TOKEN = "phase5-governance-test-token-012345"
AUTH_HEADER = {"Authorization": f"Bearer {AUTH_TOKEN}"}
NOW = datetime.now(timezone.utc)


@pytest.fixture
def gov_client(monkeypatch):
    monkeypatch.setattr(api_settings, "SIREADER_API_AUTH_TOKEN", AUTH_TOKEN)
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def override_session():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app = create_app(manage_database_lifecycle=False)
    app.dependency_overrides[get_ro_session] = override_session
    with TestClient(app) as client:
        yield client, session_factory
    engine.dispose()


def _seed_review_row(
    session_factory,
    *,
    review_id: str,
    status: str = "PENDING",
    expires_delta_hours: int = 24,
    original_output: dict | None = None,
    adjusted_output: dict | None = None,
    payload_purged: bool = False,
):
    with session_factory() as session:
        session.execute(
            insert(v_api_workforce_task_reviews).values(
                review_id=review_id,
                task_id=f"task_{review_id}",
                attempt_id=f"att_{review_id}",
                status=status,
                original_output_sha256="a" * 64,
                adjusted_output_sha256="b" * 64 if status == "EDITED" else None,
                adjudicated_by="ops-user" if status != "PENDING" else None,
                adjudicated_at=NOW if status != "PENDING" else None,
                reason_code=None,
                notes=None,
                review_expires_at=NOW + timedelta(hours=expires_delta_hours),
                created_at=NOW,
                updated_at=NOW,
                original_output=original_output if not payload_purged else None,
                adjusted_output=adjusted_output if not payload_purged else None,
                payload_purged_at=NOW if payload_purged else None,
            )
        )
        session.commit()


def test_reviews_endpoints_require_bearer_token(gov_client):
    client, _ = gov_client
    endpoints = [
        "/api/v1/governance/reviews/pending",
        "/api/v1/workforce/reviews/pending",
        "/api/v1/governance/reviews/rev_test_00000001",
        "/api/v1/workforce/reviews/rev_test_00000001",
    ]
    for url in endpoints:
        resp = client.get(url)
        assert resp.status_code == 401, f"{url} should require auth"


def test_pending_reviews_endpoint_returns_only_pending_and_paginates(gov_client):
    client, session_factory = gov_client
    # Seed 3 reviews: 2 PENDING (different expiry), 1 APPROVED
    _seed_review_row(session_factory, review_id="rev_test_pending_01", status="PENDING", expires_delta_hours=10)
    _seed_review_row(session_factory, review_id="rev_test_pending_02", status="PENDING", expires_delta_hours=2)
    _seed_review_row(session_factory, review_id="rev_test_approved_01", status="APPROVED", expires_delta_hours=5)

    # Test governance endpoint
    resp = client.get("/api/v1/governance/reviews/pending", headers=AUTH_HEADER)
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_count"] == 2
    assert len(data["reviews"]) == 2
    # Verify ordering: expires sooner (2h) comes first
    assert data["reviews"][0]["review_id"] == "rev_test_pending_02"
    assert data["reviews"][1]["review_id"] == "rev_test_pending_01"

    # Test pagination
    paged_resp = client.get("/api/v1/governance/reviews/pending?limit=1&offset=0", headers=AUTH_HEADER)
    assert paged_resp.status_code == 200
    paged_data = paged_resp.json()
    assert len(paged_data["reviews"]) == 1
    assert paged_data["reviews"][0]["review_id"] == "rev_test_pending_02"
    assert paged_data["total_count"] == 2

    # Offset 1
    offset_resp = client.get("/api/v1/governance/reviews/pending?limit=1&offset=1", headers=AUTH_HEADER)
    assert offset_resp.status_code == 200
    offset_data = offset_resp.json()
    assert len(offset_data["reviews"]) == 1
    assert offset_data["reviews"][0]["review_id"] == "rev_test_pending_01"

    # Test workforce alias endpoint
    wf_resp = client.get("/api/v1/workforce/reviews/pending", headers=AUTH_HEADER)
    assert wf_resp.status_code == 200
    assert wf_resp.json()["total_count"] == 2


def test_review_detail_returns_404_for_unknown(gov_client):
    client, _ = gov_client
    resp = client.get("/api/v1/governance/reviews/rev_nonexistent_01", headers=AUTH_HEADER)
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Review not found."


def test_review_detail_sanitizes_payloads_defense_in_depth(gov_client):
    client, session_factory = gov_client
    raw_output = {
        "summary": "Financial analysis generated",
        "secret_key": "sk-proj-0123456789abcdef012345",
        "nested": {"token": "Bearer supersecrettokenvalue"},
    }
    _seed_review_row(
        session_factory,
        review_id="rev_test_sensitive_01",
        status="PENDING",
        original_output=raw_output,
    )

    resp = client.get("/api/v1/governance/reviews/rev_test_sensitive_01", headers=AUTH_HEADER)
    assert resp.status_code == 200
    data = resp.json()
    assert data["review_id"] == "rev_test_sensitive_01"
    assert data["status"] == "PENDING"

    orig = data["original_output"]
    assert orig["summary"] == "Financial analysis generated"
    assert orig["secret_key"] == "[REDACTED]"
    assert orig["nested"]["token"] == "[REDACTED]"

    # Verify workforce alias route also works
    wf_resp = client.get("/api/v1/workforce/reviews/rev_test_sensitive_01", headers=AUTH_HEADER)
    assert wf_resp.status_code == 200
    assert wf_resp.json()["original_output"]["secret_key"] == "[REDACTED]"


def test_review_detail_purged_payload_returns_none(gov_client):
    client, session_factory = gov_client
    _seed_review_row(
        session_factory,
        review_id="rev_test_purged_01",
        status="APPROVED",
        payload_purged=True,
    )

    resp = client.get("/api/v1/governance/reviews/rev_test_purged_01", headers=AUTH_HEADER)
    assert resp.status_code == 200
    data = resp.json()
    assert data["original_output"] is None
    assert data["adjusted_output"] is None
    assert data["payload_purged_at"] is not None
    assert data["original_output_sha256"] == "a" * 64
