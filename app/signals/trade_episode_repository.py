from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database.models import (
    TelegramMessage,
    TradeEpisode,
    TradeEpisodeMessage,
    TradePendingEntry,
)


# ============================================================
# CONSTANTS
# ============================================================

RELATION_ANCHOR = "ANCHOR"
ROLE_NEW_SIGNAL = "NEW_SIGNAL"
ASSOCIATION_ANCHOR = "ANCHOR"
ASSOCIATION_PERSISTENT_PENDING = "PERSISTENT_PENDING"

PENDING_ENTRY_TTL_MINUTES = 30
PENDING_WAITING = "WAITING_FOR_ZONE"
PENDING_MATCHED = "MATCHED"
PENDING_CANCELLED = "CANCELLED"
PENDING_EXPIRED = "EXPIRED"
PENDING_AMBIGUOUS = "AMBIGUOUS"


# ============================================================
# REPOSITORY ERRORS
# ============================================================

class TradeEpisodeRepositoryError(Exception):
    """Kesalahan umum repository episode."""


class TradeEpisodeValidationError(TradeEpisodeRepositoryError):
    """Input episode atau relasi tidak valid."""


class TradeEpisodeNotFoundError(TradeEpisodeRepositoryError):
    """Episode atau pesan Telegram tidak ditemukan."""


class TradeEpisodeConflictError(TradeEpisodeRepositoryError):
    """Identitas atau kepemilikan episode bertentangan."""


# ============================================================
# GENERAL HELPERS
# ============================================================

def _normalize_text(value, field_name, max_length=40):
    if not isinstance(value, str):
        raise TradeEpisodeValidationError(
            f"{field_name} harus berupa teks."
        )

    value = value.strip().upper()

    if not value:
        raise TradeEpisodeValidationError(
            f"{field_name} tidak boleh kosong."
        )

    if len(value) > max_length:
        raise TradeEpisodeValidationError(
            f"{field_name} terlalu panjang."
        )

    return value


def _to_price(value, field_name):
    if value is None:
        raise TradeEpisodeValidationError(
            f"{field_name} wajib diisi."
        )

    try:
        price = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise TradeEpisodeValidationError(
            f"{field_name} bukan harga yang valid."
        )

    if not price.is_finite():
        raise TradeEpisodeValidationError(
            f"{field_name} harus berupa angka finite."
        )

    price = price.quantize(Decimal("0.00001"))

    if price <= 0:
        raise TradeEpisodeValidationError(
            f"{field_name} harus lebih besar dari nol."
        )

    return price


def _prepare_zone(entry_low, entry_high):
    low = _to_price(entry_low, "entry_low")
    high = _to_price(entry_high, "entry_high")

    if low >= high:
        raise TradeEpisodeValidationError(
            "entry_low harus lebih kecil daripada entry_high."
        )

    midpoint = (
        (low + high) / Decimal("2")
    ).quantize(Decimal("0.00001"))

    return low, high, midpoint


def _build_episode_key(chat_id, telegram_message_id):
    return f"trade_{chat_id}_{telegram_message_id}"


def _get_telegram_message(db, chat_id, telegram_message_id):
    return db.scalar(
        select(TelegramMessage).where(
            TelegramMessage.chat_id == chat_id,
            TelegramMessage.telegram_message_id == telegram_message_id,
        )
    )


def _get_episode_by_anchor(db, anchor_message_id):
    return db.scalar(
        select(TradeEpisode).where(
            TradeEpisode.anchor_message_id == anchor_message_id
        )
    )


def _get_message_mapping(db, telegram_message_db_id):
    return db.scalar(
        select(TradeEpisodeMessage).where(
            TradeEpisodeMessage.telegram_message_db_id
            == telegram_message_db_id
        )
    )


# ============================================================
# READ HELPERS
# ============================================================

def get_trade_episode_by_key(db, episode_key):
    return db.scalar(
        select(TradeEpisode).where(
            TradeEpisode.episode_key == episode_key
        )
    )


