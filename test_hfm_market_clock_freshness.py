from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.execution import mt5_connector
from app.signals.decision_service import _select_fresh_price


BASE_TICK_MSC = 1_793_959_398_627
BASE_REFERENCE_MSC = BASE_TICK_MSC + 302


def relative_market_data(
    *,
    lag_ms=302,
    confirmed=True,
    silence=0.2,
):
    tick_msc = BASE_TICK_MSC
    reference_msc = tick_msc + lag_ms
    return {
        "ok": True,
        "symbol": "XAUUSD",
        "bid": "4051.64",
        "ask": "4051.98",
        "tick_size": "0.01",
        # This raw broker timestamp is intentionally about 2h ahead of the
        # sample device UTC when both are formatted as UTC.
        "tick_time": tick_msc // 1000,
        "tick_time_msc": tick_msc,
        "reference_time_msc": reference_msc,
        "reference_symbol": "USA100",
        "reference_clock_confirmed": confirmed,
        "reference_clock_reason": "MARKET_WATCH_CLOCK_ADVANCED",
        "reference_silence_seconds": silence,
        "account_mode": "DEMO",
        "readonly": True,
    }


def test_freshness_memakai_lag_market_watch_bukan_device_utc():
    now = datetime(2026, 11, 6, 8, 3, 34, 488738, tzinfo=timezone.utc)

    price, tick_size, metadata = _select_fresh_price(
        "BUY",
        relative_market_data(),
        now=now,
    )

    assert price == "4051.98"
    assert tick_size == "0.01"
    assert metadata["available"] is True
    assert metadata["tick_age_seconds"] == pytest.approx(0.302)
    assert metadata["tick_age_basis"] == "HFM_MARKET_WATCH"
    assert metadata["reference_symbol"] == "USA100"


def test_freshness_menolak_lag_xauusd_di_atas_15_detik():
    price, _, metadata = _select_fresh_price(
        "BUY",
        relative_market_data(lag_ms=15_001),
    )

    assert price is None
    assert metadata["available"] is False
    assert metadata["reason"] == "MT5_TICK_STALE"


def test_freshness_menolak_clock_baseline_yang_belum_terkonfirmasi():
    price, _, metadata = _select_fresh_price(
        "BUY",
        relative_market_data(confirmed=False),
    )

    assert price is None
    assert metadata["available"] is False
    assert metadata["reason"] == "MT5_REFERENCE_CLOCK_NOT_CONFIRMED"


def test_freshness_menolak_market_watch_yang_tidak_menerima_tick_baru():
    price, _, metadata = _select_fresh_price(
        "BUY",
        relative_market_data(silence=15.1),
    )

    assert price is None
    assert metadata["available"] is False
    assert metadata["reason"] == "MT5_REFERENCE_FEED_STALE"


def _fake_tick(time_msc, bid="1.0", ask="1.1"):
    return SimpleNamespace(
        time=time_msc // 1000,
        time_msc=time_msc,
        bid=float(bid),
        ask=float(ask),
        symbol="XAUUSD",
    )


def test_connector_memilih_tick_terbaru_dari_visible_market_watch(monkeypatch):
    mt5_connector.reset_market_clock_state()
    ticks = {
        "XAUUSD": _fake_tick(1_000),
        "USA100": _fake_tick(1_302),
        "EURUSD": _fake_tick(1_101),
    }
    monkeypatch.setattr(
        mt5_connector,
        "get_symbol_spec",
        lambda symbol: {
            "symbol": symbol,
            "tick_size": "0.01",
            "bid": "1.0",
            "ask": "1.1",
        },
    )
    monkeypatch.setattr(
        mt5_connector.mt5,
        "symbol_info_tick",
        lambda symbol: ticks.get(symbol),
    )
    monkeypatch.setattr(
        mt5_connector.mt5,
        "symbols_get",
        lambda: [
            SimpleNamespace(name="XAUUSD", visible=True),
            SimpleNamespace(name="USA100", visible=True),
            SimpleNamespace(name="EURUSD", visible=False),
        ],
    )
    monkeypatch.setattr(mt5_connector.time, "monotonic", lambda: 10.0)

    snapshot = mt5_connector.get_market_clock_snapshot("XAUUSD")

    assert snapshot["reference_time_msc"] == 1_302
    assert snapshot["reference_symbol"] == "USA100"
    assert snapshot["tick_time_msc"] == 1_000
    assert snapshot["reference_clock_confirmed"] is False
    assert snapshot["reference_silence_seconds"] == 0.0


