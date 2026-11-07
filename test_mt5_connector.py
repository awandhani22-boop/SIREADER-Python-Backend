from types import SimpleNamespace
from decimal import Decimal

import MetaTrader5 as mt5
import pytest

from app.execution import mt5_connector as connector


@pytest.fixture
def fake_terminal(tmp_path, monkeypatch):
    path = tmp_path / "terminal64.exe"
    path.write_text("test")

    terminal = SimpleNamespace(
        name="HFM Metatrader 5",
        company="HF Markets (SV) Ltd.",
        path=str(tmp_path),
        connected=True,
        trade_allowed=False,
        tradeapi_disabled=False,
    )

    account = SimpleNamespace(
        trade_mode=mt5.ACCOUNT_TRADE_MODE_DEMO,
        server="HFMarketsGlobal-Demo4",
        company="HF Markets (SV) Ltd.",
        currency="USD",
        leverage=1000,
        trade_allowed=False,
        trade_expert=True,
    )

    monkeypatch.setattr(
        connector.mt5,
        "initialize",
        lambda **kwargs: True,
    )

    monkeypatch.setattr(
        connector.mt5,
        "terminal_info",
        lambda: terminal,
    )

    monkeypatch.setattr(
        connector.mt5,
        "account_info",
        lambda: account,
    )

    monkeypatch.setattr(
        connector.mt5,
        "shutdown",
        lambda: None,
    )

    return path, terminal, account


def test_koneksi_demo_berhasil(fake_terminal):
    path, _, _ = fake_terminal

    result = connector.connect_readonly(
        path,
        expected_server="HFMarketsGlobal-Demo4",
    )

    assert result["connected"] is True
    assert result["account"]["mode"] == "DEMO"
    assert result["readonly"] is True


def test_server_yang_tidak_sesuai_ditolak(fake_terminal):
    path, _, _ = fake_terminal

    with pytest.raises(connector.MT5ConnectorError):
        connector.connect_readonly(
            path,
            expected_server="SERVER-SALAH",
        )


def test_akun_live_ditolak(fake_terminal):
    path, _, account = fake_terminal

    account.trade_mode = mt5.ACCOUNT_TRADE_MODE_REAL

    with pytest.raises(
        connector.MT5ConnectorError,
        match="bukan akun DEMO",
    ):
        connector.connect_readonly(path)


def test_terminal_terputus_ditolak(fake_terminal):
    path, terminal, _ = fake_terminal

    terminal.connected = False

    with pytest.raises(
        connector.MT5ConnectorError,
        match="belum terhubung",
    ):
        connector.connect_readonly(path)


def test_terminal_tidak_ditemukan():
    with pytest.raises(FileNotFoundError):
        connector.connect_readonly(
            r"C:\folder_tidak_ada\terminal64.exe"
        )


def test_spesifikasi_hfm_xauusd(monkeypatch):
    info = SimpleNamespace(
        name="XAUUSD",
        visible=True,
        digits=2,
        point=0.01,
        trade_tick_size=0.01,
        trade_tick_value=1.0,
        trade_contract_size=100.0,
        volume_min=0.01,
        volume_max=100.0,
        volume_step=0.01,
        trade_stops_level=0,
        trade_freeze_level=8,
        filling_mode=1,
        trade_mode=4,
    )

    tick = SimpleNamespace(
        bid=3938.70,
        ask=3938.90,
        time=123456789,
    )

    monkeypatch.setattr(
        connector.mt5,
        "symbol_info",
        lambda symbol: info,
    )

    monkeypatch.setattr(
        connector.mt5,
        "symbol_info_tick",
        lambda symbol: tick,
    )

    result = connector.get_symbol_spec("XAUUSD")

    assert result["symbol"] == "XAUUSD"
    assert result["pip_price"] == "0.10"
    assert Decimal(result["points_per_pip"]) == Decimal("10")
    assert result["tick_size"] == "0.01"
    assert result["bid"] == "3938.7"
    assert result["ask"] == "3938.9"


def test_point_nol_ditolak(monkeypatch):
    info = SimpleNamespace(
        name="XAUUSD",
        point=0,
        trade_tick_size=0.01,
    )

    monkeypatch.setattr(
        connector.mt5,
        "symbol_info",
        lambda symbol: info,
    )

    with pytest.raises(connector.MT5ConnectorError):
        connector.get_symbol_spec("XAUUSD")


def test_simbol_tidak_ditemukan(monkeypatch):
    monkeypatch.setattr(
        connector.mt5,
        "symbol_info",
        lambda symbol: None,
    )

    with pytest.raises(connector.MT5ConnectorError):
        connector.get_symbol_spec("SIMBOL_TIDAK_ADA")


def test_pip_tidak_berubah_oleh_digit_broker(monkeypatch):
    info = SimpleNamespace(
        name="XAUUSD",
        visible=True,
        digits=3,
        point=0.001,
        trade_tick_size=0.001,
        trade_tick_value=1.0,
        trade_contract_size=100.0,
        volume_min=0.01,
        volume_max=100.0,
        volume_step=0.01,
        trade_stops_level=0,
        trade_freeze_level=0,
        filling_mode=1,
        trade_mode=4,
    )

    monkeypatch.setattr(
        connector.mt5,
        "symbol_info",
        lambda symbol: info,
    )

    monkeypatch.setattr(
        connector.mt5,
        "symbol_info_tick",
        lambda symbol: None,
    )

    result = connector.get_symbol_spec("XAUUSD")

    assert result["pip_price"] == "0.10"
    assert Decimal(result["points_per_pip"]) == Decimal("100")