def get_trade_episode_for_message(db, telegram_message_db_id):
    """Ambil episode persisten yang memiliki pesan Telegram internal."""
    return db.scalar(
        select(TradeEpisode)
        .join(
            TradeEpisodeMessage,
            TradeEpisodeMessage.episode_id == TradeEpisode.id,
        )
        .where(
            TradeEpisodeMessage.telegram_message_db_id
            == telegram_message_db_id
        )
    )


def list_trade_episode_messages(db, episode_id):
    """Ambil mapping pesan episode dalam urutan pencatatan."""
    return db.scalars(
        select(TradeEpisodeMessage)
        .where(TradeEpisodeMessage.episode_id == episode_id)
        .order_by(
            TradeEpisodeMessage.created_at.asc(),
            TradeEpisodeMessage.id.asc(),
        )
    ).all()


# ============================================================
# EPISODE IDENTITY VALIDATION
# ============================================================

def _validate_existing_episode(
    episode,
    anchor_message,
    chat_id,
    direction,
    entry_low,
    entry_high,
    entry_midpoint,
):
    """Memastikan episode key masih menunjuk setup yang sama."""
    identity_matches = (
        episode.chat_id == chat_id
        and episode.anchor_message_id == anchor_message.id
        and episode.direction == direction
        and episode.entry_low == entry_low
        and episode.entry_high == entry_high
        and episode.entry_midpoint == entry_midpoint
    )

    if not identity_matches:
        raise TradeEpisodeConflictError(
            "Episode key sudah digunakan oleh setup yang berbeda. "
            "Periksa perubahan pesan sumber."
        )


# ============================================================
# INTERNAL MESSAGE LINKING
# ============================================================

def _link_message_in_current_transaction(
    db,
    episode,
    telegram_message,
    message_role,
    relation_type,
    association_method,
):
    """Membuat atau memperbarui mapping; tidak melakukan commit."""
    existing = _get_message_mapping(db, telegram_message.id)

    if existing:
        if existing.episode_id != episode.id:
            raise TradeEpisodeConflictError(
                "Pesan Telegram sudah terhubung ke episode lain."
            )

        existing.message_role = message_role
        existing.relation_type = relation_type
        existing.association_method = association_method
        return existing

    mapping = TradeEpisodeMessage(
        episode_id=episode.id,
        telegram_message_db_id=telegram_message.id,
        message_role=message_role,
        relation_type=relation_type,
        association_method=association_method,
    )

    db.add(mapping)
    db.flush()
    return mapping


# ============================================================
# CREATE OR GET TRADE EPISODE
# ============================================================