def test_connector_mengonfirmasi_clock_setelah_reference_berjalan(monkeypatch):
    mt5_connector.reset_market_clock_state()
    ticks = {
        "XAUUSD": _fake_tick(1_000),
        "USA100": _fake_tick(1_100),
    }
    monkeypatch.setattr(
        mt5_connector,
        "get_symbol_spec",
        lambda symbol: {
            "symbol": symbol,
            "tick_size": "0.01",
            "bid": "1.0",
            "ask": "1.1",
        },
    )
    monkeypatch.setattr(
        mt5_connector.mt5,
        "symbol_info_tick",
        lambda symbol: ticks.get(symbol),
    )
    monkeypatch.setattr(
        mt5_connector.mt5,
        "symbols_get",
        lambda: [
            SimpleNamespace(name="XAUUSD", visible=True),
            SimpleNamespace(name="USA100", visible=True),
        ],
    )
    monotonic_values = iter([10.0, 11.0])
    monkeypatch.setattr(
        mt5_connector.time,
        "monotonic",
        lambda: next(monotonic_values),
    )

    first = mt5_connector.get_market_clock_snapshot("XAUUSD")
    ticks["USA100"] = _fake_tick(1_200)
    second = mt5_connector.get_market_clock_snapshot("XAUUSD")

    assert first["reference_clock_confirmed"] is False
    assert second["reference_clock_confirmed"] is True
    assert second["reference_time_msc"] == 1_200
    assert second["reference_silence_seconds"] == 0.0


def test_connector_menghitung_keheningan_feed_dengan_monotonic(monkeypatch):
    mt5_connector.reset_market_clock_state()
    ticks = {
        "XAUUSD": _fake_tick(1_000),
        "USA100": _fake_tick(1_100),
    }
    monkeypatch.setattr(
        mt5_connector,
        "get_symbol_spec",
        lambda symbol: {
            "symbol": symbol,
            "tick_size": "0.01",
            "bid": "1.0",
            "ask": "1.1",
        },
    )
    monkeypatch.setattr(
        mt5_connector.mt5,
        "symbol_info_tick",
        lambda symbol: ticks.get(symbol),
    )
    monkeypatch.setattr(
        mt5_connector.mt5,
        "symbols_get",
        lambda: [
            SimpleNamespace(name="XAUUSD", visible=True),
            SimpleNamespace(name="USA100", visible=True),
        ],
    )
    monotonic_values = iter([10.0, 11.0, 28.0])
    monkeypatch.setattr(
        mt5_connector.time,
        "monotonic",
        lambda: next(monotonic_values),
    )

    mt5_connector.get_market_clock_snapshot("XAUUSD")
    ticks["USA100"] = _fake_tick(1_200)
    confirmed = mt5_connector.get_market_clock_snapshot("XAUUSD")
    stale = mt5_connector.get_market_clock_snapshot("XAUUSD")

    assert confirmed["reference_clock_confirmed"] is True
    assert stale["reference_clock_confirmed"] is True
    assert stale["reference_silence_seconds"] == 17.0


def test_connector_reset_clock_menghapus_baseline():
    mt5_connector.reset_market_clock_state()
    assert mt5_connector._MARKET_CLOCK_LAST_REFERENCE_MSC is None
    assert mt5_connector._MARKET_CLOCK_CONFIRMED is False


def test_connector_menolak_jika_tidak_ada_simbol_referensi_lain(
    monkeypatch,
):
    mt5_connector.reset_market_clock_state()

    ticks = {
        "XAUUSD": _fake_tick(1_000),
    }

    monkeypatch.setattr(
        mt5_connector,
        "get_symbol_spec",
        lambda symbol: {
            "symbol": symbol,
            "tick_size": "0.01",
            "bid": "1.0",
            "ask": "1.1",
        },
    )
    monkeypatch.setattr(
        mt5_connector.mt5,
        "symbol_info_tick",
        lambda symbol: ticks.get(symbol),
    )
    monkeypatch.setattr(
        mt5_connector.mt5,
        "symbols_get",
        lambda: [
            SimpleNamespace(name="XAUUSD", visible=True),
            SimpleNamespace(name="EURUSD", visible=False),
        ],
    )

    with pytest.raises(
        mt5_connector.MT5ConnectorError,
        match="Tidak ada simbol Market Watch lain",
    ):
        mt5_connector.get_market_clock_snapshot("XAUUSD")

    # Kegagalan mencari reference tidak boleh mengonfirmasi clock.
    assert mt5_connector._MARKET_CLOCK_LAST_REFERENCE_MSC is None
    assert mt5_connector._MARKET_CLOCK_CONFIRMED is False


def test_freshness_menolak_xauusd_sebagai_referensi_dirinya_sendiri():
    market_data = relative_market_data()
    market_data["reference_symbol"] = "XAUUSD"

    price, _, metadata = _select_fresh_price(
        "BUY",
        market_data,
    )

    assert price is None
    assert metadata["available"] is False
    assert metadata["reason"] == (
        "MT5_REFERENCE_CLOCK_SELF_REFERENCE"
    )

