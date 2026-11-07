
import json
import uuid
from datetime import datetime, timezone

from sqlalchemy import select, delete

from app.database.connection import SessionLocal
from app.database.models import TelegramMessage, AIAnalysisLog
from app.ai import pipeline


# ID unik agar tidak bertabrakan dengan pesan asli.
nonce = uuid.uuid4().int

chat_id = -(nonce % 8_000_000_000_000_000 + 1)
message_id = nonce % 1_000_000_000 + 1

calls = []

original_analyze_bundle = pipeline.analyze_bundle


def fake_analyze_bundle(bundle):
    """Simulasi AI tanpa memanggil API."""

    calls.append(bundle)

    return {
        "status": "ANALYZED",
        "final_analysis": {
            "status": "CLEAR",
            "confidence": 0.99,
            "summary": "Mock integration test. No trading decision.",
        },
        "attempts": [
            {
                "tier": "FAST",
                "model": "MOCK-HAIKU",
                "cost_rp": "0",
            }
        ],
    }


try:
    # -------------------------------------------------
    # 1. Buat pesan simulasi di Supabase
    # -------------------------------------------------

    now = datetime.now(timezone.utc)

    with SessionLocal() as db:
        db.add(
            TelegramMessage(
                chat_id=chat_id,
                telegram_message_id=message_id,
                sender="integration-test",
                message_text="Pantau sell",
                reply_to_id=None,
                is_edited=False,
                received_at=now,
                telegram_date=now,
            )
        )
        db.commit()

    print("PESAN SIMULASI: BERHASIL DISIMPAN")

    # -------------------------------------------------
    # 2. Ganti AI Router dengan mock
    # -------------------------------------------------

    pipeline.analyze_bundle = fake_analyze_bundle

    # -------------------------------------------------
    # 3. Jalankan Pipeline sungguhan
    # -------------------------------------------------

    first = pipeline.process_telegram_message(
        chat_id,
        message_id,
    )

    print("PIPELINE STATUS:", first["status"])
    print("AI MOCK CALL:", len(calls))

    assert first["status"] == "ANALYZED"
    assert len(calls) == 1

    # -------------------------------------------------
    # 4. Periksa hasil yang tersimpan di Supabase
    # -------------------------------------------------

    with SessionLocal() as db:
        log = db.scalar(
            select(AIAnalysisLog).where(
                AIAnalysisLog.chat_id == chat_id,
                AIAnalysisLog.target_message_id == message_id,
            )
        )

        assert log is not None
        assert log.status == "ANALYZED"
        assert log.result_json is not None

        saved_result = json.loads(log.result_json)

        print("DATABASE STATUS:", log.status)
        print("HASIL JSON TERSIMPAN:", bool(saved_result))

    # -------------------------------------------------
    # 5. Uji deduplikasi
    # -------------------------------------------------

    second = pipeline.process_telegram_message(
        chat_id,
        message_id,
    )

    print("DUPLIKASI STATUS:", second["status"])
    print("TOTAL AI MOCK CALL:", len(calls))

    assert second["status"] == "SKIPPED_DUPLICATE"
    assert len(calls) == 1

    print("\nSEMUA INTEGRATION CHECK BERHASIL")

finally:
    # Pulihkan fungsi AI Router.
    pipeline.analyze_bundle = original_analyze_bundle

    # Hapus seluruh data pengujian.
    with SessionLocal() as db:
        db.execute(
            delete(AIAnalysisLog).where(
                AIAnalysisLog.chat_id == chat_id,
                AIAnalysisLog.target_message_id == message_id,
            )
        )

        db.execute(
            delete(TelegramMessage).where(
                TelegramMessage.chat_id == chat_id,
                TelegramMessage.telegram_message_id == message_id,
            )
        )

        db.commit()

    print("DATA SIMULASI DIBERSIHKAN")
