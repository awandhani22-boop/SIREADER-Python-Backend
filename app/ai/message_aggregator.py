import re
from datetime import timedelta

from sqlalchemy import select, func, exists, or_

from app.database.connection import SessionLocal
from app.database.models import (
    TelegramMessage,
    TelegramMessageEdit,
)
from app.signals.trade_episode_repository import (
    get_trade_episode_for_message,
    list_trade_episode_messages,
    find_pending_entry_candidates,
    get_pending_entry_by_source_message,
)


# ============================================================
# MESSAGE ROLES
# ============================================================

ROLE_NEW_SIGNAL = "NEW_SIGNAL"
ROLE_ENTRY_REQUEST = "ENTRY_REQUEST"
ROLE_ENTRY_CONFIRMATION = "ENTRY_CONFIRMATION"
ROLE_REENTRY = "REENTRY"  # Legacy compatibility
ROLE_REENTRY_PLAN = "REENTRY_PLAN"
ROLE_PRICE_ZONE = "PRICE_ZONE"
ROLE_ENTRY_CONDITION = "ENTRY_CONDITION"
ROLE_SETUP_HINT = "SETUP_HINT"
ROLE_STOP_LOSS = "STOP_LOSS"
ROLE_TAKE_PROFIT = "TAKE_PROFIT"
ROLE_PARTIAL_TP = "PARTIAL_TP"
ROLE_POSITION_UPDATE = "POSITION_UPDATE"
ROLE_POSITION_MANAGEMENT = "POSITION_MANAGEMENT"
ROLE_POSITION_CLOSE = "POSITION_CLOSE"  # Legacy; no longer inferred
ROLE_SKIP = "SKIP"
ROLE_COMMENTARY = "COMMENTARY"
ROLE_UNKNOWN = "UNKNOWN"


# ============================================================
# GENERAL HELPERS
# ============================================================

def get_message_time(message):
    """Utamakan waktu asli Telegram. received_at menjadi cadangan."""
    return message.telegram_date or message.received_at


def _has_text(value):
    return isinstance(value, str) and bool(value.strip())


def _normalize_text(text):
    return re.sub(r"\s+", " ", text.lower()).strip()


def _extract_direction(text):
    """Mengambil arah BUY/SELL jika disebutkan secara eksplisit."""
    normalized = _normalize_text(text)
    match = re.search(r"\b(buy|sell)\b", normalized)
    return match.group(1).upper() if match else None


def _extract_price_range(text):
    """Mengambil rentang harga; hasil selalu (harga_bawah, harga_atas)."""
    if not _has_text(text):
        return None

    match = re.search(
        r"(?<!\d)"
        r"(\d{3,5}(?:\.\d+)?)"
        r"\s*[-–—]\s*"
        r"(\d{3,5}(?:\.\d+)?)"
        r"(?!\d)",
        text,
    )

    if not match:
        return None

    first = float(match.group(1))
    second = float(match.group(2))

    if first == second:
        return None

    return min(first, second), max(first, second)


def _is_reentry_plan(normalized):
    """Rencana mencari re-entry bukan instruksi entry aktual."""
    patterns = (
        r"\b(?:ntar|nanti)\b.{0,50}\bcarikan\b.{0,30}\bre[\s-]?entry\b",
        r"\bcarikan\b.{0,30}\bre[\s-]?entry\b",
        r"\bcari\b.{0,30}\bre[\s-]?entry\b",
        r"\bre[\s-]?entry\b.{0,30}\b(?:nanti|ntar)\b",
        r"\bentry\s+ulang\b",
        r"\bmasuk\s+ulang\b",
    )

    return any(re.search(pattern, normalized) for pattern in patterns)


# ============================================================
# MESSAGE CLASSIFICATION
# ============================================================