def create_or_get_trade_episode(
    db: Session,
    *,
    chat_id,
    anchor_telegram_message_id,
    direction,
    entry_low,
    entry_high,
):
    """Membuat episode permanen atau mengambil episode yang identik.

    ID stabil: trade_{chat_id}_{anchor_telegram_message_id}
    Anchor otomatis dipetakan sebagai NEW_SIGNAL / ANCHOR.
    Fungsi tidak melakukan commit.
    """
    if chat_id is None or anchor_telegram_message_id is None:
        raise TradeEpisodeValidationError(
            "chat_id dan anchor_telegram_message_id wajib diisi."
        )

    try:
        chat_id = int(chat_id)
        anchor_telegram_message_id = int(anchor_telegram_message_id)
    except (ValueError, TypeError):
        raise TradeEpisodeValidationError(
            "Identitas Telegram harus berupa integer."
        )

    direction = _normalize_text(direction, "direction", max_length=10)
    if direction not in ("BUY", "SELL"):
        raise TradeEpisodeValidationError(
            "direction hanya boleh BUY atau SELL."
        )

    entry_low, entry_high, entry_midpoint = _prepare_zone(
        entry_low,
        entry_high,
    )

    anchor_message = _get_telegram_message(
        db,
        chat_id,
        anchor_telegram_message_id,
    )

    if not anchor_message:
        raise TradeEpisodeNotFoundError(
            "Pesan anchor tidak ditemukan pada chat tersebut."
        )

    episode_key = _build_episode_key(
        chat_id,
        anchor_telegram_message_id,
    )

    # 1. Episode sudah ada.
    existing = get_trade_episode_by_key(db, episode_key)
    if existing:
        _validate_existing_episode(
            existing,
            anchor_message,
            chat_id,
            direction,
            entry_low,
            entry_high,
            entry_midpoint,
        )

        link_trade_episode_message(
            db,
            episode_id=existing.id,
            telegram_message_db_id=anchor_message.id,
            message_role=ROLE_NEW_SIGNAL,
            relation_type=RELATION_ANCHOR,
            association_method=ASSOCIATION_ANCHOR,
        )

        return existing

    # 2. Anchor sudah dipakai episode lain.
    existing_anchor = _get_episode_by_anchor(db, anchor_message.id)
    if existing_anchor:
        raise TradeEpisodeConflictError(
            "Pesan anchor sudah digunakan oleh episode lain."
        )

    # 3. Buat episode dan mapping anchor dalam savepoint.
    episode = TradeEpisode(
        episode_key=episode_key,
        chat_id=chat_id,
        anchor_message_id=anchor_message.id,
        direction=direction,
        entry_low=entry_low,
        entry_high=entry_high,
        entry_midpoint=entry_midpoint,
        status="OPEN",
    )

    try:
        with db.begin_nested():
            db.add(episode)
            db.flush()

            _link_message_in_current_transaction(
                db,
                episode,
                anchor_message,
                ROLE_NEW_SIGNAL,
                RELATION_ANCHOR,
                ASSOCIATION_ANCHOR,
            )
            db.flush()

    except IntegrityError as exc:
        existing = get_trade_episode_by_key(db, episode_key)
        if existing:
            _validate_existing_episode(
                existing,
                anchor_message,
                chat_id,
                direction,
                entry_low,
                entry_high,
                entry_midpoint,
            )

            link_trade_episode_message(
                db,
                episode_id=existing.id,
                telegram_message_db_id=anchor_message.id,
                message_role=ROLE_NEW_SIGNAL,
                relation_type=RELATION_ANCHOR,
                association_method=ASSOCIATION_ANCHOR,
            )
            return existing

        existing_anchor = _get_episode_by_anchor(db, anchor_message.id)
        if existing_anchor:
            raise TradeEpisodeConflictError(
                "Pesan anchor sudah dimiliki episode lain."
            ) from exc

        existing_mapping = _get_message_mapping(db, anchor_message.id)
        if existing_mapping:
            raise TradeEpisodeConflictError(
                "Pesan anchor sudah terhubung ke episode lain."
            ) from exc

        raise

    return episode


# ============================================================
# LINK TELEGRAM MESSAGE TO EPISODE
# ============================================================

