from pathlib import Path
from decimal import Decimal, InvalidOperation
import threading
import time

import MetaTrader5 as mt5

from app.execution.price_math import PIP_PRICE


# HFM Market Watch clock state. The monotonic clock is used only to detect
# how long the latest Market Watch quote has stopped advancing; it does not
# interpret broker timestamps as UTC.
_MARKET_CLOCK_LOCK = threading.Lock()
_MARKET_CLOCK_LAST_REFERENCE_MSC = None
_MARKET_CLOCK_LAST_ADVANCE_MONOTONIC = None
_MARKET_CLOCK_CONFIRMED = False


def reset_market_clock_state():
    """Reset in-memory Market Watch clock tracking (mainly for tests)."""
    global _MARKET_CLOCK_LAST_REFERENCE_MSC
    global _MARKET_CLOCK_LAST_ADVANCE_MONOTONIC
    global _MARKET_CLOCK_CONFIRMED

    with _MARKET_CLOCK_LOCK:
        _MARKET_CLOCK_LAST_REFERENCE_MSC = None
        _MARKET_CLOCK_LAST_ADVANCE_MONOTONIC = None
        _MARKET_CLOCK_CONFIRMED = False


class MT5ConnectorError(RuntimeError):
    pass


def connect_readonly(
    terminal_path,
    expected_server=None,
    require_demo=True,
    timeout_ms=60000,
):
    """
    Menghubungkan Python ke terminal MT5 tertentu.

    Hanya membaca informasi terminal dan akun.
    Tidak mengirim order.
    """

    path = Path(terminal_path)

    if not path.is_file():
        raise FileNotFoundError(
            f"Terminal MT5 tidak ditemukan: {path}"
        )

    initialized = mt5.initialize(
        path=str(path),
        timeout=timeout_ms,
    )

    if not initialized:
        raise MT5ConnectorError(
            f"MT5 initialize gagal: {mt5.last_error()}"
        )

    try:
        terminal = mt5.terminal_info()
        account = mt5.account_info()

        if terminal is None or account is None:
            raise MT5ConnectorError(
                f"Informasi MT5 tidak tersedia: {mt5.last_error()}"
            )

        if not terminal.connected:
            raise MT5ConnectorError(
                "Terminal MT5 belum terhubung ke server broker."
            )

        if (
            expected_server
            and account.server != expected_server
        ):
            raise MT5ConnectorError(
                "Server tidak sesuai. "
                f"Diharapkan {expected_server}, "
                f"terhubung ke {account.server}."
            )

        if (
            require_demo
            and account.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO
        ):
            raise MT5ConnectorError(
                "Koneksi ditolak: akun bukan akun DEMO."
            )

        if account.trade_mode == mt5.ACCOUNT_TRADE_MODE_DEMO:
            account_mode = "DEMO"
        elif account.trade_mode == mt5.ACCOUNT_TRADE_MODE_CONTEST:
            account_mode = "CONTEST"
        elif account.trade_mode == mt5.ACCOUNT_TRADE_MODE_REAL:
            account_mode = "REAL"
        else:
            account_mode = "UNKNOWN"

        return {
            "connected": True,
            "terminal": {
                "name": terminal.name,
                "company": terminal.company,
                "path": terminal.path,
                "connected": bool(terminal.connected),
                "trade_allowed": bool(terminal.trade_allowed),
                "tradeapi_disabled": bool(
                    terminal.tradeapi_disabled
                ),
            },
            "account": {
                "mode": account_mode,
                "server": account.server,
                "company": account.company,
                "currency": account.currency,
                "leverage": int(account.leverage),
                "trade_allowed": bool(account.trade_allowed),
                "trade_expert": bool(account.trade_expert),
            },
            "readonly": True,
        }

    except Exception:
        mt5.shutdown()
        raise


def get_symbol_spec(symbol):
    """
    Membaca spesifikasi simbol dan harga terkini.

    Pip menggunakan standar internal SIREADER:
    1 pip = 0.10 perubahan harga.
    """

    symbol = str(symbol).strip()

    if not symbol:
        raise ValueError("Nama simbol wajib diisi.")

    info = mt5.symbol_info(symbol)

    if info is None:
        raise MT5ConnectorError(
            f"Simbol {symbol} tidak ditemukan: {mt5.last_error()}"
        )

    try:
        point = Decimal(str(info.point))
        tick_size = Decimal(str(info.trade_tick_size))
        pip_in_points = PIP_PRICE / point

    except (InvalidOperation, ZeroDivisionError):
        raise MT5ConnectorError(
            f"Spesifikasi harga {symbol} tidak valid."
        )

    if point <= 0 or tick_size <= 0:
        raise MT5ConnectorError(
            f"Point atau tick size {symbol} tidak valid."
        )

    tick = mt5.symbol_info_tick(symbol)

    bid = None
    ask = None
    tick_time = None

    if tick is not None:
        bid = Decimal(str(tick.bid))
        ask = Decimal(str(tick.ask))
        tick_time = tick.time

    return {
        "symbol": info.name,
        "visible": bool(info.visible),
        "digits": int(info.digits),
        "point": str(point),
        "pip_price": str(PIP_PRICE),
        "points_per_pip": str(pip_in_points),
        "tick_size": str(tick_size),
        "tick_value": str(info.trade_tick_value),
        "contract_size": str(info.trade_contract_size),
        "volume_min": str(info.volume_min),
        "volume_max": str(info.volume_max),
        "volume_step": str(info.volume_step),
        "stops_level_points": int(info.trade_stops_level),
        "freeze_level_points": int(info.trade_freeze_level),
        "filling_mode": int(info.filling_mode),
        "trade_mode": int(info.trade_mode),
        "bid": str(bid) if bid is not None else None,
        "ask": str(ask) if ask is not None else None,
        "tick_time": tick_time,
    }