def classify_message(text):
    """Klasifikasi pesan mentor.

    Penting:
    - SL dan TP hanya informasi.
    - Hit SL tidak otomatis menutup episode.
    - Try BUY/SELL menunggu zona.
    - Rencana re-entry bukan sinyal aktual.
    - Zona tanpa arah dapat dipasangkan oleh annotator
      dengan instruksi BUY/SELL sebelumnya.
    """
    if not _has_text(text):
        return [ROLE_UNKNOWN]

    normalized = _normalize_text(text)
    roles = []

    # 1. SKIP
    skip_patterns = (
        r"\bskip\b",
        r"\bskip\s+aja\b",
        r"\bskip\s+dulu\b",
        r"\bjangan\s+entry\b",
        r"\bjangan\s+ambil\b",
    )

    if any(re.search(pattern, normalized) for pattern in skip_patterns):
        roles.append(ROLE_SKIP)

    # 2. RE-ENTRY PLAN
    if _is_reentry_plan(normalized):
        roles.append(ROLE_REENTRY_PLAN)

    # 3. ENTRY CONDITION
    condition_patterns = (
        r"\bcisd\b",
        r"\bkonfirmasi\b",
        r"\bconfirmation\b",
        r"\btunggu\s+candle\b",
        r"\btunggu\s+close\b",
        r"\bmenunggu\s+validasi\b",
        r"\btunggu\s+validasi\b",
    )

    if any(re.search(pattern, normalized) for pattern in condition_patterns):
        roles.append(ROLE_ENTRY_CONDITION)

    # 4. STOP LOSS INFORMATION
    sl_patterns = (
        r"\bhit\s+sl\b",
        r"\bstop\s+loss\s+kena\b",
        r"\bsl\s+kena\b",
        r"\bsl\s*[:=]?\s*\d",
        r"\bstop\s+loss\b",
    )

    if any(re.search(pattern, normalized) for pattern in sl_patterns):
        roles.append(ROLE_STOP_LOSS)

    # 5. TAKE PROFIT INFORMATION
    tp_patterns = (
        r"\btp\s*\d*\b",
        r"\btake\s+profit\b",
        r"\btarget\s+\d+\b",
    )

    if any(re.search(pattern, normalized) for pattern in tp_patterns):
        if re.search(r"\btp\s*[1-5]\b", normalized):
            roles.append(ROLE_PARTIAL_TP)
        else:
            roles.append(ROLE_TAKE_PROFIT)

    # 6. POSITION MANAGEMENT INFORMATION
    management_patterns = (
        r"\bamanin\b",
        r"\bbreakeven\b",
        r"\bbe\b",
        r"\bpindah\s+sl\b",
        r"\bgeser\s+sl\b",
        r"\bsecure\s+profit\b",
        r"\bpartial\b",
        r"\bclose\s+all\b",
        r"\bclose\s+position\b",
        r"\bclose\s+posisi\b",
        r"\bclosed\s+all\b",
        r"\bcut\s+loss\b",
    )

    if any(re.search(pattern, normalized) for pattern in management_patterns):
        roles.append(ROLE_POSITION_MANAGEMENT)

    update_patterns = (
        r"\d+\s*pips?\b",
        r"\btahan\s+aja\b",
        r"\btahan\s+dulu\b",
        r"\bfloating\b",
        r"\brunning\b",
        r"\bjunam\b",
        r"\bterbang\b",
        r"\bprofit\b",
    )

    if any(re.search(pattern, normalized) for pattern in update_patterns):
        roles.append(ROLE_POSITION_UPDATE)

    # 7. ENTRY INTENT
    direction = _extract_direction(normalized)
    is_reentry_plan = ROLE_REENTRY_PLAN in roles

    entry_request_patterns = (
        r"\btry\s+(buy|sell)\b",
        r"\byuk\s+(?:try\s+)?(buy|sell)\b",
        r"\b(buy|sell)\s+(?:again|lagi|now|sekarang)\b",
        r"\bentry\s+(buy|sell)\b",
        r"\bopen\s+(buy|sell)\b",
        r"\bambil\s+(buy|sell)\b",
    )

    has_entry_request = any(
        re.search(pattern, normalized)
        for pattern in entry_request_patterns
    )

    if (
        has_entry_request
        and direction
        and not is_reentry_plan
        and ROLE_SKIP not in roles
    ):
        roles.append(ROLE_ENTRY_REQUEST)

    # 8. PRICE ZONE
    price_range = _extract_price_range(text)

    has_zone_keyword = bool(
        re.search(
            r"\b(buy|sell)\s+(?:zone|area)\b"
            r"|\bzone\s+(?:buy|sell)\b"
            r"|\barea\s+(?:buy|sell)\b"
            r"|\b(buy|sell)\s+di\s+\d",
            normalized,
        )
    )

    if price_range:
        if direction or has_zone_keyword:
            if not is_reentry_plan and ROLE_SKIP not in roles:
                roles.append(ROLE_NEW_SIGNAL)
        else:
            roles.append(ROLE_PRICE_ZONE)

    # 9. SETUP HINT
    setup_patterns = (
        r"\bcari\s+(buy|sell)\s+disini\b",
        r"\bcari\s+(buy|sell)\b",
        r"\bpantau\s+(buy|sell)\b",
        r"\bsiapkan\s+(buy|sell)\b",
        r"\bprepare\s+(buy|sell)\b",
    )

    if (
        any(re.search(pattern, normalized) for pattern in setup_patterns)
        and not is_reentry_plan
    ):
        roles.append(ROLE_SETUP_HINT)

    # 10. FALLBACK
    if not roles:
        if direction:
            roles.append(ROLE_UNKNOWN)
        else:
            roles.append(ROLE_COMMENTARY)

    return list(dict.fromkeys(roles))