def link_trade_episode_message(
    db: Session,
    *,
    episode_id,
    telegram_message_db_id,
    message_role,
    relation_type,
    association_method,
):
    """Menghubungkan satu pesan Telegram ke maksimal satu episode."""
    try:
        episode_id = int(episode_id)
        telegram_message_db_id = int(telegram_message_db_id)
    except (ValueError, TypeError):
        raise TradeEpisodeValidationError(
            "ID episode dan pesan harus berupa integer."
        )

    message_role = _normalize_text(message_role, "message_role")
    relation_type = _normalize_text(relation_type, "relation_type")
    association_method = _normalize_text(
        association_method,
        "association_method",
    )

    episode = db.get(TradeEpisode, episode_id)
    if not episode:
        raise TradeEpisodeNotFoundError(
            "Trade episode tidak ditemukan."
        )

    telegram_message = db.get(TelegramMessage, telegram_message_db_id)
    if not telegram_message:
        raise TradeEpisodeNotFoundError(
            "Pesan Telegram tidak ditemukan."
        )

    if telegram_message.chat_id != episode.chat_id:
        raise TradeEpisodeConflictError(
            "Pesan dan episode berasal dari chat berbeda."
        )

    is_anchor = telegram_message.id == episode.anchor_message_id

    if is_anchor and relation_type != RELATION_ANCHOR:
        raise TradeEpisodeConflictError(
            "Pesan anchor wajib memiliki relation_type ANCHOR."
        )

    if not is_anchor and relation_type == RELATION_ANCHOR:
        raise TradeEpisodeConflictError(
            "Pesan non-anchor tidak boleh menjadi ANCHOR."
        )

    try:
        with db.begin_nested():
            mapping = _link_message_in_current_transaction(
                db,
                episode,
                telegram_message,
                message_role,
                relation_type,
                association_method,
            )
            db.flush()

        return mapping

    except IntegrityError as exc:
        existing = _get_message_mapping(db, telegram_message.id)
        if existing:
            if existing.episode_id != episode.id:
                raise TradeEpisodeConflictError(
                    "Pesan Telegram sudah dimiliki episode lain."
                ) from exc
            return existing
        raise


# ============================================================
# PERSISTENT PENDING ENTRY
# ============================================================

def _as_utc(value):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _telegram_message_time(message):
    value = message.telegram_date or message.received_at
    normalized = _as_utc(value)
    if normalized is None:
        raise TradeEpisodeValidationError(
            "Pesan Telegram tidak memiliki timestamp yang valid."
        )
    return normalized


def _get_pending_by_source_message(db, source_message_id):
    return db.scalar(
        select(TradePendingEntry).where(
            TradePendingEntry.source_message_id == source_message_id
        )
    )


def get_pending_entry_by_source_message(db, source_message_id):
    """Public read helper untuk mengambil status pending dari pesan sumber."""
    return _get_pending_by_source_message(db, source_message_id)


def _pending_candidates_for_timestamp(
    db,
    *,
    chat_id,
    zone_time,
    direction=None,
    ttl_minutes=PENDING_ENTRY_TTL_MINUTES,
):
    """Ambil pending request yang mendahului zona dan belum kedaluwarsa."""
    zone_time = _as_utc(zone_time)
    if zone_time is None:
        return []

    statement = select(TradePendingEntry).where(
        TradePendingEntry.chat_id == int(chat_id),
        TradePendingEntry.status == PENDING_WAITING,
    )
    if direction is not None:
        direction = _normalize_text(direction, "direction", 10)
        if direction not in ("BUY", "SELL"):
            return []
        statement = statement.where(
            TradePendingEntry.direction == direction
        )

    rows = db.scalars(statement.order_by(TradePendingEntry.id.asc())).all()
    ttl = timedelta(minutes=int(ttl_minutes))
    eligible = []

    for pending in rows:
        requested_at = _as_utc(pending.requested_at)
        if requested_at is None or requested_at >= zone_time:
            continue

        age = zone_time - requested_at
        if age <= ttl:
            eligible.append(pending)

    return eligible


def find_pending_entry_candidates(
    db,
    *,
    chat_id,
    zone_message,
    direction=None,
    ttl_minutes=PENDING_ENTRY_TTL_MINUTES,
):
    """Read-only lookup untuk kandidat pending entry yang dapat dipasangkan."""
    if zone_message is None:
        return []

    return _pending_candidates_for_timestamp(
        db,
        chat_id=chat_id,
        zone_time=_telegram_message_time(zone_message),
        direction=direction,
        ttl_minutes=ttl_minutes,
    )


