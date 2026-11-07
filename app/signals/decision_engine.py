
"""
SIREADER Decision Engine.

Tanggung jawab:
- Memeriksa hasil AI Router.
- Mengidentifikasi sinyal dan zona entry.
- Menghitung midpoint.
- Menghitung SL tetap 30 pips.
- Membaca kondisi CISD.
- Memastikan konfirmasi CISD berasal dari episode yang sama.
- Memeriksa jarak harga terhadap midpoint.
- Mengklasifikasikan jenis entry.
- Menentukan keputusan APPROVAL atau AUTO DRY_RUN.

Tidak melakukan:
- Query atau perubahan database.
- Pengiriman order MT5.
- Pembatalan order MT5.
- Penutupan posisi MT5.

Semua keputusan bersifat deterministik dan dapat diuji.
"""

import re

from decimal import Decimal, InvalidOperation

from app.execution.price_math import (
    PIP_PRICE,
    calculate_price_from_pips,
)


# ============================================================
# CONSTANTS
# ============================================================

FIXED_SL_PIPS = Decimal("30")
MAX_ENTRY_DISTANCE = FIXED_SL_PIPS * PIP_PRICE

VALID_DIRECTIONS = {"BUY", "SELL"}
VALID_EXECUTION_MODES = {"APPROVAL", "AUTO"}

SIGNAL_SOURCE_ROLES = {
    "NEW_SIGNAL",
    "ENTRY_REQUEST",
    "PRICE_ZONE",
    "ENTRY_CONDITION",
    "SETUP_HINT",
}

ZONE_PATTERN = re.compile(
    r"(?<![\d.,])"
    r"(\d{3,5}(?:[.,]\d{1,3})?)"
    r"\s*(?:-|–|—|\bto\b|\bsampai\b)\s*"
    r"(\d{3,5}(?:[.,]\d{1,3})?)"
    r"(?![\d.,])",
    re.IGNORECASE,
)

TP_PIPS_PATTERN = re.compile(
    r"(?<!\d)(\d+(?:[.,]\d+)?)\s*pips?\b",
    re.IGNORECASE,
)

CISD_CONFIRMED_PATTERN = re.compile(
    r"\bcisd\b.{0,40}\b"
    r"(?:valid|confirmed|terkonfirmasi|terbentuk|muncul|sudah)\b"
    r"|"
    r"\b(?:valid|confirmed|terkonfirmasi|terbentuk|muncul|sudah)\b"
    r".{0,40}\bcisd\b",
    re.IGNORECASE,
)

CISD_NEGATIVE_PATTERN = re.compile(
    r"\b(?:belum|tidak|not|isn't|isnt|pending)\b"
    r".{0,40}\bcisd\b"
    r"|"
    r"\bcisd\b.{0,40}\b"
    r"(?:belum|tidak|not|pending)\b",
    re.IGNORECASE,
)

CISD_WAIT_PATTERN = re.compile(
    r"\b(?:tunggu|menunggu|wait|waiting|await)\b"
    r".{0,50}\bcisd\b"
    r"|"
    r"\bcisd\b.{0,50}\b"
    r"(?:tunggu|menunggu|wait|waiting)\b",
    re.IGNORECASE,
)


# ============================================================
# BASIC HELPERS
# ============================================================

def _to_decimal(value):
    """
    Konversi angka menjadi Decimal tanpa menerima boolean.

    Mendukung angka desimal dengan koma.
    """

    if isinstance(value, bool) or value is None:
        raise ValueError("Nilai angka tidak valid.")

    try:
        result = Decimal(
            str(value).strip().replace(",", ".")
        )

    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("Nilai angka tidak valid.")

    if not result.is_finite():
        raise ValueError("Nilai angka harus finite.")

    return result


def _format_decimal(value):
    """Format Decimal menjadi string yang rapi."""

    value = Decimal(value)
    text = format(value, "f")

    if "." in text:
        text = text.rstrip("0").rstrip(".")

    return text or "0"