# ============================================================
# TRADE EPISODE ID
# ============================================================

def _build_trade_episode_id(
    message,
    chat_id=None,
    fallback_number=None,
):
    """Membuat ID episode stabil dari pesan asal Telegram."""
    if not isinstance(message, dict):
        raise ValueError("Pesan untuk episode tidak valid.")

    message_id = message.get("telegram_message_id")
    message_chat_id = message.get("chat_id")

    if message_chat_id is None:
        message_chat_id = chat_id

    if message_id is not None:
        if message_chat_id is not None:
            return f"trade_{message_chat_id}_{message_id}"
        return f"trade_message_{message_id}"

    if fallback_number is not None:
        return f"trade_{fallback_number}"

    raise ValueError(
        "Pesan tidak memiliki identitas untuk membuat episode."
    )


# ============================================================
# TRADE EPISODE GROUPING
# ============================================================

def annotate_trade_episodes(messages, chat_id=None):
    """Mengelompokkan instruksi sinyal berdasarkan urutan pesan."""
    active_episode = None
    episode_counter = 0
    pending_direction = None

    informational_roles = {
        ROLE_ENTRY_CONDITION,
        ROLE_STOP_LOSS,
        ROLE_TAKE_PROFIT,
        ROLE_PARTIAL_TP,
        ROLE_POSITION_UPDATE,
        ROLE_POSITION_MANAGEMENT,
        ROLE_ENTRY_CONFIRMATION,
    }

    for message in messages:
        roles = message.get("message_roles") or []
        text = message.get("classification_text") or message.get("text") or ""

        direction = _extract_direction(text)
        price_range = _extract_price_range(text)

        episode_id = None
        episode_status = None

        # 1. SKIP
        if ROLE_SKIP in roles:
            if active_episode:
                episode_id = active_episode
                episode_status = "SKIP_REQUESTED"

            pending_direction = None
            active_episode = None

        # 2. DIRECT SIGNAL WITH DIRECTION + ZONE
        elif ROLE_NEW_SIGNAL in roles:
            episode_counter += 1

            active_episode = _build_trade_episode_id(
                message,
                chat_id=chat_id,
                fallback_number=episode_counter,
            )

            episode_id = active_episode
            episode_status = "OPEN"
            pending_direction = None

            message["signal_direction"] = direction
            message["entry_zone"] = {
                "low": price_range[0],
                "high": price_range[1],
                "midpoint": (price_range[0] + price_range[1]) / 2,
            } if price_range else None

        # 3. ENTRY REQUEST
        elif ROLE_ENTRY_REQUEST in roles:
            pending_direction = direction
            episode_id = None
            episode_status = "WAITING_FOR_ZONE"
            message["pending_direction"] = direction

        # 4. ZONE WITHOUT DIRECTION
        elif (
            ROLE_PRICE_ZONE in roles
            and pending_direction
            and price_range
        ):
            episode_counter += 1

            active_episode = _build_trade_episode_id(
                message,
                chat_id=chat_id,
                fallback_number=episode_counter,
            )

            episode_id = active_episode
            episode_status = "OPEN"

            message["message_roles"] = list(
                dict.fromkeys(roles + [ROLE_NEW_SIGNAL])
            )
            message["message_role"] = message["message_roles"][0]
            message["signal_direction"] = pending_direction
            message["entry_zone"] = {
                "low": price_range[0],
                "high": price_range[1],
                "midpoint": (price_range[0] + price_range[1]) / 2,
            }

            pending_direction = None

        # 5. FOLLOW-UP INFORMATION
        elif (
            active_episode
            and any(role in informational_roles for role in roles)
        ):
            episode_id = active_episode
            episode_status = "OPEN"

        # 6. SET MESSAGE METADATA
        message["trade_episode_id"] = episode_id
        message["trade_episode_status"] = episode_status

        if (
            ROLE_ENTRY_REQUEST in roles
            and episode_status == "WAITING_FOR_ZONE"
        ):
            message["pending_direction"] = direction

    return messages