def shutdown():
    """Menutup koneksi Python ke MT5."""
    mt5.shutdown()


def get_market_clock_snapshot(symbol):
    """Return one symbol quote plus the latest visible Market Watch tick.

    The returned tick timestamps are compared with each other in the same
    broker-server time domain. They are deliberately NOT compared directly
    with datetime.now(timezone.utc), because the observed HFM server timestamp
    may have a broker timezone offset relative to the device clock.

    `reference_clock_confirmed` becomes True only after the latest Market
    Watch timestamp has advanced at least once since this Python process
    started. `reference_silence_seconds` is measured with time.monotonic().
    """
    global _MARKET_CLOCK_LAST_REFERENCE_MSC
    global _MARKET_CLOCK_LAST_ADVANCE_MONOTONIC
    global _MARKET_CLOCK_CONFIRMED

    symbol = str(symbol).strip()
    if not symbol:
        raise ValueError("Nama simbol wajib diisi.")

    # Keep the existing contract/spec validation and refresh the target tick.
    spec = get_symbol_spec(symbol)
    target_tick = mt5.symbol_info_tick(symbol)
    if target_tick is None:
        raise MT5ConnectorError(
            f"Tick {symbol} tidak tersedia: {mt5.last_error()}"
        )

    target_time_msc = getattr(target_tick, "time_msc", None)
    if target_time_msc is None or int(target_time_msc) <= 0:
        raise MT5ConnectorError(
            f"Timestamp milidetik {symbol} tidak tersedia."
        )
    target_time_msc = int(target_time_msc)

    target_bid = Decimal(str(target_tick.bid))
    target_ask = Decimal(str(target_tick.ask))
    if (
        not target_bid.is_finite()
        or not target_ask.is_finite()
        or target_bid <= 0
        or target_ask <= 0
        or target_ask < target_bid
    ):
        raise MT5ConnectorError(f"Bid/Ask {symbol} tidak valid.")

    symbols = mt5.symbols_get()
    if symbols is None:
        raise MT5ConnectorError(
            f"Market Watch symbols tidak tersedia: {mt5.last_error()}"
        )

    # Reference wajib berasal dari instrumen lain.
    # XAUUSD tidak boleh menjadi referensi untuk dirinya sendiri.
    candidates = []
    target_symbol_key = symbol.casefold()

    for info in symbols:
        name = getattr(info, "name", None)
        if (
            not name
            or str(name).strip().casefold() == target_symbol_key
        ):
            continue
        if not bool(getattr(info, "visible", False)):
            continue

        reference_tick = mt5.symbol_info_tick(str(name))
        if reference_tick is None:
            continue
        reference_msc = getattr(reference_tick, "time_msc", None)
        if reference_msc is None:
            continue
        try:
            reference_msc = int(reference_msc)
        except (TypeError, ValueError, OverflowError):
            continue
        if reference_msc > 0:
            candidates.append((reference_msc, str(name)))

    if not candidates:
        raise MT5ConnectorError(
            "Tidak ada simbol Market Watch lain dengan tick valid "
            f"untuk menjadi referensi clock {symbol}."
        )

    reference_time_msc, reference_symbol = max(
        candidates,
        key=lambda item: item[0],
    )

    monotonic_now = time.monotonic()
    with _MARKET_CLOCK_LOCK:
        if _MARKET_CLOCK_LAST_REFERENCE_MSC is None:
            _MARKET_CLOCK_LAST_REFERENCE_MSC = reference_time_msc
            _MARKET_CLOCK_LAST_ADVANCE_MONOTONIC = monotonic_now
            _MARKET_CLOCK_CONFIRMED = False
            clock_reason = "INITIAL_BASELINE_WAITING_FOR_ADVANCE"
        elif reference_time_msc > _MARKET_CLOCK_LAST_REFERENCE_MSC:
            _MARKET_CLOCK_LAST_REFERENCE_MSC = reference_time_msc
            _MARKET_CLOCK_LAST_ADVANCE_MONOTONIC = monotonic_now
            _MARKET_CLOCK_CONFIRMED = True
            clock_reason = "MARKET_WATCH_CLOCK_ADVANCED"
        elif reference_time_msc < _MARKET_CLOCK_LAST_REFERENCE_MSC:
            # A server timestamp rollback/reconnect is a new baseline. Require
            # a subsequent advance before treating it as a trusted clock.
            _MARKET_CLOCK_LAST_REFERENCE_MSC = reference_time_msc
            _MARKET_CLOCK_LAST_ADVANCE_MONOTONIC = monotonic_now
            _MARKET_CLOCK_CONFIRMED = False
            clock_reason = "MARKET_WATCH_CLOCK_ROLLBACK"
        else:
            clock_reason = "MARKET_WATCH_CLOCK_STABLE"

        silence_seconds = max(
            0.0,
            monotonic_now - _MARKET_CLOCK_LAST_ADVANCE_MONOTONIC,
        )
        confirmed = _MARKET_CLOCK_CONFIRMED

    snapshot = dict(spec)
    snapshot.update({
        "symbol": str(getattr(target_tick, "symbol", None) or spec.get("symbol") or symbol),
        "bid": str(target_bid),
        "ask": str(target_ask),
        "tick_time": int(getattr(target_tick, "time", target_time_msc // 1000)),
        "tick_time_msc": target_time_msc,
        "reference_time_msc": int(reference_time_msc),
        "reference_symbol": reference_symbol,
        "reference_clock_confirmed": bool(confirmed),
        "reference_clock_reason": clock_reason,
        "reference_silence_seconds": round(silence_seconds, 3),
        "clock_source": "HFM_MARKET_WATCH_LATEST_VISIBLE_TICK",
    })
    return snapshot