def _result(status, reason, **values):
    """Membuat struktur keputusan dengan format konsisten."""

    payload = {
        "status": status,
        "reason": reason,

        "direction": None,

        "entry_zone": None,
        "entry_price_reference": None,
        "entry_type": None,

        "stop_loss_pips": _format_decimal(FIXED_SL_PIPS),
        "spread_pips": None,
        "maximum_spread_pips": _format_decimal(FIXED_SL_PIPS),
        "stop_loss_price_reference": None,

        "take_profit_targets_pips": [],
        "take_profit_prices_reference": [],
        "take_profit_source": "NONE",

        "requires_cisd": False,
        "cisd_confirmed": False,

        "trade_episode_id": None,

        "execution_mode": None,
        "trading_mode": None,

        "skip_scope": None,
    }

    payload.update(values)

    return payload


# ============================================================
# MESSAGE METADATA
# ============================================================

def _message_roles(message):
    """Mengambil semua role dari satu pesan."""

    roles = set()

    if not isinstance(message, dict):
        return roles

    role = message.get("message_role")

    if isinstance(role, str):
        roles.add(role.upper())

    message_roles = message.get("message_roles")

    if isinstance(message_roles, list):
        roles.update(
            item.upper()
            for item in message_roles
            if isinstance(item, str)
        )

    return roles


def _get_message_context(bundle):
    """Membaca seluruh role pesan dari bundle."""

    if not isinstance(bundle, dict):
        return set(), []

    messages = bundle.get("messages")

    if not isinstance(messages, list):
        return set(), []

    all_roles = set()

    for message in messages:
        all_roles.update(_message_roles(message))

    return all_roles, messages


def _resolve_signal_episode_id(
    bundle,
    explicit_episode_id=None,
):
    """
    Menentukan episode sinyal yang sedang dievaluasi.

    ID dicari dari:
    - ID eksplisit pemanggil.
    - ID bundle.
    - Pesan pembentuk sinyal.

    ENTRY_CONFIRMATION tidak boleh menentukan episode target.
    """

    candidates = set()

    if explicit_episode_id is not None:
        candidates.add(explicit_episode_id)

    if not isinstance(bundle, dict):
        if len(candidates) == 1:
            return next(iter(candidates))

        return None

    bundle_candidates = set()

    bundle_episode_id = bundle.get("trade_episode_id")

    if bundle_episode_id is not None:
        bundle_candidates.add(bundle_episode_id)

    messages = bundle.get("messages")

    if not isinstance(messages, list):
        messages = []

    for message in messages:

        if not isinstance(message, dict):
            continue

        roles = _message_roles(message)

        if not roles.intersection(SIGNAL_SOURCE_ROLES):
            continue

        episode_id = message.get("trade_episode_id")

        if episode_id is not None:
            bundle_candidates.add(episode_id)

    # Jika ID diberikan secara eksplisit,
    # ID bundle tidak boleh bertentangan dengannya.
    if explicit_episode_id is not None:

        if (
            bundle_candidates
            and explicit_episode_id not in bundle_candidates
        ):
            return None

        return explicit_episode_id

    # Tanpa ID eksplisit, harus ada tepat satu episode.
    if len(bundle_candidates) == 1:
        return next(iter(bundle_candidates))

    return None


def _resolve_skip_episode(
    bundle,
    explicit_episode_id=None,
):
    """
    Menentukan episode yang menjadi sasaran Skip.

    Tidak boleh memilih episode secara acak.
    """

    candidates = set()

    if isinstance(bundle, dict):

        messages = bundle.get("messages")

        if isinstance(messages, list):

            for message in messages:

                if not isinstance(message, dict):
                    continue

                if "SKIP" not in _message_roles(message):
                    continue

                episode_id = message.get("trade_episode_id")

                if episode_id is not None:
                    candidates.add(episode_id)

    # Jika ID eksplisit diberikan,
    # ID tersebut harus cocok dengan target Skip.
    if explicit_episode_id is not None:

        if candidates and explicit_episode_id not in candidates:
            return None

        return explicit_episode_id

    if len(candidates) == 1:
        return next(iter(candidates))

    return None


def _get_message_text(message):
    """
    Mengambil teks terkini dari pesan.

    Jika text tidak tersedia, gunakan new_text terakhir
    dari riwayat edit.
    """

    current_text = message.get("text")

    if isinstance(current_text, str) and current_text.strip():
        return current_text

    edits = message.get("edit_history")

    if not isinstance(edits, list):
        return ""

    for edit in reversed(edits):

        if not isinstance(edit, dict):
            continue

        new_text = edit.get("new_text")

        if isinstance(new_text, str) and new_text.strip():
            return new_text

    return ""


