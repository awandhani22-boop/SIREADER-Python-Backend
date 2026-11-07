"""
Regression test untuk patch latensi SIREADER.

Mencakup:
  - collect_message_bundle dengan Session eksternal (tidak menutup Session).
  - collect_message_bundle tanpa Session eksternal (buka+tutup sendiri).
  - get_review_detail memakai SATU Session dan memuat manual_tp.
  - Hasil review identik dengan perilaku yang diharapkan.
  - Notification worker lifecycle: start, hanya satu worker, graceful shutdown.
  - Worker hanya mengirim PENDING, tidak mengirim ulang SENT.

Semua READ-ONLY terhadap jaringan: sqlite in-memory, tanpa Telegram/AI/MT5.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.models import (
    Base,
    AIAnalysisLog,
    TelegramMessage,
    TelegramOutbox,
)
from app.approval import service
from app.ai import message_aggregator
from app.approval import outbox


# ============================================================
# FIXTURE: sqlite in-memory dengan semua tabel
# ============================================================

@pytest.fixture
def db_factory(monkeypatch):
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)

    factory = sessionmaker(bind=engine, expire_on_commit=False)

    # Arahkan semua modul ke factory sqlite.
    monkeypatch.setattr(service, "SessionLocal", factory)
    monkeypatch.setattr(message_aggregator, "SessionLocal", factory)
    monkeypatch.setattr(outbox, "SessionLocal", factory)

    yield factory

    Base.metadata.drop_all(engine)
    engine.dispose()


def _add_analysis(factory, status="MANUAL_REVIEW", message_id=20519,
                  chat_id=-1003014486607):
    result = {
        "status": "ANALYZED",
        "final_analysis": {
            "status": "CLEAR",
            "confidence": 0.9,
            "summary": "Sinyal BUY",
            "extracted": {"direction": "BUY"},
            "evidence": [],
            "uncertainties": [],
        },
    }
    with factory() as db:
        log = AIAnalysisLog(
            chat_id=chat_id,
            target_message_id=message_id,
            status=status,
            result_json=json.dumps(result),
        )
        db.add(log)
        db.commit()
        db.refresh(log)
        return log.id


def _add_message(factory, chat_id=-1003014486607, message_id=20519,
                 text="3960-3962 tunggu cisd m1"):
    from datetime import datetime, timezone
    with factory() as db:
        msg = TelegramMessage(
            chat_id=chat_id,
            telegram_message_id=message_id,
            message_text=text,
            sender="mentor",
            telegram_date=datetime(2026, 11, 3, 11, 0, tzinfo=timezone.utc),
            received_at=datetime(2026, 11, 3, 11, 0, 1, tzinfo=timezone.utc),
            is_edited=False,
        )
        db.add(msg)
        db.commit()


# ============================================================
# collect_message_bundle: Session eksternal vs mandiri
# ============================================================

def test_bundle_dengan_session_eksternal_tidak_menutup(db_factory):
    _add_message(db_factory)

    with db_factory() as db:
        result = message_aggregator.collect_message_bundle(
            chat_id=-1003014486607,
            telegram_message_id=20519,
            db=db,
        )
        # Helper TIDAK boleh menutup Session milik pemanggil.
        assert db.is_active
        # Session masih bisa dipakai untuk query lain.
        count = db.scalar(select(TelegramMessage).where(
            TelegramMessage.telegram_message_id == 20519))
        assert count is not None

    assert result is not None
    assert result["target_message_id"] == 20519
    assert any(m["telegram_message_id"] == 20519
               for m in result["messages"])


def test_bundle_tanpa_session_eksternal_tetap_jalan(db_factory):
    _add_message(db_factory)

    result = message_aggregator.collect_message_bundle(
        chat_id=-1003014486607,
        telegram_message_id=20519,
    )
    assert result is not None
    assert result["target_message_id"] == 20519


def test_bundle_hasil_identik_dengan_dan_tanpa_session(db_factory):
    _add_message(db_factory)

    without = message_aggregator.collect_message_bundle(
        chat_id=-1003014486607, telegram_message_id=20519)

    with db_factory() as db:
        withdb = message_aggregator.collect_message_bundle(
            chat_id=-1003014486607, telegram_message_id=20519, db=db)

    assert without == withdb


# ============================================================
# get_review_detail: satu Session + manual_tp termuat
# ============================================================

def test_get_review_detail_memuat_manual_tp(db_factory):
    log_id = _add_analysis(db_factory)
    _add_message(db_factory)

    detail = service.get_review_detail(log_id)

    assert detail["status"] == "MANUAL_REVIEW"
    # Kunci manual_tp selalu ada (None jika belum diatur).
    assert "manual_tp" in detail
    assert detail["manual_tp"] is None
    assert detail["source_bundle"]["target_message_id"] == 20519


def test_get_review_detail_manual_tp_terisi_setelah_set(db_factory):
    log_id = _add_analysis(db_factory)
    _add_message(db_factory)

    service.set_manual_tp(
        analysis_log_id=log_id, tp_pips="50", user_id=12345)

    detail = service.get_review_detail(log_id)
    from decimal import Decimal
    assert detail["manual_tp"] is not None
    assert Decimal(str(detail["manual_tp"]["tp_pips"])) == Decimal("50")
    # 1 pip = 0.10 -> 50 pips = 5.0 (standar SIREADER dipertahankan).
    assert Decimal(str(detail["manual_tp"]["price_distance"])) == Decimal("5.0")


# ============================================================
# Notification worker lifecycle
# ============================================================

class _FakeTask:
    """Task tiruan untuk menguji logika start idempoten tanpa event loop."""
    def __init__(self, done=False):
        self._done = done

    def done(self):
        return self._done


class _FakeBotData(dict):
    pass


class _FakeApp:
    def __init__(self):
        self.bot_data = {}


def test_start_worker_hanya_satu_saat_sudah_aktif(monkeypatch):
    import app.approval.telegram_bot as tb

    app = _FakeApp()
    existing = _FakeTask(done=False)
    app.bot_data[tb.WORKER_TASK_KEY] = existing

    created = {"count": 0}

    def fake_create_task(coro, name=None):
        created["count"] += 1
        coro.close()  # jangan tinggalkan coroutine tak-ditunggu
        return _FakeTask(done=False)

    monkeypatch.setattr(tb.asyncio, "create_task", fake_create_task)

    result = tb._start_notification_worker(app)

    # Worker lama masih hidup -> TIDAK membuat worker kedua.
    assert created["count"] == 0
    assert result is existing


def test_start_worker_membuat_baru_jika_kosong(monkeypatch):
    import app.approval.telegram_bot as tb

    app = _FakeApp()
    created = {"count": 0}

    def fake_create_task(coro, name=None):
        created["count"] += 1
        coro.close()
        return _FakeTask(done=False)

    monkeypatch.setattr(tb.asyncio, "create_task", fake_create_task)

    tb._start_notification_worker(app)

    assert created["count"] == 1
    assert tb.WORKER_TASK_KEY in app.bot_data


def test_start_worker_mengganti_task_mati(monkeypatch):
    import app.approval.telegram_bot as tb

    app = _FakeApp()
    app.bot_data[tb.WORKER_TASK_KEY] = _FakeTask(done=True)
    created = {"count": 0}

    def fake_create_task(coro, name=None):
        created["count"] += 1
        coro.close()
        return _FakeTask(done=False)

    monkeypatch.setattr(tb.asyncio, "create_task", fake_create_task)

    tb._start_notification_worker(app)

    # Task lama sudah done -> buat worker baru.
    assert created["count"] == 1


def test_post_stop_membatalkan_dan_tidak_membuat_worker():
    import app.approval.telegram_bot as tb

    async def run():
        app = _FakeApp()

        cancelled = {"done": False}

        async def dummy_worker():
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                cancelled["done"] = True
                raise

        task = asyncio.create_task(dummy_worker())
        app.bot_data[tb.WORKER_TASK_KEY] = task
        await asyncio.sleep(0)  # biarkan worker mulai

        await tb.post_stop(app)

        # Worker dibatalkan dan TIDAK ada worker baru dibuat.
        assert cancelled["done"] is True
        assert tb.WORKER_TASK_KEY not in app.bot_data

    asyncio.run(run())


# ============================================================
# Outbox: hanya PENDING, tidak kirim ulang SENT
# ============================================================

def test_list_pending_hanya_status_pending(db_factory):
    log_id = _add_analysis(db_factory)

    with db_factory() as db:
        db.add(TelegramOutbox(
            event_key=f"MANUAL_REVIEW:{log_id}",
            analysis_log_id=log_id,
            recipient_chat_id=12345,
            event_type="MANUAL_REVIEW",
            status="PENDING",
        ))
        db.add(TelegramOutbox(
            event_key="MANUAL_REVIEW:999",
            analysis_log_id=999,
            recipient_chat_id=12345,
            event_type="MANUAL_REVIEW",
            status="SENT",
        ))
        db.commit()

    pending = outbox.list_pending_notification_ids()

    assert len(pending) == 1
    assert pending[0]["analysis_log_id"] == log_id


def test_claim_tidak_mengambil_sent(db_factory):
    log_id = _add_analysis(db_factory)

    with db_factory() as db:
        db.add(TelegramOutbox(
            event_key="MANUAL_REVIEW:777",
            analysis_log_id=777,
            recipient_chat_id=12345,
            event_type="MANUAL_REVIEW",
            status="SENT",
        ))
        db.commit()

    # Hanya ada SENT -> claim harus None.
    assert outbox.claim_next_notification() is None