def register_pending_entry_requests(
    db,
    bundle,
    *,
    ttl_minutes=PENDING_ENTRY_TTL_MINUTES,
    message_rows=None,
):
    """Simpan instruksi ENTRY_REQUEST sebagai pending entry secara idempoten."""
    if not isinstance(bundle, dict):
        return 0

    chat_id = bundle.get("chat_id")
    messages = bundle.get("messages") or []
    if chat_id is None or not isinstance(messages, list):
        return 0

    chat_id = int(chat_id)
    if message_rows is None:
        telegram_ids = []
        for item in messages:
            if not isinstance(item, dict):
                continue
            if item.get("telegram_message_id") is not None:
                try:
                    telegram_ids.append(int(item["telegram_message_id"]))
                except (TypeError, ValueError):
                    pass
        message_rows = {
            row.telegram_message_id: row
            for row in db.scalars(
                select(TelegramMessage).where(
                    TelegramMessage.chat_id == chat_id,
                    TelegramMessage.telegram_message_id.in_(set(telegram_ids)),
                )
            ).all()
        } if telegram_ids else {}

    created = 0
    ttl = timedelta(minutes=int(ttl_minutes))

    for item in messages:
        if not isinstance(item, dict):
            continue
        roles = set(item.get("message_roles") or [])
        if "ENTRY_REQUEST" not in roles or "NEW_SIGNAL" in roles or "SKIP" in roles:
            continue

        direction = item.get("pending_direction")
        if direction not in ("BUY", "SELL"):
            continue

        try:
            telegram_id = int(item.get("telegram_message_id"))
        except (TypeError, ValueError):
            continue

        source = message_rows.get(telegram_id)
        if source is None:
            continue

        existing = _get_pending_by_source_message(db, source.id)
        if existing is not None:
            # Status yang sudah MATCHED/CANCELLED/EXPIRED tidak boleh direset.
            continue

        requested_at = _telegram_message_time(source)
        pending = TradePendingEntry(
            chat_id=chat_id,
            source_message_id=source.id,
            direction=direction,
            status=PENDING_WAITING,
            requested_at=requested_at,
            expires_at=requested_at + ttl,
        )
        db.add(pending)
        db.flush()
        created += 1

    return created


def cancel_pending_entry_from_reply(
    db,
    *,
    chat_id,
    reply_to_telegram_message_id,
):
    """Batalkan pending hanya jika SKIP membalas pesan request yang tepat."""
    if reply_to_telegram_message_id is None:
        return False

    try:
        parent = _get_telegram_message(
            db,
            int(chat_id),
            int(reply_to_telegram_message_id),
        )
    except (TypeError, ValueError):
        return False

    if parent is None:
        return False

    pending = _get_pending_by_source_message(db, parent.id)
    if pending is None or pending.status != PENDING_WAITING:
        return False

    pending.status = PENDING_CANCELLED
    pending.resolved_at = datetime.now(timezone.utc)
    return True


def mark_pending_entries_ambiguous_for_zone(
    db,
    *,
    chat_id,
    zone_message,
    direction=None,
    ttl_minutes=PENDING_ENTRY_TTL_MINUTES,
):
    """Tandai seluruh kandidat ambigu agar tidak dipasangkan pada zona berikutnya."""
    expire_old_pending_entries(
        db,
        chat_id=chat_id,
        reference_time=_telegram_message_time(zone_message),
        ttl_minutes=ttl_minutes,
    )
    candidates = find_pending_entry_candidates(
        db,
        chat_id=chat_id,
        zone_message=zone_message,
        direction=direction,
        ttl_minutes=ttl_minutes,
    )
    if len(candidates) <= 1:
        return 0

    now = datetime.now(timezone.utc)
    for pending in candidates:
        pending.status = PENDING_AMBIGUOUS
        pending.resolved_at = now
    return len(candidates)