def _is_cisd_confirmed_text(text):
    """Memeriksa konfirmasi CISD secara eksplisit."""

    if not isinstance(text, str):
        return False

    if not re.search(
        r"\bcisd\b",
        text,
        re.IGNORECASE,
    ):
        return False

    # Penyangkalan lebih kuat daripada kata valid/confirmed.
    if CISD_NEGATIVE_PATTERN.search(text):
        return False

    return bool(
        CISD_CONFIRMED_PATTERN.search(text)
    )


def _has_entry_confirmation(
    bundle,
    target_episode_id,
):
    """
    Konfirmasi CISD wajib:
    1. Memiliki role ENTRY_CONFIRMATION.
    2. Memiliki trade_episode_id yang sama.
    3. Memiliki teks yang menyatakan CISD sudah valid.
    """

    if target_episode_id is None:
        return False

    if not isinstance(bundle, dict):
        return False

    messages = bundle.get("messages")

    if not isinstance(messages, list):
        return False

    for message in messages:

        if not isinstance(message, dict):
            continue

        if "ENTRY_CONFIRMATION" not in _message_roles(message):
            continue

        if message.get("trade_episode_id") != target_episode_id:
            continue

        text = _get_message_text(message)

        if _is_cisd_confirmed_text(text):
            return True

    return False


# ============================================================
# SIGNAL TEXT PARSING
# ============================================================

def _parse_entry_zone(zone_text):
    """Mengambil satu rentang harga dari teks zona."""

    if not isinstance(zone_text, str):
        raise ValueError("Format zona entry tidak valid.")

    if not zone_text.strip():
        raise ValueError("Zona entry kosong.")

    matches = list(
        ZONE_PATTERN.finditer(zone_text)
    )

    if len(matches) != 1:
        raise ValueError(
            "Zona entry tidak ditemukan atau memiliki beberapa rentang."
        )

    low = _to_decimal(matches[0].group(1))
    high = _to_decimal(matches[0].group(2))

    if low <= 0 or high <= 0:
        raise ValueError(
            "Harga zona harus lebih besar dari nol."
        )

    if low > high:
        raise ValueError(
            "Batas bawah zona lebih besar dari batas atas."
        )

    midpoint = (low + high) / Decimal("2")

    return {
        "low": low,
        "high": high,
        "midpoint": midpoint,
    }


def _extract_tp_targets(tp_texts):
    """
    Mengambil target TP yang secara eksplisit menggunakan pips.

    Contoh:
    - TP 100 pips
    - 60 pips
    - TP1 60 pips, TP2 100 pips

    Target berupa harga atau teks ambigu tidak ditebak.
    """

    if not isinstance(tp_texts, list):
        raise ValueError(
            "Take profit harus berupa daftar teks."
        )

    targets = []

    for text in tp_texts:

        if not isinstance(text, str):
            raise ValueError(
                "Format teks TP tidak valid."
            )

        matches = list(
            TP_PIPS_PATTERN.finditer(text)
        )

        if not matches:
            raise ValueError(
                f"Target TP ambigu atau tidak menggunakan pips: {text}"
            )

        for match in matches:

            target = _to_decimal(match.group(1))

            if target <= 0:
                raise ValueError(
                    "Target TP harus lebih besar dari nol."
                )

            targets.append(target)

    # Hapus duplikat sambil mempertahankan urutan.
    unique_targets = []

    for target in targets:

        if target not in unique_targets:
            unique_targets.append(target)

    return unique_targets


def _is_skip_instruction(action_instructions):
    """
    Mendeteksi instruksi Skip.

    Tidak menganggap 'jangan skip' sebagai instruksi Skip.
    """

    if not isinstance(action_instructions, list):
        return False

    for instruction in action_instructions:

        if not isinstance(instruction, str):
            continue

        text = instruction.strip().lower()

        if not re.search(r"\bskip\b", text):
            continue

        if re.search(
            r"\b(jangan|not|don't|dont|do not)\s+skip\b",
            text,
        ):
            continue

        return True

    return False


