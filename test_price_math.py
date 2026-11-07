from decimal import Decimal
import pytest

from app.execution.price_math import (
    pips_to_price_distance,
    calculate_tp_price,
    calculate_sl_price,
)


def test_10_pips_sama_dengan_1_harga():
    assert pips_to_price_distance(10) == Decimal("1.00")


def test_100_pips_sama_dengan_10_harga():
    assert pips_to_price_distance(100) == Decimal("10.00")


def test_hfm_buy_10_pips():
    result = calculate_tp_price(3935.30, "BUY", 10, 0.01)
    assert result == Decimal("3936.30")


def test_hfm_sell_10_pips():
    result = calculate_tp_price(3935.30, "SELL", 10, 0.01)
    assert result == Decimal("3934.30")


def test_hfm_buy_50_pips():
    result = calculate_tp_price(3935.30, "BUY", 50, 0.01)
    assert result == Decimal("3940.30")


def test_oanda_buy_10_pips():
    result = calculate_tp_price(3900.000, "BUY", 10, 0.001)
    assert result == Decimal("3901.000")


def test_oanda_sell_10_pips():
    result = calculate_tp_price(3900.000, "SELL", 10, 0.001)
    assert result == Decimal("3899.000")


def test_sl_buy_10_pips():
    result = calculate_sl_price(3935.30, "BUY", 10, 0.01)
    assert result == Decimal("3934.30")


def test_sl_sell_10_pips():
    result = calculate_sl_price(3935.30, "SELL", 10, 0.01)
    assert result == Decimal("3936.30")


def test_tp_rounding_menjauhi_entry():
    result = calculate_tp_price(100.00, "BUY", 10.1, 0.25)
    assert result == Decimal("101.25")


def test_direction_invalid_ditolak():
    with pytest.raises(ValueError):
        calculate_tp_price(3935.30, "HOLD", 10, 0.01)


def test_pips_nol_ditolak():
    with pytest.raises(ValueError):
        calculate_tp_price(3935.30, "BUY", 0, 0.01)


def test_tick_size_nol_ditolak():
    with pytest.raises(ValueError):
        calculate_tp_price(3935.30, "BUY", 10, 0)