def match_pending_entry_to_episode(
    db,
    *,
    zone_message,
    episode,
    ttl_minutes=PENDING_ENTRY_TTL_MINUTES,
):
    """Cocokkan satu pending request yang unik ke episode zona."""
    expire_old_pending_entries(
        db,
        chat_id=episode.chat_id,
        reference_time=_telegram_message_time(zone_message),
        ttl_minutes=ttl_minutes,
    )
    candidates = find_pending_entry_candidates(
        db,
        chat_id=episode.chat_id,
        zone_message=zone_message,
        direction=episode.direction,
        ttl_minutes=ttl_minutes,
    )

    if len(candidates) > 1:
        now = datetime.now(timezone.utc)
        for pending in candidates:
            pending.status = PENDING_AMBIGUOUS
            pending.resolved_at = now
        return {"status": PENDING_AMBIGUOUS, "matched": 0}

    if not candidates:
        return {"status": "NO_MATCH", "matched": 0}

    pending = candidates[0]
    source = db.get(TelegramMessage, pending.source_message_id)
    if source is None:
        pending.status = PENDING_EXPIRED
        pending.resolved_at = datetime.now(timezone.utc)
        return {"status": PENDING_EXPIRED, "matched": 0}

    # Lindungi terhadap pesan request yang sebenarnya setelah zona.
    if _telegram_message_time(source) >= _telegram_message_time(zone_message):
        return {"status": "NO_MATCH", "matched": 0}

    link_trade_episode_message(
        db,
        episode_id=episode.id,
        telegram_message_db_id=source.id,
        message_role="ENTRY_REQUEST",
        relation_type="ENTRY_REQUEST",
        association_method=ASSOCIATION_PERSISTENT_PENDING,
    )

    pending.status = PENDING_MATCHED
    pending.matched_episode_id = episode.id
    pending.resolved_at = datetime.now(timezone.utc)

    return {"status": PENDING_MATCHED, "matched": 1}


def expire_old_pending_entries(
    db,
    *,
    chat_id,
    reference_time,
    ttl_minutes=PENDING_ENTRY_TTL_MINUTES,
):
    """Tandai pending entry yang sudah lewat TTL relatif terhadap waktu pesan baru."""
    reference_time = _as_utc(reference_time)
    if reference_time is None:
        return 0

    rows = db.scalars(
        select(TradePendingEntry).where(
            TradePendingEntry.chat_id == int(chat_id),
            TradePendingEntry.status == PENDING_WAITING,
        )
    ).all()

    ttl = timedelta(minutes=int(ttl_minutes))
    count = 0
    now = datetime.now(timezone.utc)
    for pending in rows:
        requested_at = _as_utc(pending.requested_at)
        if requested_at is None:
            continue
        if reference_time >= requested_at and reference_time - requested_at > ttl:
            pending.status = PENDING_EXPIRED
            pending.resolved_at = now
            count += 1
    return count


# ============================================================
# BUNDLE PERSISTENCE
# ============================================================

def _get_bundle_relation(roles):
    priorities = (
        ("SKIP", "SKIP_REQUEST"),
        ("ENTRY_REQUEST", "ENTRY_REQUEST"),
        ("ENTRY_CONFIRMATION", "ENTRY_CONFIRMATION"),
        ("ENTRY_CONDITION", "ENTRY_CONDITION"),
        ("STOP_LOSS", "STOP_LOSS"),
        ("TAKE_PROFIT", "TAKE_PROFIT"),
        ("PARTIAL_TP", "PARTIAL_TP"),
        ("POSITION_MANAGEMENT", "POSITION_MANAGEMENT"),
        ("POSITION_UPDATE", "POSITION_UPDATE"),
        ("SETUP_HINT", "SETUP_HINT"),
        ("PRICE_ZONE", "PRICE_ZONE"),
        ("COMMENTARY", "CONTEXT"),
        ("UNKNOWN", "CONTEXT"),
    )

    role_set = set(roles or [])
    for role, relation in priorities:
        if role in role_set:
            return role, relation
    return "UNKNOWN", "CONTEXT"