def _requires_cisd(
    conditions,
    explicit_entry_text,
):
    """
    Menentukan apakah sinyal mensyaratkan menunggu CISD.

    Jika terdapat instruksi menunggu CISD,
    syarat tetap berlaku.

    Jika CISD disebut tanpa konfirmasi eksplisit,
    perlakukan sebagai syarat menunggu.
    """

    texts = []

    if isinstance(conditions, list):
        texts.extend(
            item
            for item in conditions
            if isinstance(item, str)
        )

    if isinstance(explicit_entry_text, str):
        texts.append(explicit_entry_text)

    combined = " ".join(texts)

    if not re.search(
        r"\bcisd\b",
        combined,
        re.IGNORECASE,
    ):
        return False

    if CISD_WAIT_PATTERN.search(combined):
        return True

    if CISD_NEGATIVE_PATTERN.search(combined):
        return True

    if _is_cisd_confirmed_text(combined):
        return False

    return True


# ============================================================
# ENTRY TYPE CLASSIFICATION
# ============================================================

def _classify_entry_type(
    direction,
    current_price,
    low,
    high,
):
    """
    Mengklasifikasikan jenis entry.

    BUY:
    - Di dalam zona: MARKET
    - Harga di bawah zona: BUY STOP
    - Harga di atas zona: BUY LIMIT

    SELL:
    - Di dalam zona: MARKET
    - Harga di atas zona: SELL STOP
    - Harga di bawah zona: SELL LIMIT

    Ini klasifikasi awal, bukan validasi order broker.
    """

    if low <= current_price <= high:
        return "MARKET"

    if direction == "BUY":

        if current_price < low:
            return "BUY_STOP"

        return "BUY_LIMIT"

    if direction == "SELL":

        if current_price > high:
            return "SELL_STOP"

        return "SELL_LIMIT"

    raise ValueError(
        "Direction tidak valid."
    )


# ============================================================
# MAIN DECISION ENGINE
# ============================================================

