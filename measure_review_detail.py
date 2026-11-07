"""
Mengukur get_review_detail SESUDAH patch (satu Session) vs simulasi
perilaku lama (dua Session terpisah: detail + bundle + manual TP).

READ-ONLY: tidak mengubah DB, tidak approval, tidak kirim order MT5,
tidak kirim pesan Telegram, tidak panggil AI.

Memakai satu analysis_log_id MANUAL_REVIEW yang sudah ada. Jika tidak
ada, keluar dengan pesan jelas tanpa membuat data.
"""
from __future__ import annotations

import time
import statistics

from sqlalchemy import select

from app.database.connection import SessionLocal
from app.database.models import AIAnalysisLog
from app.approval import service
from app.ai.message_aggregator import collect_message_bundle


def pick_analysis_id():
    with SessionLocal() as db:
        log = db.scalar(
            select(AIAnalysisLog)
            .where(AIAnalysisLog.status == "MANUAL_REVIEW")
            .order_by(AIAnalysisLog.created_at.asc())
            .limit(1)
        )
        if log is None:
            # fallback: analisis apa pun agar tetap bisa ukur round-trip.
            log = db.scalar(
                select(AIAnalysisLog)
                .order_by(AIAnalysisLog.id.asc())
                .limit(1)
            )
        return log.id if log else None


def simulate_old_two_sessions(analysis_id):
    """Meniru perilaku lama: get_review_detail versi lama membuka
    Session untuk log+decisions, LALU collect_message_bundle membuka
    Session kedua, LALU get_manual_tp_setting membuka Session ketiga."""
    detail = service.get_review_detail(analysis_id)  # kini 1 session
    # Tambah 1 checkout terpisah utk bundle (seperti versi lama).
    _ = collect_message_bundle(
        chat_id=detail["chat_id"],
        telegram_message_id=detail["target_message_id"],
    )
    # Tambah 1 checkout terpisah utk manual TP (seperti versi lama).
    _ = service.get_manual_tp_setting(analysis_id)
    return detail


def measure(label, fn, n=4):
    samples = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t0) * 1000.0)
    print(f"{label:<42} n={n}  "
          f"min={min(samples):8.1f}ms  "
          f"med={statistics.median(samples):8.1f}ms  "
          f"max={max(samples):8.1f}ms")
    return samples


def main():
    print("=" * 64)
    print("get_review_detail — SESUDAH PATCH")
    print("=" * 64)
    print("READ-ONLY. Tidak ubah DB / approval / order / AI.")
    print()

    analysis_id = pick_analysis_id()
    if analysis_id is None:
        print("Tidak ada AIAnalysisLog di database. Tidak bisa mengukur.")
        print("Tidak membuat data apa pun.")
        return

    print(f"analysis_log_id yang diukur: {analysis_id}")
    print()

    # Warm-up satu kali agar koneksi cold tidak mencemari median.
    service.get_review_detail(analysis_id)

    measure("BARU: get_review_detail (1 session)",
            lambda: service.get_review_detail(analysis_id))
    measure("LAMA (simulasi): detail + bundle + TP (3 checkout)",
            lambda: simulate_old_two_sessions(analysis_id))

    print()
    print("Selesai. Tidak ada perubahan database.")


if __name__ == "__main__":
    main()