def persist_bundle_trade_episodes(db, bundle):
    """Simpan anchor dan relasi pesan dari bundle; tidak melakukan commit."""
    if not isinstance(bundle, dict):
        return {"created": 0, "linked": 0, "unresolved": 0}

    chat_id = bundle.get("chat_id")
    messages = bundle.get("messages") or []

    if chat_id is None or not isinstance(messages, list) or not messages:
        return {"created": 0, "linked": 0, "unresolved": 0}

    try:
        chat_id = int(chat_id)
    except (ValueError, TypeError):
        raise TradeEpisodeValidationError("chat_id bundle tidak valid.")

    # 1. Ambil pesan-pesan bundle dari database.
    telegram_ids = set()
    for item in messages:
        if not isinstance(item, dict):
            continue
        message_id = item.get("telegram_message_id")
        if message_id is None:
            continue
        try:
            telegram_ids.add(int(message_id))
        except (ValueError, TypeError):
            continue

    if not telegram_ids:
        return {
            "created": 0,
            "linked": 0,
            "unresolved": len(messages),
        }

    telegram_rows = db.scalars(
        select(TelegramMessage).where(
            TelegramMessage.chat_id == chat_id,
            TelegramMessage.telegram_message_id.in_(telegram_ids),
        )
    ).all()

    message_rows = {
        row.telegram_message_id: row
        for row in telegram_rows
    }

    # 1B. Persist pending entry request terlebih dahulu. Dengan demikian
    # request 15-30 menit sebelumnya tetap tersedia saat zona datang.
    register_pending_entry_requests(
        db,
        bundle,
        message_rows=message_rows,
    )

    # SKIP hanya membatalkan pending request bila membalas pesan request
    # yang spesifik; SKIP tanpa reply tidak membatalkan semua pending.
    for item in messages:
        if not isinstance(item, dict):
            continue
        if "SKIP" in set(item.get("message_roles") or []):
            cancel_pending_entry_from_reply(
                db,
                chat_id=chat_id,
                reply_to_telegram_message_id=item.get("reply_to_id"),
            )

    # 2. Persist semua anchor yang ada di bundle.
    anchors = {}
    episodes_by_key = {}
    created_count = 0

    for item in messages:
        if not isinstance(item, dict):
            continue

        roles = item.get("message_roles") or []
        if "NEW_SIGNAL" not in roles:
            continue

        direction = item.get("signal_direction")
        zone = item.get("entry_zone")

        if not isinstance(direction, str) or not isinstance(zone, dict):
            continue
        if zone.get("low") is None or zone.get("high") is None:
            continue

        try:
            telegram_message_id = int(item["telegram_message_id"])
        except (KeyError, TypeError, ValueError):
            continue

        anchor_message = message_rows.get(telegram_message_id)
        if anchor_message is None:
            continue

        episode_key = _build_episode_key(chat_id, telegram_message_id)
        existed_before = get_trade_episode_by_key(db, episode_key) is not None

        episode = create_or_get_trade_episode(
            db,
            chat_id=chat_id,
            anchor_telegram_message_id=telegram_message_id,
            direction=direction,
            entry_low=zone["low"],
            entry_high=zone["high"],
        )

        anchors[telegram_message_id] = episode
        episodes_by_key[episode.episode_key] = episode

        if not existed_before:
            created_count += 1

        item["trade_episode_id"] = episode.episode_key
        item["trade_episode_status"] = episode.status

    # 2B. Match pending entry yang disimpan dari bundle sebelumnya.
    # Satu kandidat cocok -> relasikan; lebih dari satu -> AMBIGUOUS.
    matched_pending_count = 0
    for telegram_message_id, episode in anchors.items():
        anchor_message = message_rows.get(telegram_message_id)
        if anchor_message is None:
            continue
        match_result = match_pending_entry_to_episode(
            db,
            zone_message=anchor_message,
            episode=episode,
        )
        matched_pending_count += int(match_result.get("matched", 0))

    # Zona tanpa arah yang memiliki lebih dari satu pending kandidat
    # tidak boleh dipasangkan; tandai kandidatnya AMBIGUOUS.
    for item in messages:
        if not isinstance(item, dict):
            continue
        roles = set(item.get("message_roles") or [])
        if "PRICE_ZONE" not in roles or "NEW_SIGNAL" in roles:
            continue
        try:
            telegram_message_id = int(item.get("telegram_message_id"))
        except (TypeError, ValueError):
            continue
        zone_message = message_rows.get(telegram_message_id)
        if zone_message is not None:
            mark_pending_entries_ambiguous_for_zone(
                db,
                chat_id=chat_id,
                zone_message=zone_message,
                direction=None,
            )

    # 3. Resolusi dan link pesan.
    linked_count = matched_pending_count
    unresolved_count = 0

    for item in messages:
        if not isinstance(item, dict):
            continue

        try:
            telegram_message_id = int(item.get("telegram_message_id"))
        except (ValueError, TypeError):
            unresolved_count += 1
            continue

        telegram_message = message_rows.get(telegram_message_id)
        if telegram_message is None:
            unresolved_count += 1
            continue

        roles = item.get("message_roles") or []
        role_set = set(roles)

        existing_mapping = _get_message_mapping(db, telegram_message.id)
        episode = None
        association_method = None

        # Pesan anchor.
        if telegram_message_id in anchors:
            episode = anchors[telegram_message_id]
            association_method = "ANCHOR"

        # Mapping database adalah pemilik yang paling berwenang.
        elif existing_mapping:
            episode = db.get(TradeEpisode, existing_mapping.episode_id)
            association_method = existing_mapping.association_method

        # Coba episode ID dari aggregator/persistent read path.
        if episode is None:
            candidate_key = item.get("trade_episode_id")
            if isinstance(candidate_key, str):
                episode = episodes_by_key.get(candidate_key)
                if episode is None:
                    episode = get_trade_episode_by_key(db, candidate_key)
                if episode is not None:
                    association_method = (
                        item.get("episode_association_method")
                        or "SAME_WINDOW"
                    )

        # Reply Telegram adalah bukti relasi eksplisit.
        if episode is None:
            reply_to_id = item.get("reply_to_id")
            if reply_to_id is not None:
                try:
                    reply_to_id = int(reply_to_id)
                except (TypeError, ValueError):
                    reply_to_id = None

            if reply_to_id is not None:
                parent = _get_telegram_message(db, chat_id, reply_to_id)
                if parent is not None:
                    parent_mapping = _get_message_mapping(db, parent.id)
                    if parent_mapping:
                        episode = db.get(
                            TradeEpisode,
                            parent_mapping.episode_id,
                        )
                        association_method = "REPLY_TO"

        is_anchor = telegram_message_id in anchors

        # Simpan relasi pesan non-anchor.
        if episode is not None and not is_anchor:
            message_role, relation_type = _get_bundle_relation(roles)
            if association_method is None:
                association_method = "SAME_WINDOW"

            if existing_mapping is None:
                link_trade_episode_message(
                    db,
                    episode_id=episode.id,
                    telegram_message_db_id=telegram_message.id,
                    message_role=message_role,
                    relation_type=relation_type,
                    association_method=association_method,
                )
                linked_count += 1
            else:
                link_trade_episode_message(
                    db,
                    episode_id=episode.id,
                    telegram_message_db_id=telegram_message.id,
                    message_role=message_role,
                    relation_type=relation_type,
                    association_method=existing_mapping.association_method,
                )

            if "SKIP" in role_set:
                episode.status = "SKIP_REQUESTED"

            item["trade_episode_id"] = episode.episode_key
            item["trade_episode_status"] = episode.status

        elif episode is None and not is_anchor:
            unresolved_count += 1

    db.flush()

    return {
        "created": created_count,
        "linked": linked_count,
        "unresolved": unresolved_count,
    }
