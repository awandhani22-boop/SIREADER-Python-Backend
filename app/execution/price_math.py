from decimal import (
    Decimal,
    InvalidOperation,
    ROUND_CEILING,
    ROUND_FLOOR,
)


# Standar pip internal SIREADER untuk XAUUSD.
# 1 pip = 0.10 perubahan harga.
PIP_PRICE = Decimal("0.10")


def _positive_decimal(value, field_name):
    if isinstance(value, bool):
        raise ValueError(f"{field_name} tidak valid.")

    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"{field_name} tidak valid.")

    if not number.is_finite() or number <= 0:
        raise ValueError(f"{field_name} harus lebih besar dari nol.")

    return number


def pips_to_price_distance(pips):
    """Mengonversi jumlah pips menjadi perubahan harga."""
    value = _positive_decimal(pips, "Pips")
    return value * PIP_PRICE


def calculate_price_from_pips(
    entry_price,
    direction,
    pips,
    tick_size,
    level="TP",
):
    """
    Menghitung harga TP atau SL.

    TP BUY: di atas entry.
    TP SELL: di bawah entry.
    SL BUY: di bawah entry.
    SL SELL: di atas entry.

    Harga dibulatkan menjauhi entry sesuai tick size broker.
    Tidak mengirim order ke MT5.
    """

    entry = _positive_decimal(entry_price, "Entry price")
    tick = _positive_decimal(tick_size, "Tick size")
    distance = pips_to_price_distance(pips)

    direction = str(direction).strip().upper()
    level = str(level).strip().upper()

    if direction not in {"BUY", "SELL"}:
        raise ValueError("Direction hanya boleh BUY atau SELL.")

    if level not in {"TP", "SL"}:
        raise ValueError("Level hanya boleh TP atau SL.")

    if level == "TP":
        sign = 1 if direction == "BUY" else -1
    else:
        sign = -1 if direction == "BUY" else 1

    target = entry + (distance * sign)

    if target <= 0:
        raise ValueError("Harga target tidak boleh nol atau negatif.")

    # Round outward so the requested distance is not reduced.
    units = target / tick

    rounding = (
        ROUND_CEILING if target > entry else ROUND_FLOOR
    )

    rounded_units = units.to_integral_value(rounding=rounding)
    final_price = rounded_units * tick

    if final_price <= 0:
        raise ValueError("Harga hasil perhitungan tidak valid.")

    return final_price


def calculate_tp_price(entry_price, direction, pips, tick_size):
    return calculate_price_from_pips(
        entry_price=entry_price,
        direction=direction,
        pips=pips,
        tick_size=tick_size,
        level="TP",
    )


def calculate_sl_price(entry_price, direction, pips, tick_size):
    return calculate_price_from_pips(
        entry_price=entry_price,
        direction=direction,
        pips=pips,
        tick_size=tick_size,
        level="SL",
    )
