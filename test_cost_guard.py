
from decimal import Decimal

import pytest

from app.ai import cost_guard


@pytest.mark.parametrize(
    "tier, expected",
    [
        ("FAST", Decimal("0.835600")),
        ("REASONING", Decimal("2.130350")),
        ("ESCALATION", Decimal("7.515550")),
    ],
)
def test_perhitungan_biaya_semua_tier(tier, expected):
    """
    Menguji tarif input, output, cache read, dan cache write.
    Input biasa tidak memasukkan token cache.
    """

    result = cost_guard.calculate_cost_rp(
        tier=tier,
        input_tokens=1000,
        output_tokens=100,
        cache_read_tokens=200,
        cache_write_tokens=50,
    )

    assert result == expected


def test_biaya_tanpa_cache():
    result = cost_guard.calculate_cost_rp(
        tier="FAST",
        input_tokens=1000,
        output_tokens=100,
    )

    assert result == Decimal("0.792000")


def test_semua_token_nol():
    result = cost_guard.calculate_cost_rp(
        tier="FAST",
        input_tokens=0,
        output_tokens=0,
        cache_read_tokens=0,
        cache_write_tokens=0,
    )

    assert result == Decimal("0.000000")


@pytest.mark.parametrize(
    "kwargs",
    [
        {
            "input_tokens": -1,
            "output_tokens": 100,
        },
        {
            "input_tokens": 100,
            "output_tokens": -1,
        },
        {
            "input_tokens": 100,
            "output_tokens": 100,
            "cache_read_tokens": -1,
        },
        {
            "input_tokens": 100,
            "output_tokens": 100,
            "cache_write_tokens": -1,
        },
    ],
)
def test_token_negatif_ditolak(kwargs):
    with pytest.raises(ValueError):
        cost_guard.calculate_cost_rp(
            tier="FAST",
            **kwargs,
        )


def test_boolean_token_ditolak():
    with pytest.raises(ValueError):
        cost_guard.calculate_cost_rp(
            tier="FAST",
            input_tokens=True,
            output_tokens=100,
        )
