import pytest

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.models import (
    Base,
    AIAnalysisLog,
    RuntimeControlState,
    ManualTPSetting,
)
from app.approval import service


@pytest.fixture
def control_db(monkeypatch):
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    Base.metadata.create_all(engine)
    factory = sessionmaker(
        bind=engine,
        expire_on_commit=False,
    )

    monkeypatch.setattr(
        service,
        "SessionLocal",
        factory,
    )

    yield factory

    Base.metadata.drop_all(engine)
    engine.dispose()


def add_analysis(factory, status="MANUAL_REVIEW"):
    with factory() as db:
        log = AIAnalysisLog(
            chat_id=-1003014486607,
            target_message_id=123,
            status=status,
        )
        db.add(log)
        db.commit()
        db.refresh(log)
        return log.id


def test_default_runtime_aman(control_db):
    state = service.get_runtime_control()

    assert state["execution_mode"] == "APPROVAL"
    assert state["trading_mode"] == "DRY_RUN"
    assert state["auto_execution_enabled"] is False
    assert state["auto_paused"] is True


def test_set_approval_mode(control_db):
    state = service.set_approval_mode(123456)

    assert state["execution_mode"] == "APPROVAL"
    assert state["auto_execution_enabled"] is False
    assert state["updated_by"] == 123456


def test_pause_auto(control_db):
    state = service.pause_auto(123456)

    assert state["auto_execution_enabled"] is False
    assert state["auto_paused"] is True


def test_manual_tp_50_pips(control_db):
    log_id = add_analysis(control_db)

    result = service.set_manual_tp(
        log_id,
        50,
        123456,
    )

    assert result["tp_pips"] == 50
    assert result["price_distance"] == 5


def test_manual_tp_menyimpan_riwayat(control_db):
    log_id = add_analysis(control_db)

    service.set_manual_tp(log_id, 50, 123456)
    service.set_manual_tp(log_id, 75, 123456)

    with control_db() as db:
        records = db.scalars(
            select(ManualTPSetting).where(
                ManualTPSetting.analysis_log_id == log_id
            )
        ).all()

        assert len(records) == 2

    latest = service.get_manual_tp_setting(log_id)

    assert latest["tp_pips"] == 75
    assert latest["price_distance"] == 7.5


def test_tp_nol_ditolak(control_db):
    log_id = add_analysis(control_db)

    with pytest.raises(ValueError):
        service.set_manual_tp(log_id, 0, 123456)


def test_tp_untuk_rejected_ditolak(control_db):
    log_id = add_analysis(control_db, "REJECTED")

    with pytest.raises(ValueError):
        service.set_manual_tp(log_id, 50, 123456)