def evaluate_signal(
    ai_result,
    *,
    execution_mode="APPROVAL",
    trading_mode="DRY_RUN",
    auto_execution_enabled=False,
    auto_paused=True,
    current_price=None,
    tick_size=None,
    spread_pips=None,
    manual_tp_pips=None,
    bundle=None,
    trade_episode_id=None,
):
    """
    Mengevaluasi hasil AI Router menjadi keputusan deterministik.

    Parameter:

    ai_result:
        Hasil lengkap ai_router.analyze_bundle().

    execution_mode:
        APPROVAL atau AUTO.

    trading_mode:
        DRY_RUN atau LIVE.

        Decision Engine saat ini hanya mengizinkan
        AUTO DRY_RUN.

    current_price:
        Harga Ask untuk BUY atau Bid untuk SELL.

    tick_size:
        Tick size dari spesifikasi simbol broker.

    manual_tp_pips:
        TP manual yang mengalahkan TP dari mentor.

    bundle:
        Bundle pesan aggregator untuk membaca role dan episode.

    trade_episode_id:
        ID episode eksplisit bila tersedia.

    Fungsi ini tidak pernah mengirim order MT5.
    """

    execution_mode = (
        execution_mode.strip().upper()
        if isinstance(execution_mode, str)
        else None
    )

    trading_mode = (
        trading_mode.strip().upper()
        if isinstance(trading_mode, str)
        else None
    )

    base = {
        "execution_mode": execution_mode,
        "trading_mode": trading_mode,
        "trade_episode_id": trade_episode_id,
    }

    # --------------------------------------------------------
    # 1. VALIDASI HASIL AI ROUTER
    # --------------------------------------------------------

    if not isinstance(ai_result, dict):

        return _result(
            "NEEDS_REVIEW",
            "AI_RESULT_INVALID",
            **base,
        )

    router_status = ai_result.get("status")

    if router_status == "NO_SIGNAL":

        return _result(
            "NO_SIGNAL",
            "AI_ROUTER_NO_SIGNAL",
            **base,
        )

    if router_status != "ANALYZED":

        return _result(
            "NEEDS_REVIEW",
            f"AI_ROUTER_STATUS_{router_status}",
            **base,
        )

    analysis = ai_result.get("final_analysis")

    if not isinstance(analysis, dict):

        return _result(
            "NEEDS_REVIEW",
            "FINAL_ANALYSIS_MISSING",
            **base,
        )

    analysis_status = analysis.get("status")

    if analysis_status == "NO_SIGNAL":

        return _result(
            "NO_SIGNAL",
            "FINAL_ANALYSIS_NO_SIGNAL",
            **base,
        )

    if analysis_status != "CLEAR":

        return _result(
            "NEEDS_REVIEW",
            f"ANALYSIS_STATUS_{analysis_status}",
            **base,
        )

    extracted = analysis.get("extracted")

    if not isinstance(extracted, dict):

        return _result(
            "NEEDS_REVIEW",
            "EXTRACTED_DATA_INVALID",
            **base,
        )

    # --------------------------------------------------------
    # 2. BACA ROLE DAN EPISODE PESAN
    # --------------------------------------------------------

    roles, _ = _get_message_context(bundle)

    signal_episode_id = _resolve_signal_episode_id(
        bundle,
        explicit_episode_id=trade_episode_id,
    )

    base["trade_episode_id"] = signal_episode_id

    has_entry_context = bool(
        roles.intersection({
            "NEW_SIGNAL",
            "ENTRY_REQUEST",
            "PRICE_ZONE",
        })
    )

    # REENTRY_PLAN saja bukan instruksi entry.
    if "REENTRY_PLAN" in roles and not has_entry_context:

        return _result(
            "NO_SIGNAL",
            "REENTRY_PLAN_ONLY",
            **base,
        )

    # --------------------------------------------------------
    # 3. VALIDASI SKIP
    # --------------------------------------------------------

    actions = extracted.get(
        "action_instructions",
        [],
    )

    skip_detected = _is_skip_instruction(actions)

    if "SKIP" in roles and not skip_detected:

        return _result(
            "NEEDS_REVIEW",
            "SKIP_METADATA_WITHOUT_EXPLICIT_AI_INSTRUCTION",
            **base,
        )

    if skip_detected:

        target_episode = _resolve_skip_episode(
            bundle,
            explicit_episode_id=signal_episode_id,
        )

        if target_episode is None:

            return _result(
                "NEEDS_REVIEW",
                "SKIP_TARGET_EPISODE_AMBIGUOUS",
                **base,
            )

        return _result(
            "SKIP",
            "MENTOR_SKIP_INSTRUCTION",
            trade_episode_id=target_episode,
            skip_scope="TARGET_EPISODE_ONLY",
            requires_mt5_reconciliation=True,
            **{
                key: value
                for key, value in base.items()
                if key != "trade_episode_id"
            },
        )

    # --------------------------------------------------------
    # 4. VALIDASI DIRECTION
    # --------------------------------------------------------

    direction = extracted.get("direction")

    if direction not in VALID_DIRECTIONS:

        return _result(
            "NEEDS_REVIEW",
            "DIRECTION_MISSING_OR_INVALID",
            **base,
        )

    # --------------------------------------------------------
    # 5. VALIDASI ZONA ENTRY
    # --------------------------------------------------------

    zone_text = extracted.get("entry_zone_text")

    if zone_text is None or (
        isinstance(zone_text, str)
        and not zone_text.strip()
    ):

        return _result(
            "WAITING_FOR_ZONE",
            "ENTRY_ZONE_NOT_PROVIDED",
            direction=direction,
            **base,
        )

    try:

        zone = _parse_entry_zone(zone_text)

    except ValueError as exc:

        return _result(
            "NEEDS_REVIEW",
            f"ENTRY_ZONE_INVALID: {exc}",
            direction=direction,
            **base,
        )

    low = zone["low"]
    high = zone["high"]
    midpoint = zone["midpoint"]

    # --------------------------------------------------------
    # 6. VALIDASI EXECUTION MODE
    # --------------------------------------------------------

    if execution_mode not in VALID_EXECUTION_MODES:

        return _result(
            "NEEDS_REVIEW",
            "EXECUTION_MODE_INVALID",
            direction=direction,
            **base,
        )


    # --------------------------------------------------------
    # 7. VALIDASI HARGA DAN SPREAD
    # --------------------------------------------------------

    # Harga harus valid sebelum CISD atau status AUTO dievaluasi.
    try:
        price = _to_decimal(current_price)
        tick = _to_decimal(tick_size)

        if not price.is_finite() or not tick.is_finite():
            raise ValueError("PRICE_DATA_NON_FINITE")

        if price <= 0:
            raise ValueError(
                "Current price harus lebih besar dari nol."
            )

        if tick <= 0:
            raise ValueError(
                "Tick size harus lebih besar dari nol."
            )

    except (
        ValueError,
        InvalidOperation,
        TypeError,
        ArithmeticError,
    ) as exc:
        return _result(
            "NEEDS_REVIEW",
            f"PRICE_DATA_INVALID: {exc}",
            direction=direction,
            **base,
        )

    # PRIORITAS PERTAMA:
    # Harga lebih dari 30 pips dari midpoint berarti SKIP.
    distance = abs(price - midpoint)

    spread_value = None

    if distance > MAX_ENTRY_DISTANCE:
        return _result(
            "SKIP",
            "ENTRY_PRICE_TOO_FAR_FROM_MIDPOINT",
            direction=direction,
            entry_zone={
                "low": _format_decimal(low),
                "high": _format_decimal(high),
                "midpoint": _format_decimal(midpoint),
            },
            current_price=_format_decimal(price),
            distance_from_midpoint=_format_decimal(distance),
            maximum_distance=_format_decimal(MAX_ENTRY_DISTANCE),
            spread_pips=None,
            maximum_spread_pips=_format_decimal(FIXED_SL_PIPS),
            skip_scope="NEW_ENTRY_ONLY",
            **base,
        )

    # PRIORITAS KEDUA:
    # Spread dibandingkan dengan SL yang dipakai Decision Engine.
    # None hanya dipakai oleh pemanggilan engine langsung/legacy.
    if spread_pips is not None:
        try:
            spread_value = _to_decimal(spread_pips)

            if not spread_value.is_finite():
                raise ValueError("SPREAD_NON_FINITE")

            if spread_value < 0:
                raise ValueError("SPREAD_NEGATIVE")

        except (
            ValueError,
            InvalidOperation,
            TypeError,
            ArithmeticError,
        ) as exc:
            return _result(
                "NEEDS_REVIEW",
                f"SPREAD_DATA_INVALID: {exc}",
                direction=direction,
                current_price=_format_decimal(price),
                **base,
            )

        if spread_value > FIXED_SL_PIPS:
            return _result(
                "WAITING_FOR_SPREAD",
                "SPREAD_EXCEEDS_STOP_LOSS",
                direction=direction,
                entry_zone={
                    "low": _format_decimal(low),
                    "high": _format_decimal(high),
                    "midpoint": _format_decimal(midpoint),
                },
                current_price=_format_decimal(price),
                distance_from_midpoint=_format_decimal(distance),
                spread_pips=_format_decimal(spread_value),
                maximum_spread_pips=_format_decimal(FIXED_SL_PIPS),
                stop_loss_pips=_format_decimal(FIXED_SL_PIPS),
                requires_cisd=_requires_cisd(
                    extracted.get("conditions", []),
                    extracted.get("explicit_entry_text"),
                ),
                cisd_confirmed=_has_entry_confirmation(
                    bundle,
                    signal_episode_id,
                ),
                **base,
            )

    # --------------------------------------------------------
    # 8. VALIDASI CISD
    # --------------------------------------------------------

    conditions = extracted.get(
        "conditions",
        [],
    )

    requires_cisd = _requires_cisd(
        conditions,
        extracted.get("explicit_entry_text"),
    )

    cisd_confirmed = _has_entry_confirmation(
        bundle,
        signal_episode_id,
    )

    if (
        execution_mode == "APPROVAL"
        and requires_cisd
        and not cisd_confirmed
    ):

        return _result(
            "WAITING_FOR_CISD",
            "WAITING_FOR_CISD_CONFIRMATION",
            direction=direction,
            entry_zone={
                "low": _format_decimal(low),
                "high": _format_decimal(high),
                "midpoint": _format_decimal(midpoint),
            },
            requires_cisd=True,
            cisd_confirmed=False,
            **base,
        )

    # AUTO mengabaikan syarat menunggu CISD.
    # Pemeriksaan zona dan harga tetap wajib.

    # --------------------------------------------------------
    # 9. VALIDASI AUTO
    # --------------------------------------------------------

    if execution_mode == "AUTO":

        if (
            auto_execution_enabled is not True
            or auto_paused is not False
        ):

            return _result(
                "AUTO_PAUSED",
                "AUTO_DISABLED_OR_PAUSED",
                direction=direction,
                requires_cisd=requires_cisd,
                cisd_confirmed=cisd_confirmed,
                **base,
            )

        if trading_mode != "DRY_RUN":

            return _result(
                "NEEDS_REVIEW",
                "AUTO_LIVE_NOT_SUPPORTED_BY_DECISION_ENGINE",
                direction=direction,
                **base,
            )

    # --------------------------------------------------------
    # 10. VALIDASI TAKE PROFIT
    # --------------------------------------------------------

    try:

        if manual_tp_pips is not None:

            manual_tp = _to_decimal(manual_tp_pips)

            if manual_tp <= 0:

                raise ValueError(
                    "Manual TP harus lebih besar dari nol."
                )

            tp_targets = [manual_tp]
            tp_source = "MANUAL"

        else:

            tp_targets = _extract_tp_targets(
                extracted.get(
                    "take_profit_texts",
                    [],
                )
            )

            tp_source = (
                "MENTOR"
                if tp_targets
                else "NONE"
            )

    except ValueError as exc:

        return _result(
            "NEEDS_REVIEW",
            f"TAKE_PROFIT_INVALID: {exc}",
            direction=direction,
            **base,
        )

    # --------------------------------------------------------
    # 11. TENTUKAN JENIS ENTRY
    # --------------------------------------------------------

    try:

        entry_type = _classify_entry_type(
            direction,
            price,
            low,
            high,
        )

    except ValueError as exc:

        return _result(
            "NEEDS_REVIEW",
            f"ENTRY_CLASSIFICATION_FAILED: {exc}",
            direction=direction,
            **base,
        )

    # MARKET menggunakan harga saat ini sebagai referensi fill.
    # Pending order menggunakan midpoint sebagai harga target.
    entry_reference = (
        price
        if entry_type == "MARKET"
        else midpoint
    )

    # --------------------------------------------------------
    # 12. HITUNG SL DAN TP BERDASARKAN ENTRY REFERENCE
    # --------------------------------------------------------

    try:

        sl_price = calculate_price_from_pips(
            entry_reference,
            direction,
            FIXED_SL_PIPS,
            tick,
            level="SL",
        )

        tp_prices = []

        for target in tp_targets:

            target_price = calculate_price_from_pips(
                entry_reference,
                direction,
                target,
                tick,
                level="TP",
            )

            tp_prices.append(target_price)

    except (
        ValueError,
        InvalidOperation,
        TypeError,
        ArithmeticError,
    ) as exc:

        return _result(
            "NEEDS_REVIEW",
            f"PRICE_CALCULATION_FAILED: {exc}",
            direction=direction,
            **base,
        )

    # --------------------------------------------------------
    # 13. HASIL AKHIR
    # --------------------------------------------------------

    final_status = (
        "READY_FOR_DRY_RUN"
        if execution_mode == "AUTO"
        else "READY_FOR_APPROVAL"
    )

    return _result(
        final_status,
        "SIGNAL_VALIDATED",

        direction=direction,

        entry_zone={
            "low": _format_decimal(low),
            "high": _format_decimal(high),
            "midpoint": _format_decimal(midpoint),
        },

        current_price=_format_decimal(price),
        spread_pips=(
            _format_decimal(spread_value)
            if spread_value is not None else None
        ),
        maximum_spread_pips=_format_decimal(FIXED_SL_PIPS),

        entry_price_reference=_format_decimal(
            entry_reference
        ),

        entry_type=entry_type,

        stop_loss_pips=_format_decimal(
            FIXED_SL_PIPS
        ),

        stop_loss_price_reference=_format_decimal(
            sl_price
        ),

        take_profit_targets_pips=[
            _format_decimal(item)
            for item in tp_targets
        ],

        take_profit_prices_reference=[
            _format_decimal(item)
            for item in tp_prices
        ],

        take_profit_source=tp_source,

        requires_cisd=requires_cisd,
        cisd_confirmed=cisd_confirmed,

        **base,
    )