# ============================================================
# CLASSIFICATION + GROUPING
# ============================================================

def classify_and_group_messages(messages, chat_id=None):
    """Klasifikasi dan pengelompokan pesan kronologis."""
    for message in messages:
        text = message.get("text") or ""

        if not _has_text(text):
            history = message.get("edit_history") or []

            for edit in reversed(history):
                candidate = edit.get("new_text")
                if _has_text(candidate):
                    text = candidate
                    break

        message["classification_text"] = text
        roles = classify_message(text)
        message["message_roles"] = roles
        message["message_role"] = roles[0]

    annotate_trade_episodes(messages, chat_id=chat_id)

    for message in messages:
        message.pop("classification_text", None)

    return messages


# ============================================================
# PERSISTENT EPISODE READ PATH
# ============================================================

def _attach_persistent_episode_context(
    db,
    chat_id,
    target,
    result,
    serialize_message,
    has_text_content,
    max_messages,
):
    """Membaca riwayat episode melalui mapping atau reply eksplisit.

    Fungsi ini read-only. Tidak membuat mapping dan tidak melakukan commit.
    Pesan tanpa mapping target atau reply parent yang valid tidak dipaksa
    masuk ke episode hanya karena ada episode berstatus OPEN.
    """
    persistent_metadata = {}

    # Mapping persisten untuk pesan yang sudah ada di bundle.
    for item in result:
        database_id = item.get("_database_id")
        if database_id is None:
            continue

        telegram_message = db.get(TelegramMessage, database_id)
        if telegram_message is None or telegram_message.chat_id != chat_id:
            continue

        episode = get_trade_episode_for_message(db, telegram_message.id)
        if episode is not None:
            persistent_metadata[telegram_message.telegram_message_id] = (
                episode.episode_key,
                episode.status,
                "PERSISTENT_LOOKUP",
            )

    # Resolusi episode target melalui mapping langsung.
    target_episode = get_trade_episode_for_message(db, target.id)
    reply_parent = None
    target_association_method = "PERSISTENT_LOOKUP"

    # Jika target belum punya mapping, hanya reply eksplisit yang boleh
    # menjadi bukti relasi ke episode parent.
    if target_episode is None and target.reply_to_id is not None:
        reply_parent = db.scalar(
            select(TelegramMessage).where(
                TelegramMessage.chat_id == chat_id,
                TelegramMessage.telegram_message_id == target.reply_to_id,
            )
        )

        if reply_parent is not None:
            target_episode = get_trade_episode_for_message(
                db,
                reply_parent.id,
            )
            if target_episode is not None:
                target_association_method = "REPLY_TO"
                persistent_metadata[target.telegram_message_id] = (
                    target_episode.episode_key,
                    target_episode.status,
                    "REPLY_TO",
                )

    if target_episode is None:
        return persistent_metadata

    # Ambil hanya pesan yang memang sudah tercatat di episode target.
    mappings = list_trade_episode_messages(db, target_episode.id)
    history_rows = []

    for mapping in mappings:
        telegram_message = db.get(
            TelegramMessage,
            mapping.telegram_message_db_id,
        )
        if telegram_message is None:
            continue
        if telegram_message.chat_id != chat_id:
            continue
        if not has_text_content(telegram_message):
            continue
        history_rows.append(telegram_message)

    history_rows.sort(
        key=lambda item: (
            get_message_time(item),
            item.telegram_message_id,
        )
    )

    try:
        history_limit = max(1, min(int(max_messages), 15))
    except (TypeError, ValueError):
        history_limit = 15

    priority_rows = []
    anchor = db.get(TelegramMessage, target_episode.anchor_message_id)

    if (
        anchor is not None
        and anchor.chat_id == chat_id
        and has_text_content(anchor)
    ):
        priority_rows.append(anchor)

    if reply_parent is not None:
        parent_episode = get_trade_episode_for_message(db, reply_parent.id)
        if (
            parent_episode is not None
            and parent_episode.id == target_episode.id
            and has_text_content(reply_parent)
        ):
            priority_rows.append(reply_parent)

    selected_rows = []
    selected_ids = set()

    def add_selected(row):
        if row is None or row.id in selected_ids:
            return
        if len(selected_rows) >= history_limit:
            return
        selected_rows.append(row)
        selected_ids.add(row.id)

    for row in priority_rows:
        add_selected(row)

    # Tambahkan pesan episode terbaru, tetap menjaga anchor.
    for row in reversed(history_rows):
        if len(selected_rows) >= history_limit:
            break
        add_selected(row)

    result_by_database_id = {
        item.get("_database_id"): item
        for item in result
    }

    for telegram_message in selected_rows:
        database_id = telegram_message.id

        if database_id not in result_by_database_id:
            item = serialize_message(
                telegram_message,
                "persistent_episode",
            )
            result.append(item)
            result_by_database_id[database_id] = item

        method = (
            "ANCHOR"
            if database_id == target_episode.anchor_message_id
            else "PERSISTENT_LOOKUP"
        )

        # Untuk target reply yang belum tersimpan mapping-nya, pertahankan
        # metode relasi REPLY_TO; mapping belum dibuat di fungsi read-only.
        if telegram_message.id == target.id and target_association_method == "REPLY_TO":
            method = "REPLY_TO"

        persistent_metadata[telegram_message.telegram_message_id] = (
            target_episode.episode_key,
            target_episode.status,
            method,
        )

    return persistent_metadata


