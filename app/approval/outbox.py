from datetime import datetime, timedelta, timezone

from sqlalchemy import select, or_, and_

from app.database.connection import SessionLocal
from app.database.models import (
    AIAnalysisLog,
    TelegramOutbox,
)


def ensure_pending_review_notifications(recipient_chat_id):
    """
    Membuat antrean notifikasi untuk semua review yang belum
    memiliki event notifikasi.

    Berguna untuk notifikasi yang terlewat saat bot mati.
    """

    if (
        isinstance(recipient_chat_id, bool)
        or not isinstance(recipient_chat_id, int)
        or recipient_chat_id <= 0
    ):
        raise ValueError("Telegram recipient ID tidak valid.")

    created = 0

    with SessionLocal() as db:
        logs = db.scalars(
            select(AIAnalysisLog).where(
                AIAnalysisLog.status == "MANUAL_REVIEW"
            )
        ).all()

        for log in logs:
            event_key = f"MANUAL_REVIEW:{log.id}"

            existing = db.scalar(
                select(TelegramOutbox).where(
                    TelegramOutbox.event_key == event_key
                )
            )

            if existing:
                continue

            db.add(
                TelegramOutbox(
                    event_key=event_key,
                    analysis_log_id=log.id,
                    recipient_chat_id=recipient_chat_id,
                    event_type="MANUAL_REVIEW",
                    status="PENDING",
                )
            )
            created += 1

        db.commit()

    return created


def list_pending_notification_ids():
    """
    Mengembalikan daftar (outbox_id, analysis_log_id) untuk notifikasi
    berstatus PENDING. READ-ONLY: tidak mengubah status apa pun.

    Dipakai saat startup untuk menampilkan antrean tertahan sebelum
    worker diaktifkan.
    """
    with SessionLocal() as db:
        rows = db.execute(
            select(
                TelegramOutbox.id,
                TelegramOutbox.analysis_log_id,
            )
            .where(TelegramOutbox.status == "PENDING")
            .order_by(
                TelegramOutbox.created_at.asc(),
                TelegramOutbox.id.asc(),
            )
        ).all()

        return [
            {"outbox_id": row[0], "analysis_log_id": row[1]}
            for row in rows
        ]


def claim_next_notification(stale_after_seconds=300):
    """
    Mengambil satu notifikasi PENDING.

    SENDING yang sudah terlalu lama juga dapat diambil kembali
    setelah proses bot sebelumnya mungkin berhenti mendadak.
    """

    now = datetime.now(timezone.utc)
    stale_before = now - timedelta(
        seconds=stale_after_seconds
    )

    eligible = or_(
        TelegramOutbox.status == "PENDING",
        and_(
            TelegramOutbox.status == "SENDING",
            TelegramOutbox.updated_at < stale_before,
        ),
    )

    with SessionLocal() as db:
        item = db.scalar(
            select(TelegramOutbox)
            .where(eligible)
            .order_by(
                TelegramOutbox.created_at.asc(),
                TelegramOutbox.id.asc(),
            )
            .limit(1)
            .with_for_update(skip_locked=True)
        )

        if item is None:
            return None

        item.status = "SENDING"
        item.attempts = (item.attempts or 0) + 1
        item.updated_at = now

        db.commit()

        return {
            "id": item.id,
            "analysis_log_id": item.analysis_log_id,
            "recipient_chat_id": item.recipient_chat_id,
            "event_type": item.event_type,
            "attempts": item.attempts,
        }


def mark_notification_sent(outbox_id, telegram_message_id):
    now = datetime.now(timezone.utc)

    with SessionLocal() as db:
        item = db.get(TelegramOutbox, outbox_id)

        if not item:
            raise LookupError("Outbox event tidak ditemukan.")

        if item.status != "SENDING":
            raise ValueError(
                f"Status outbox tidak valid: {item.status}"
            )

        item.status = "SENT"
        item.telegram_message_id = telegram_message_id
        item.sent_at = now
        item.updated_at = now
        item.last_error = None

        db.commit()


def mark_notification_failed(outbox_id, error_message):
    now = datetime.now(timezone.utc)

    with SessionLocal() as db:
        item = db.get(TelegramOutbox, outbox_id)

        if not item:
            raise LookupError("Outbox event tidak ditemukan.")

        item.status = "FAILED"
        item.last_error = str(error_message)[:1000]
        item.updated_at = now

        db.commit()


def mark_notification_skipped(outbox_id, reason):
    now = datetime.now(timezone.utc)

    with SessionLocal() as db:
        item = db.get(TelegramOutbox, outbox_id)

        if not item:
            raise LookupError("Outbox event tidak ditemukan.")

        item.status = "SKIPPED"
        item.last_error = str(reason)[:1000]
        item.updated_at = now

        db.commit()
