from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.ai import cost_guard
from app.database.models import AIUsageLog, Base


@pytest.fixture
def db_factory(monkeypatch):
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    monkeypatch.setattr(cost_guard, "SessionLocal", factory)

    yield factory

    Base.metadata.drop_all(engine)
    engine.dispose()


def add_usage(factory, request_id, analysis_log_id, status):
    with factory() as db:
        db.add(
            AIUsageLog(
                request_id=request_id,
                usage_date=date(2026, 11, 5),
                provider="omniroute",
                model="test/model",
                tier="FAST",
                input_tokens=100,
                output_tokens=50,
                billing_currency="IDR",
                cost_rp=Decimal("0"),
                reserved_rp=Decimal("0"),
                cost_usd=Decimal("0"),
                reserved_usd=Decimal("0"),
                analysis_log_id=analysis_log_id,
                status=status,
            )
        )
        db.commit()


def test_status_semua_transaksi_completed(db_factory):
    add_usage(db_factory, "req-1", 101, "COMPLETED")
    add_usage(db_factory, "req-2", 101, "COMPLETED")

    result = cost_guard.get_analysis_usage_status(101)

    assert result["total"] == 2
    assert result["completed"] == 2
    assert result["failed"] == 0
    assert result["reserved"] == 0
    assert result["unknown"] == 0
    assert result["has_uncertain"] is False
    assert result["all_terminal"] is True


def test_status_transaksi_campuran(db_factory):
    add_usage(db_factory, "req-1", 102, "COMPLETED")
    add_usage(db_factory, "req-2", 102, "FAILED")
    add_usage(db_factory, "req-3", 102, "RESERVED")
    add_usage(db_factory, "req-4", 102, "UNKNOWN")

    result = cost_guard.get_analysis_usage_status(102)

    assert result["total"] == 4
    assert result["completed"] == 1
    assert result["failed"] == 1
    assert result["reserved"] == 1
    assert result["unknown"] == 1
    assert result["has_uncertain"] is True
    assert result["all_terminal"] is False


def test_status_tanpa_transaksi(db_factory):
    result = cost_guard.get_analysis_usage_status(103)

    assert result["total"] == 0
    assert result["transactions"] == []
    assert result["has_uncertain"] is False
    assert result["all_terminal"] is True


def test_analysis_log_id_none_ditolak():
    with pytest.raises(ValueError, match="analysis_log_id wajib diisi"):
        cost_guard.get_analysis_usage_status(None)