# ============================================================
# PERSISTENT PENDING ENTRY CONTEXT
# ============================================================

def _message_classification_text(item):
    text = item.get("text") or ""
    if _has_text(text):
        return text
    history = item.get("edit_history") or []
    for edit in reversed(history):
        candidate = edit.get("new_text")
        if _has_text(candidate):
            return candidate
    return ""


def _attach_pending_entry_context(
    db,
    *,
    chat_id,
    result,
    serialize_message,
    has_text_content,
):
    """Tambah request lama/satu-bundle hanya jika kandidatnya unik.

    Untuk zona tanpa arah, semua kandidat BUY/SELL diperiksa. Sistem tidak
    boleh mengikuti pending_direction terakhir secara membabi-buta.
    """
    added_source_ids = set()
    zone_items = list(result)

    def as_utc(value):
        if value is None:
            return None
        from datetime import datetime, timezone
        if isinstance(value, str):
            try:
                value = datetime.fromisoformat(value)
            except ValueError:
                return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    for item in zone_items:
        if not isinstance(item, dict):
            continue

        roles = set(item.get("message_roles") or [])
        if "NEW_SIGNAL" not in roles and "PRICE_ZONE" not in roles:
            continue

        classification_text = _message_classification_text(item)
        price_range = _extract_price_range(classification_text)
        if price_range is None:
            continue

        try:
            telegram_message_id = int(item.get("telegram_message_id"))
        except (TypeError, ValueError):
            continue

        zone_message = db.scalar(
            select(TelegramMessage).where(
                TelegramMessage.chat_id == chat_id,
                TelegramMessage.telegram_message_id == telegram_message_id,
            )
        )
        if zone_message is None:
            continue

        zone_time = as_utc(
            zone_message.telegram_date or zone_message.received_at
        )
        if zone_time is None:
            continue

        # Arah eksplisit berasal dari isi pesan zona itu sendiri.
        explicit_direction = _extract_direction(classification_text)
        inferred_direction = item.get("signal_direction")
        direction_was_inferred = (
            explicit_direction is None
            and "PRICE_ZONE" in roles
            and "NEW_SIGNAL" in roles
        )
        direction_filter = explicit_direction

        # Kandidat yang telah direkam pada pemrosesan sebelumnya.
        persisted_candidates = find_pending_entry_candidates(
            db,
            chat_id=chat_id,
            zone_message=zone_message,
            direction=direction_filter,
        )

        candidates = {}
        for pending in persisted_candidates:
            source = db.get(TelegramMessage, pending.source_message_id)
            if source is not None:
                candidates[source.id] = {
                    "source": source,
                    "direction": pending.direction,
                    "pending": pending,
                    "item": None,
                }

        # Tambahkan request yang baru muncul dalam bundle yang sama dan
        # belum sempat ditulis ke tabel pending oleh pipeline.
        ttl_seconds = 30 * 60
        for request_item in list(result):
            if not isinstance(request_item, dict):
                continue
            request_roles = set(request_item.get("message_roles") or [])
            if "ENTRY_REQUEST" not in request_roles or "NEW_SIGNAL" in request_roles:
                continue

            try:
                request_telegram_id = int(
                    request_item.get("telegram_message_id")
                )
            except (TypeError, ValueError):
                continue

            request_source = db.scalar(
                select(TelegramMessage).where(
                    TelegramMessage.chat_id == chat_id,
                    TelegramMessage.telegram_message_id == request_telegram_id,
                )
            )
            if request_source is None or request_source.id == zone_message.id:
                continue

            # Jangan anggap ulang sebuah request yang sebelumnya sudah
            # dipetakan ke episode permanen.
            if get_trade_episode_for_message(db, request_source.id) is not None:
                continue

            existing_pending = get_pending_entry_by_source_message(
                db,
                request_source.id,
            )
            if existing_pending is not None:
                # WAITING sudah diperiksa lewat query persisted. Status final
                # seperti MATCHED/CANCELLED/EXPIRED tidak boleh dihidupkan lagi.
                continue

            request_time = as_utc(
                request_source.telegram_date or request_source.received_at
            )
            if request_time is None or request_time >= zone_time:
                continue
            age_seconds = (zone_time - request_time).total_seconds()
            if age_seconds < 0 or age_seconds > ttl_seconds:
                continue

            request_direction = request_item.get("pending_direction")
            if request_direction not in ("BUY", "SELL"):
                request_direction = _extract_direction(
                    _message_classification_text(request_item)
                )
            if request_direction not in ("BUY", "SELL"):
                continue
            if (
                direction_filter is not None
                and request_direction != direction_filter
            ):
                continue

            candidates[request_source.id] = {
                "source": request_source,
                "direction": request_direction,
                "pending": None,
                "item": request_item,
            }

        # Zona tanpa arah tidak boleh memakai pending_direction terakhir
        # yang kebetulan dipilih oleh grouping sementara.
        if len(candidates) > 1:
            item["pending_entry_ambiguous"] = True
            item["pending_entry_candidate_count"] = len(candidates)
            if direction_was_inferred:
                item["message_roles"] = [
                    role for role in item.get("message_roles", [])
                    if role != "NEW_SIGNAL"
                ]
                item["message_role"] = (
                    item["message_roles"][0]
                    if item["message_roles"]
                    else "PRICE_ZONE"
                )
                item.pop("signal_direction", None)
                item.pop("entry_zone", None)
                item.pop("trade_episode_id", None)
                item.pop("trade_episode_status", None)
            continue

        if not candidates:
            if direction_was_inferred:
                # Tidak ada bukti persisted atau request se-bundle yang cocok.
                item["message_roles"] = [
                    role for role in item.get("message_roles", [])
                    if role != "NEW_SIGNAL"
                ]
                item["message_role"] = (
                    item["message_roles"][0]
                    if item["message_roles"]
                    else "PRICE_ZONE"
                )
                item.pop("signal_direction", None)
                item.pop("entry_zone", None)
                item.pop("trade_episode_id", None)
                item.pop("trade_episode_status", None)
            continue

        candidate = next(iter(candidates.values()))
        source = candidate["source"]
        direction = candidate["direction"]

        if not has_text_content(source):
            continue

        if explicit_direction is None:
            new_roles = list(item.get("message_roles") or [])
            if "NEW_SIGNAL" not in new_roles:
                new_roles.append("NEW_SIGNAL")
            item["message_roles"] = new_roles
            item["message_role"] = new_roles[0]
            item["signal_direction"] = direction
            item["entry_zone"] = {
                "low": price_range[0],
                "high": price_range[1],
                "midpoint": (price_range[0] + price_range[1]) / 2,
            }

        episode_key = f"trade_{chat_id}_{telegram_message_id}"
        item["trade_episode_id"] = episode_key
        item["trade_episode_status"] = "OPEN"
        item["pending_entry_source_telegram_message_id"] = (
            source.telegram_message_id
        )
        item["pending_entry_match_method"] = "PERSISTENT_PENDING"

        # Request lama yang di luar window ditambahkan sebagai bukti AI.
        source_item = candidate["item"]
        if source_item is None:
            source_item = next(
                (
                    existing for existing in result
                    if existing.get("telegram_message_id") == source.telegram_message_id
                ),
                None,
            )
        if source_item is None:
            source_item = serialize_message(
                source,
                "persistent_pending_entry",
            )
            source_item["message_roles"] = ["ENTRY_REQUEST"]
            source_item["message_role"] = "ENTRY_REQUEST"
            source_item["pending_direction"] = direction
            source_item["trade_episode_id"] = episode_key
            source_item["trade_episode_status"] = "OPEN"
            source_item["episode_association_method"] = "PERSISTENT_PENDING"
            result.append(source_item)
        else:
            source_item["trade_episode_id"] = episode_key
            source_item["trade_episode_status"] = "OPEN"

        added_source_ids.add(source.id)

    return len(added_source_ids)


