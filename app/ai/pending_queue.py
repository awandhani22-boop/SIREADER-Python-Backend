"""Antrean pesan yang belum dianalisis karena budget AI habis.

Tabel `ai_pending_messages` terpisah dari `telegram_notification_outbox`
karena lifecycle-nya berbeda: antrean ini tentang ANALISIS, bukan pengiriman.

Belum ada worker inference yang diaktifkan. Semua fungsi di sini hanya
menyediakan mekanisme aman untuk enqueue, claim, retry manual, dan recovery.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.database.connection import SessionLocal
from app.database.models import AIPendingMessage

STATUS_PENDING = "PENDING"
STATUS_CLAIMED = "CLAIMED"
STATUS_ANALYZED = "ANALYZED"
STATUS_DISCARDED = "DISCARDED"

# Status pipeline yang berarti pesan belum dianalisis karena budget.
BUDGET_BLOCK_STATUSES = ("BLOCKED_HARD_BUDGET", "MANUAL_REVIEW_BUDGET")

DEFAULT_CLAIM_TIMEOUT_MINUTES = 30


def _now():
    return datetime.now(timezone.utc)


def pending_key(chat_id, telegram_message_id):
    """Identitas unik pesan sumber; satu pesan hanya boleh antre sekali."""
    return f"{int(chat_id)}:{int(telegram_message_id)}"


def _as_dict(row):
    return {
        "id": row.id,
        "dedup_key": row.dedup_key,
        "chat_id": row.chat_id,
        "telegram_message_id": row.telegram_message_id,
        "analysis_log_id": row.analysis_log_id,
        "status": row.status,
        "reason": row.reason,
        "attempts": row.attempts,
    }


def enqueue_pending_message(
    db,
    chat_id,
    telegram_message_id,
    analysis_log_id=None,
    reason=None,
):
    """Idempoten di dalam session pemanggil. Caller memiliki commit/rollback.

    Duplikat tidak membuat baris baru dan tidak mengubah baris lama.
    """
    key = pending_key(chat_id, telegram_message_id)
    existing = db.scalar(
        select(AIPendingMessage).where(AIPendingMessage.dedup_key == key)
    )
    if existing:
        return {
            "created": False,
            "id": existing.id,
            "status": existing.status,
            "reason": existing.reason,
        }

    row = AIPendingMessage(
        dedup_key=key,
        chat_id=int(chat_id),
        telegram_message_id=int(telegram_message_id),
        analysis_log_id=analysis_log_id,
        status=STATUS_PENDING,
        reason=reason,
        attempts=0,
        created_at=_now(),
        updated_at=_now(),
    )

    try:
        # Savepoint: IntegrityError dari request paralel tidak merusak
        # transaksi pemanggil.
        with db.begin_nested():
            db.add(row)
    except IntegrityError:
        existing = db.scalar(
            select(AIPendingMessage).where(AIPendingMessage.dedup_key == key)
        )
        return {
            "created": False,
            "id": existing.id if existing else None,
            "status": existing.status if existing else None,
            "reason": existing.reason if existing else None,
        }

    return {
        "created": True,
        "id": row.id,
        "status": row.status,
        "reason": row.reason,
    }


def enqueue_message(chat_id, telegram_message_id, analysis_log_id=None, reason=None):
    """Versi mandiri: membuka session sendiri dan commit."""
    with SessionLocal() as db:
        result = enqueue_pending_message(
            db,
            chat_id=chat_id,
            telegram_message_id=telegram_message_id,
            analysis_log_id=analysis_log_id,
            reason=reason,
        )
        db.commit()
        return result


def claim_pending_batch(limit=10, owner="manual", lock_timeout_minutes=None):
    """Claim atomik baris PENDING. Baris yang sudah di-claim proses lain dilewati."""
    limit = max(1, int(limit))
    claimed = []
    with SessionLocal() as db:
        rows = (
            db.execute(
                select(AIPendingMessage)
                .where(AIPendingMessage.status == STATUS_PENDING)
                .order_by(AIPendingMessage.id)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
            .scalars()
            .all()
        )
        now = _now()
        for row in rows:
            row.status = STATUS_CLAIMED
            row.claimed_by = str(owner)[:100]
            row.claimed_at = now
            row.updated_at = now
            row.attempts = int(row.attempts or 0) + 1
            claimed.append(_as_dict(row))
        if claimed:
            db.commit()
        else:
            db.rollback()
    return claimed


def release_stale_claims(timeout_minutes=DEFAULT_CLAIM_TIMEOUT_MINUTES):
    """Recovery setelah restart: claim yang menggantung dikembalikan ke PENDING.

    Tidak memproses pesan, hanya melepas kunci agar aman di-claim ulang.
    """
    cutoff = _now() - timedelta(minutes=int(timeout_minutes))
    released = 0
    with SessionLocal() as db:
        rows = (
            db.execute(
                select(AIPendingMessage).where(
                    AIPendingMessage.status == STATUS_CLAIMED,
                    AIPendingMessage.claimed_at.is_not(None),
                    AIPendingMessage.claimed_at < cutoff,
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            row.status = STATUS_PENDING
            row.claimed_by = None
            row.claimed_at = None
            row.updated_at = _now()
            released += 1
        if released:
            db.commit()
        else:
            db.rollback()
    return released


def mark_pending_analyzed(pending_id, owner=None):
    """Tutup antrean setelah analisis berhasil. Tidak mengirim ulang apa pun."""
    with SessionLocal() as db:
        row = db.get(AIPendingMessage, int(pending_id))
        if not row:
            return False
        if owner is not None and row.claimed_by not in (None, str(owner)):
            return False
        row.status = STATUS_ANALYZED
        row.claimed_by = None
        row.claimed_at = None
        row.updated_at = _now()
        db.commit()
        return True


def mark_pending_retry(pending_id, error_message=None, delay_minutes=15):
    """Kembalikan ke PENDING untuk retry manual/terjadwal."""
    with SessionLocal() as db:
        row = db.get(AIPendingMessage, int(pending_id))
        if not row:
            return False
        if row.status == STATUS_ANALYZED:
            return False
        row.status = STATUS_PENDING
        row.claimed_by = None
        row.claimed_at = None
        row.next_attempt_at = _now() + timedelta(minutes=int(delay_minutes))
        row.last_error = str(error_message)[:1000] if error_message else row.last_error
        row.updated_at = _now()
        db.commit()
        return True


def discard_pending(pending_id, note=None):
    """Buang dari antrean tanpa analisis (keputusan manusia)."""
    with SessionLocal() as db:
        row = db.get(AIPendingMessage, int(pending_id))
        if not row:
            return False
        row.status = STATUS_DISCARDED
        row.last_error = str(note)[:1000] if note else row.last_error
        row.updated_at = _now()
        db.commit()
        return True


def count_pending():
    with SessionLocal() as db:
        return int(
            db.scalar(
                select(func.count(AIPendingMessage.id)).where(
                    AIPendingMessage.status == STATUS_PENDING
                )
            )
            or 0
        )
