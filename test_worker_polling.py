"""
Regression test untuk notification worker adaptive polling.

Mencakup:
  - Worker idle polling cepat (~1s), bukan backoff panjang.
  - Event yang masuk saat worker idle terdeteksi dalam <= 2 detik.
  - Hanya PENDING yang dikirim; review yang sudah diputuskan di-skip.
  - Graceful shutdown (cancel + CancelledError).
  - Warning dicatat jika pengiriman > 5 detik (dengan analysis_log_id).

Memakai asyncio.run (tanpa pytest-asyncio). Tidak memanggil Telegram API
nyata, tidak AI, tidak MT5. send_message dan fungsi DB di-mock.
"""
from __future__ import annotations

import asyncio
import logging
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import app.approval.telegram_bot as tb


class _FakeApp:
    def __init__(self, send_message):
        self.bot = SimpleNamespace(send_message=send_message)
        self.bot_data = {}


def _make_detail(status="MANUAL_REVIEW"):
    return {
        "analysis_log_id": 1,
        "chat_id": -1003014486607,
        "target_message_id": 123,
        "status": status,
        "final_analysis": {"extracted": {}, "evidence": [],
                           "uncertainties": []},
        "source_bundle": {"messages": []},
        "manual_tp": None,
    }


async def _run_worker_briefly(app, seconds):
    task = asyncio.create_task(tb.notification_worker(app))
    await asyncio.sleep(seconds)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def test_event_saat_idle_terdeteksi_cepat(monkeypatch):
    """Event dimasukkan saat worker idle; harus diklaim <= 2 detik."""
    claimed_at = {}
    event_available = {"ready": False}
    t_insert = {}

    def fake_claim():
        if event_available["ready"] and "t" not in claimed_at:
            claimed_at["t"] = time.perf_counter()
            return {"id": 10, "analysis_log_id": 1,
                    "recipient_chat_id": 123, "attempts": 1}
        return None

    monkeypatch.setattr(tb, "claim_next_notification", fake_claim)
    monkeypatch.setattr(tb, "get_review_detail", lambda aid: _make_detail())
    monkeypatch.setattr(tb, "mark_notification_sent", lambda **kw: None)

    send_message = AsyncMock(return_value=SimpleNamespace(message_id=55))
    app = _FakeApp(send_message)

    async def scenario():
        task = asyncio.create_task(tb.notification_worker(app))
        await asyncio.sleep(1.2)          # worker idle dulu
        t_insert["t"] = time.perf_counter()
        event_available["ready"] = True   # event masuk saat idle
        for _ in range(30):               # tunggu deteksi, maks 3s
            if "t" in claimed_at:
                break
            await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())

    assert "t" in claimed_at, "event tidak pernah diklaim"
    detect_delay = claimed_at["t"] - t_insert["t"]
    assert detect_delay <= 2.0, f"deteksi {detect_delay:.2f}s > 2s"
    send_message.assert_awaited_once()


def test_idle_polling_interval_sekitar_satu_detik(monkeypatch):
    """Saat kosong, worker memanggil claim ~1Hz (bukan backoff lambat)."""
    calls = {"n": 0}

    def counting():
        calls["n"] += 1
        return None

    monkeypatch.setattr(tb, "claim_next_notification", counting)
    app = _FakeApp(AsyncMock())

    asyncio.run(_run_worker_briefly(app, 3.3))

    # ~1 poll/detik -> 3-5 kali dalam 3.3s. Backoff akan jauh lebih sedikit.
    assert 3 <= calls["n"] <= 5, f"jumlah poll={calls['n']} di luar 3-5"


def test_skip_review_yang_sudah_diputuskan(monkeypatch):
    """Analisis non-MANUAL_REVIEW ditandai skipped, tidak dikirim."""
    returned = {"done": False}

    def fake_claim():
        if not returned["done"]:
            returned["done"] = True
            return {"id": 11, "analysis_log_id": 1,
                    "recipient_chat_id": 123, "attempts": 1}
        return None

    skipped = {}
    monkeypatch.setattr(tb, "claim_next_notification", fake_claim)
    monkeypatch.setattr(tb, "get_review_detail",
                        lambda aid: _make_detail(status="APPROVED"))
    monkeypatch.setattr(
        tb, "mark_notification_skipped",
        lambda oid, reason: skipped.update(id=oid, reason=reason))

    send_message = AsyncMock()
    app = _FakeApp(send_message)

    asyncio.run(_run_worker_briefly(app, 0.5))

    assert skipped.get("id") == 11
    send_message.assert_not_awaited()


def test_graceful_shutdown(monkeypatch):
    """Worker berhenti bersih saat di-cancel."""
    monkeypatch.setattr(tb, "claim_next_notification", lambda: None)
    app = _FakeApp(AsyncMock())

    async def scenario():
        task = asyncio.create_task(tb.notification_worker(app))
        await asyncio.sleep(0.3)
        task.cancel()
        raised = False
        try:
            await task
        except asyncio.CancelledError:
            raised = True
        assert raised, "CancelledError tidak muncul"

    asyncio.run(scenario())


def test_warning_jika_kirim_lebih_lima_detik(monkeypatch, caplog):
    """Pengiriman > 5 detik menghasilkan warning dengan analysis_log_id."""
    done = {"v": False}

    def fake_claim():
        if not done["v"]:
            done["v"] = True
            return {"id": 12, "analysis_log_id": 1,
                    "recipient_chat_id": 123, "attempts": 1}
        return None

    monkeypatch.setattr(tb, "claim_next_notification", fake_claim)
    monkeypatch.setattr(tb, "get_review_detail", lambda aid: _make_detail())
    monkeypatch.setattr(tb, "mark_notification_sent", lambda **kw: None)

    # Paksa send_elapsed > 5s dengan memajukan perf_counter:
    # panggilan pertama = send_start, kedua = setelah kirim (+6s).
    base = time.perf_counter()
    seq = iter([base, base + 6.0])
    real_pc = time.perf_counter

    def fake_pc():
        try:
            return next(seq)
        except StopIteration:
            return real_pc()

    monkeypatch.setattr(tb.time, "perf_counter", fake_pc)

    app = _FakeApp(AsyncMock(return_value=SimpleNamespace(message_id=1)))

    with caplog.at_level(logging.WARNING, logger="SIREADER_BOT"):
        asyncio.run(_run_worker_briefly(app, 0.5))

    assert any("LAMBAT" in r.message for r in caplog.records), \
        "warning lambat tidak tercatat"
