"""Persistent repository for SIREADER spread-decision watches.

All functions receive an SQLAlchemy Session and never commit it.
The caller owns transaction boundaries.
This module does not connect to MT5 or send trading orders.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.database.models import (
    AIAnalysisLog,
    TradeDecisionWatch,
    TradeEpisode,
)
from app.execution.price_math import PIP_PRICE


# Statuses stored in trade_decision_watches.
WATCH_WAITING = "WAITING_FOR_SPREAD"
WATCH_RECHECKING = "RECHECKING"
WATCH_RESOLVED = "RESOLVED"
WATCH_SKIPPED = "SKIPPED"
WATCH_CANCELLED = "CANCELLED"
WATCH_EXPIRED = "EXPIRED"
WATCH_ERROR = "ERROR"

ACTIVE_STATUSES = {WATCH_WAITING, WATCH_RECHECKING}
TERMINAL_STATUSES = {
    WATCH_RESOLVED,
    WATCH_SKIPPED,
    WATCH_CANCELLED,
    WATCH_EXPIRED,
    WATCH_ERROR,
}
MAX_WATCH_LIMIT = 500


class TradeDecisionWatchRepositoryError(Exception):
    """Base error for decision-watch repository operations."""


class TradeDecisionWatchValidationError(
    TradeDecisionWatchRepositoryError
):
    """Raised when a watch input is invalid."""


class TradeDecisionWatchNotFoundError(
    TradeDecisionWatchRepositoryError
):
    """Raised when a requested database row does not exist."""


class TradeDecisionWatchConflictError(
    TradeDecisionWatchRepositoryError
):
    """Raised when a requested state transition is not allowed."""


def _positive_id(value, field_name):
    if isinstance(value, bool):
        raise TradeDecisionWatchValidationError(
            f"{field_name} harus berupa integer positif."
        )
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        raise TradeDecisionWatchValidationError(
            f"{field_name} harus berupa integer positif."
        )
    if parsed <= 0:
        raise TradeDecisionWatchValidationError(
            f"{field_name} harus lebih besar dari nol."
        )
    if isinstance(value, float) and value != parsed:
        raise TradeDecisionWatchValidationError(
            f"{field_name} tidak boleh berupa angka desimal."
        )
    return parsed


def _decimal(value, field_name, *, allow_zero=False):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise TradeDecisionWatchValidationError(
            f"{field_name} bukan angka yang valid."
        )
    if not result.is_finite():
        raise TradeDecisionWatchValidationError(
            f"{field_name} harus berupa angka finite."
        )
    if allow_zero:
        if result < 0:
            raise TradeDecisionWatchValidationError(
                f"{field_name} tidak boleh negatif."
            )
    elif result <= 0:
        raise TradeDecisionWatchValidationError(
            f"{field_name} harus lebih besar dari nol."
        )
    return result


def _normalize_symbol(value):
    if not isinstance(value, str):
        raise TradeDecisionWatchValidationError(
            "Symbol harus berupa teks."
        )
    value = value.strip()
    if not value or len(value) > 64:
        raise TradeDecisionWatchValidationError(
            "Symbol kosong atau terlalu panjang."
        )
    return value


def _normalize_server(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise TradeDecisionWatchValidationError(
            "Broker server harus berupa teks."
        )
    value = value.strip()
    if not value:
        return None
    if len(value) > 128:
        raise TradeDecisionWatchValidationError(
            "Nama broker server terlalu panjang."
        )
    return value


def _as_utc(value, field_name="timestamp"):
    if value is None:
        return datetime.now(timezone.utc)
    if not isinstance(value, datetime):
        raise TradeDecisionWatchValidationError(
            f"{field_name} harus berupa datetime."
        )
    if value.tzinfo is None:
        # Database timestamps in this project are stored as UTC.
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _validate_quote(bid, ask):
    bid_value = _decimal(bid, "bid")
    ask_value = _decimal(ask, "ask")
    if ask_value < bid_value:
        raise TradeDecisionWatchValidationError(
            "Ask tidak boleh lebih kecil daripada Bid."
        )
    pip_price = _decimal(PIP_PRICE, "PIP_PRICE")
    spread_pips = (ask_value - bid_value) / pip_price
    return bid_value, ask_value, spread_pips


def _get_watch_or_raise(db, watch_id):
    watch_id = _positive_id(watch_id, "watch_id")
    watch = db.get(TradeDecisionWatch, watch_id)
    if watch is None:
        raise TradeDecisionWatchNotFoundError(
            f"Watch {watch_id} tidak ditemukan."
        )
    return watch


def _ensure_active(watch):
    if watch.status not in ACTIVE_STATUSES:
        raise TradeDecisionWatchConflictError(
            f"Watch tidak aktif. Status sekarang: {watch.status}."
        )


def create_watch(
    db: Session,
    *,
    analysis_log_id,
    trade_episode_id=None,
    trade_episode_key=None,
    symbol,
    maximum_spread_pips,
    bid,
    ask,
    broker_server=None,
    now=None,
    next_check_at=None,
):
    """Create a watch for an OPEN episode whose current spread exceeds SL.

    Idempotent for the same analysis/episode/symbol/server/limit identity.
    Does not commit the supplied Session.
    """
    analysis_log_id = _positive_id(analysis_log_id, "analysis_log_id")
    if trade_episode_key is not None:
        if trade_episode_id is not None:
            raise TradeDecisionWatchValidationError(
                "Gunakan trade_episode_id atau trade_episode_key, bukan keduanya."
            )

        if (
            not isinstance(trade_episode_key, str)
            or not trade_episode_key.strip()
        ):
            raise TradeDecisionWatchValidationError(
                "trade_episode_key harus berupa string yang tidak kosong."
            )

        trade_episode_key = trade_episode_key.strip()

        episode_by_key = db.scalar(
            select(TradeEpisode).where(
                TradeEpisode.episode_key == trade_episode_key
            )
        )

        if episode_by_key is None:
            raise TradeDecisionWatchNotFoundError(
                "TradeEpisode dengan episode_key tersebut tidak ditemukan."
            )

        trade_episode_id = episode_by_key.id
    else:
        trade_episode_id = _positive_id(
            trade_episode_id,
            "trade_episode_id",
        )
    symbol = _normalize_symbol(symbol)
    broker_server = _normalize_server(broker_server)
    maximum_spread = _decimal(
        maximum_spread_pips, "maximum_spread_pips"
    )
    bid_value, ask_value, spread_pips = _validate_quote(bid, ask)

    if spread_pips <= maximum_spread:
        raise TradeDecisionWatchValidationError(
            "Watch hanya boleh dibuat ketika spread melebihi batas."
        )

    now = _as_utc(now, "now")
    if next_check_at is None:
        next_check_at = now + timedelta(seconds=1)
    else:
        next_check_at = _as_utc(next_check_at, "next_check_at")

    log = db.get(AIAnalysisLog, analysis_log_id)
    if log is None:
        raise TradeDecisionWatchNotFoundError(
            "AIAnalysisLog tidak ditemukan."
        )

    episode = db.get(TradeEpisode, trade_episode_id)
    if episode is None:
        raise TradeDecisionWatchNotFoundError(
            "TradeEpisode tidak ditemukan."
        )

    if log.chat_id != episode.chat_id:
        raise TradeDecisionWatchConflictError(
            "Analysis Log dan Trade Episode berasal dari chat berbeda."
        )

    if episode.status != "OPEN":
        raise TradeDecisionWatchConflictError(
            f"Episode tidak boleh dipantau. Status: {episode.status}."
        )

    existing = db.scalar(
        select(TradeDecisionWatch).where(
            TradeDecisionWatch.analysis_log_id == analysis_log_id
        )
    )
    if existing is not None:
        same_identity = (
            existing.trade_episode_id == trade_episode_id
            and existing.symbol == symbol
            and existing.broker_server == broker_server
            and Decimal(str(existing.maximum_spread_pips))
            == maximum_spread
        )
        if not same_identity:
            raise TradeDecisionWatchConflictError(
                "Analysis Log sudah memiliki watch dengan identitas berbeda."
            )
        if existing.status in ACTIVE_STATUSES:
            return existing
        raise TradeDecisionWatchConflictError(
            "Watch sebelumnya sudah terminal dan tidak boleh diaktifkan ulang."
        )

    watch = TradeDecisionWatch(
        analysis_log_id=analysis_log_id,
        trade_episode_id=trade_episode_id,
        symbol=symbol,
        broker_server=broker_server,
        status=WATCH_WAITING,
        spread_pips=spread_pips,
        maximum_spread_pips=maximum_spread,
        last_bid=bid_value,
        last_ask=ask_value,
        last_checked_at=now,
        next_check_at=next_check_at,
        check_count=0,
        last_reason="SPREAD_EXCEEDS_STOP_LOSS",
        created_at=now,
        updated_at=now,
    )
    db.add(watch)
    db.flush()
    return watch


def get_watch_by_id(db, watch_id):
    return db.get(
        TradeDecisionWatch,
        _positive_id(watch_id, "watch_id"),
    )


def get_watch_by_analysis_log(db, analysis_log_id):
    analysis_log_id = _positive_id(
        analysis_log_id, "analysis_log_id"
    )
    return db.scalar(
        select(TradeDecisionWatch).where(
            TradeDecisionWatch.analysis_log_id == analysis_log_id
        )
    )


def claim_due_watch(
    db,
    watch_id,
    *,
    now=None,
    lease_seconds=60,
):
    """Atomically claim one due active watch using next_check_at as a lease.

    Returns (watch, lease_until), or None when another worker already claimed
    it / it is not due. The caller owns commit/rollback.
    """
    watch_id = _positive_id(watch_id, "watch_id")
    now = _as_utc(now, "now")

    if (
        isinstance(lease_seconds, bool)
        or not isinstance(lease_seconds, int)
        or lease_seconds < 1
        or lease_seconds > 3600
    ):
        raise TradeDecisionWatchValidationError(
            "lease_seconds harus integer antara 1 dan 3600."
        )

    lease_until = now + timedelta(seconds=lease_seconds)
    result = db.execute(
        update(TradeDecisionWatch)
        .where(
            TradeDecisionWatch.id == watch_id,
            TradeDecisionWatch.status.in_(ACTIVE_STATUSES),
            TradeDecisionWatch.next_check_at <= now,
        )
        .values(
            next_check_at=lease_until,
            updated_at=now,
        )
    )

    if result.rowcount != 1:
        return None

    watch = _get_watch_or_raise(db, watch_id)
    db.flush()
    return watch, lease_until


def reschedule_claimed_watch(
    db,
    watch_id,
    *,
    lease_until,
    next_check_at,
    reason,
    now=None,
):
    """Release a claimed watch for retry; fails if the lease was lost."""
    watch_id = _positive_id(watch_id, "watch_id")
    lease_until = _as_utc(lease_until, "lease_until")
    next_check_at = _as_utc(next_check_at, "next_check_at")
    now = _as_utc(now, "now")

    if not isinstance(reason, str) or not reason.strip():
        raise TradeDecisionWatchValidationError("Reason wajib diisi.")

    result = db.execute(
        update(TradeDecisionWatch)
        .where(
            TradeDecisionWatch.id == watch_id,
            TradeDecisionWatch.status.in_(ACTIVE_STATUSES),
            TradeDecisionWatch.next_check_at == lease_until,
        )
        .values(
            next_check_at=next_check_at,
            last_reason=reason.strip()[:1000],
            updated_at=now,
        )
    )

    if result.rowcount != 1:
        raise TradeDecisionWatchConflictError(
            "Lease watch sudah hilang atau sudah diambil worker lain."
        )

    db.flush()
    return _get_watch_or_raise(db, watch_id)


def update_claimed_watch_check(
    db,
    watch_id,
    *,
    lease_until,
    bid,
    ask,
    checked_at=None,
    retry_seconds=1,
):
    """Update quote for a claimed watch while enforcing its lease."""
    watch_id = _positive_id(watch_id, "watch_id")
    lease_until = _as_utc(lease_until, "lease_until")
    checked_at = _as_utc(checked_at, "checked_at")

    if (
        isinstance(retry_seconds, bool)
        or not isinstance(retry_seconds, int)
        or retry_seconds < 0
        or retry_seconds > 3600
    ):
        raise TradeDecisionWatchValidationError(
            "retry_seconds harus integer antara 0 dan 3600."
        )

    bid_value, ask_value, spread_pips = _validate_quote(bid, ask)
    watch = _get_watch_or_raise(db, watch_id)
    maximum = Decimal(str(watch.maximum_spread_pips))

    if spread_pips > maximum:
        status = WATCH_WAITING
        reason = "SPREAD_EXCEEDS_STOP_LOSS"
    else:
        status = WATCH_RECHECKING
        reason = "SPREAD_NORMAL_REEVALUATION_REQUIRED"

    result = db.execute(
        update(TradeDecisionWatch)
        .where(
            TradeDecisionWatch.id == watch_id,
            TradeDecisionWatch.status.in_(ACTIVE_STATUSES),
            TradeDecisionWatch.next_check_at == lease_until,
        )
        .values(
            status=status,
            spread_pips=spread_pips,
            last_bid=bid_value,
            last_ask=ask_value,
            last_checked_at=checked_at,
            next_check_at=checked_at + timedelta(seconds=retry_seconds),
            check_count=TradeDecisionWatch.check_count + 1,
            last_reason=reason,
            updated_at=checked_at,
        )
    )

    if result.rowcount != 1:
        raise TradeDecisionWatchConflictError(
            "Lease watch sudah hilang atau sudah diambil worker lain."
        )

    db.flush()
    return _get_watch_or_raise(db, watch_id)


def finish_claimed_watch(
    db,
    watch_id,
    *,
    lease_until,
    target_status,
    reason,
    now=None,
):
    """Terminalize a claimed watch only while the caller still owns its lease."""
    watch_id = _positive_id(watch_id, "watch_id")
    lease_until = _as_utc(lease_until, "lease_until")
    now = _as_utc(now, "now")

    if target_status not in TERMINAL_STATUSES:
        raise TradeDecisionWatchValidationError(
            "Target status bukan status terminal."
        )
    if not isinstance(reason, str) or not reason.strip():
        raise TradeDecisionWatchValidationError("Reason wajib diisi.")

    result = db.execute(
        update(TradeDecisionWatch)
        .where(
            TradeDecisionWatch.id == watch_id,
            TradeDecisionWatch.status.in_(ACTIVE_STATUSES),
            TradeDecisionWatch.next_check_at == lease_until,
        )
        .values(
            status=target_status,
            last_reason=reason.strip()[:1000],
            resolved_at=now,
            updated_at=now,
            next_check_at=now,
        )
    )

    if result.rowcount != 1:
        raise TradeDecisionWatchConflictError(
            "Lease watch sudah hilang atau sudah diambil worker lain."
        )

    db.flush()
    return _get_watch_or_raise(db, watch_id)


def get_due_watches(db, *, now=None, limit=100):
    """Read due active watches; this is not an atomic worker claim."""
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise TradeDecisionWatchValidationError(
            "limit harus berupa integer."
        )
    if limit < 1 or limit > MAX_WATCH_LIMIT:
        raise TradeDecisionWatchValidationError(
            f"limit harus antara 1 dan {MAX_WATCH_LIMIT}."
        )
    now = _as_utc(now, "now")
    return db.scalars(
        select(TradeDecisionWatch)
        .where(
            TradeDecisionWatch.status.in_(ACTIVE_STATUSES),
            TradeDecisionWatch.next_check_at <= now,
        )
        .order_by(
            TradeDecisionWatch.next_check_at.asc(),
            TradeDecisionWatch.id.asc(),
        )
        .limit(limit)
    ).all()


def update_watch_check(
    db,
    watch_id,
    *,
    bid,
    ask,
    checked_at=None,
    next_check_at=None,
):
    """Persist latest quote and derive WAITING/RECHECKING from spread.

    A normal spread moves the watch to RECHECKING, not RESOLVED. The caller
    must re-evaluate the full trading decision before resolving it.
    """
    watch = _get_watch_or_raise(db, watch_id)
    _ensure_active(watch)
    bid_value, ask_value, spread_pips = _validate_quote(bid, ask)
    checked_at = _as_utc(checked_at, "checked_at")

    if spread_pips > Decimal(str(watch.maximum_spread_pips)):
        new_status = WATCH_WAITING
        reason = "SPREAD_EXCEEDS_STOP_LOSS"
        default_next_check = checked_at + timedelta(seconds=1)
    else:
        new_status = WATCH_RECHECKING
        reason = "SPREAD_NORMAL_REEVALUATION_REQUIRED"
        default_next_check = checked_at

    if next_check_at is None:
        next_check_at = default_next_check
    else:
        next_check_at = _as_utc(next_check_at, "next_check_at")

    watch.status = new_status
    watch.spread_pips = spread_pips
    watch.last_bid = bid_value
    watch.last_ask = ask_value
    watch.last_checked_at = checked_at
    watch.next_check_at = next_check_at
    watch.check_count += 1
    watch.last_reason = reason
    watch.updated_at = checked_at
    db.flush()
    return watch


def _set_terminal_status(
    db,
    watch_id,
    *,
    target_status,
    reason,
    now=None,
):
    watch = _get_watch_or_raise(db, watch_id)
    if target_status not in TERMINAL_STATUSES:
        raise TradeDecisionWatchValidationError(
            "Target status bukan status terminal."
        )
    if not isinstance(reason, str) or not reason.strip():
        raise TradeDecisionWatchValidationError("Reason wajib diisi.")

    now = _as_utc(now, "now")
    reason = reason.strip()[:1000]
    if watch.status == target_status:
        return watch
    if watch.status not in ACTIVE_STATUSES:
        raise TradeDecisionWatchConflictError(
            f"Watch sudah terminal: {watch.status}."
        )

    watch.status = target_status
    watch.last_reason = reason
    watch.resolved_at = now
    watch.updated_at = now
    watch.next_check_at = now
    db.flush()
    return watch


def resolve_watch(
    db, watch_id, *, reason="DECISION_REEVALUATED", now=None
):
    return _set_terminal_status(
        db,
        watch_id,
        target_status=WATCH_RESOLVED,
        reason=reason,
        now=now,
    )


def skip_watch(db, watch_id, *, reason, now=None):
    return _set_terminal_status(
        db,
        watch_id,
        target_status=WATCH_SKIPPED,
        reason=reason,
        now=now,
    )


def cancel_watch(db, watch_id, *, reason, now=None):
    return _set_terminal_status(
        db,
        watch_id,
        target_status=WATCH_CANCELLED,
        reason=reason,
        now=now,
    )


def expire_watch(db, watch_id, *, reason="WATCH_EXPIRED", now=None):
    return _set_terminal_status(
        db,
        watch_id,
        target_status=WATCH_EXPIRED,
        reason=reason,
        now=now,
    )


def mark_watch_error(db, watch_id, *, reason, now=None):
    return _set_terminal_status(
        db,
        watch_id,
        target_status=WATCH_ERROR,
        reason=reason,
        now=now,
    )