# ============================================================
# MESSAGE AGGREGATOR
# ============================================================

def collect_message_bundle(
    chat_id,
    telegram_message_id,
    window_minutes=10,
    max_messages=15,
    db=None,
):
    """Mengumpulkan konteks teks. Session eksternal tetap milik pemanggil."""
    if db is not None:
        return _collect_message_bundle_impl(
            db,
            chat_id,
            telegram_message_id,
            window_minutes,
            max_messages,
        )

    with SessionLocal() as owned_db:
        return _collect_message_bundle_impl(
            owned_db,
            chat_id,
            telegram_message_id,
            window_minutes,
            max_messages,
        )


def _collect_message_bundle_impl(
    db,
    chat_id,
    telegram_message_id,
    window_minutes,
    max_messages,
):
    """Implementasi inti pengumpulan pesan."""
    edit_cache = {}

    def get_edits(database_id):
        if database_id not in edit_cache:
            edits = db.scalars(
                select(TelegramMessageEdit)
                .where(TelegramMessageEdit.message_id == database_id)
                .order_by(TelegramMessageEdit.edited_at.asc())
            ).all()

            edit_cache[database_id] = [
                {
                    "old_text": edit.old_text or "",
                    "new_text": edit.new_text or "",
                    "edited_at": (
                        edit.edited_at.isoformat()
                        if edit.edited_at
                        else None
                    ),
                }
                for edit in edits
            ]

        return edit_cache[database_id]

    def has_text_content(message):
        if _has_text(message.message_text):
            return True

        for edit in get_edits(message.id):
            if _has_text(edit["old_text"]) or _has_text(edit["new_text"]):
                return True

        return False

    def serialize_message(message, reason):
        return {
            "_database_id": message.id,
            "telegram_message_id": message.telegram_message_id,
            "text": message.message_text or "",
            "sender": message.sender,
            "reply_to_id": message.reply_to_id,
            "received_at": (
                message.received_at.isoformat()
                if message.received_at
                else None
            ),
            "telegram_date": (
                message.telegram_date.isoformat()
                if message.telegram_date
                else None
            ),
            "is_edited": message.is_edited,
            "reason": reason,
            "edit_history": get_edits(message.id),
        }

    # 1. TARGET MESSAGE
    target = db.scalar(
        select(TelegramMessage).where(
            TelegramMessage.chat_id == chat_id,
            TelegramMessage.telegram_message_id == telegram_message_id,
        )
    )

    if not target or not has_text_content(target):
        return None

    target_time = get_message_time(target)
    start_time = target_time - timedelta(minutes=window_minutes)
    end_time = target_time + timedelta(minutes=window_minutes)

    # 2. TEXT MESSAGES IN WINDOW
    message_time = func.coalesce(
        TelegramMessage.telegram_date,
        TelegramMessage.received_at,
    )

    edit_has_text = exists(
        select(1).where(
            TelegramMessageEdit.message_id == TelegramMessage.id,
            or_(
                func.trim(func.coalesce(TelegramMessageEdit.old_text, "")) != "",
                func.trim(func.coalesce(TelegramMessageEdit.new_text, "")) != "",
            ),
        )
    )

    has_text = or_(
        func.trim(func.coalesce(TelegramMessage.message_text, "")) != "",
        edit_has_text,
    )

    candidates = db.scalars(
        select(TelegramMessage).where(
            TelegramMessage.chat_id == chat_id,
            message_time >= start_time,
            message_time <= end_time,
            has_text,
        )
    ).all()

    candidates = sorted(
        candidates,
        key=lambda item: abs(
            (get_message_time(item) - target_time).total_seconds()
        ),
    )[:max_messages]

    messages = {}

    for item in candidates:
        messages[item.id] = serialize_message(
            item,
            "same_chat_time_window",
        )

    messages[target.id] = serialize_message(target, "target")

    # 3. REPLY PARENT
    if target.reply_to_id:
        original = db.scalar(
            select(TelegramMessage).where(
                TelegramMessage.chat_id == chat_id,
                TelegramMessage.telegram_message_id == target.reply_to_id,
            )
        )

        if (
            original
            and original.id not in messages
            and has_text_content(original)
        ):
            messages[original.id] = serialize_message(
                original,
                "reply_parent",
            )

    # 4. CHRONOLOGICAL ORDER
    result = sorted(
        messages.values(),
        key=lambda item: (
            item["telegram_date"] or item["received_at"],
            item["telegram_message_id"],
        ),
    )

    # 5. PERSISTENT EPISODE READ
    persistent_metadata = _attach_persistent_episode_context(
        db=db,
        chat_id=chat_id,
        target=target,
        result=result,
        serialize_message=serialize_message,
        has_text_content=has_text_content,
        max_messages=max_messages,
    )

    result = sorted(
        result,
        key=lambda item: (
            item["telegram_date"] or item["received_at"],
            item["telegram_message_id"],
        ),
    )

    # 6. CLASSIFY AND GROUP
    classify_and_group_messages(result, chat_id=chat_id)

    # 6B. PERSISTENT PENDING ENTRY CONTEXT
    _attach_pending_entry_context(
        db,
        chat_id=chat_id,
        result=result,
        serialize_message=serialize_message,
        has_text_content=has_text_content,
    )

    # Urutkan ulang setelah request lama ditambahkan.
    result = sorted(
        result,
        key=lambda item: (
            item["telegram_date"] or item["received_at"],
            item["telegram_message_id"],
        ),
    )

    # 7. RESTORE PERSISTENT IDENTITIES AFTER TEMPORARY GROUPING
    for item in result:
        telegram_id = item.get("telegram_message_id")
        persistent = persistent_metadata.get(telegram_id)

        if persistent is not None:
            episode_key, episode_status, association_method = persistent
            item["trade_episode_id"] = episode_key
            item["trade_episode_status"] = episode_status
            item["episode_association_method"] = association_method

        # ID database internal tidak boleh dikirim ke AI Router.
        item.pop("_database_id", None)

    return {
        "chat_id": chat_id,
        "target_message_id": telegram_message_id,
        "window_minutes": window_minutes,
        "messages": result,
    }
