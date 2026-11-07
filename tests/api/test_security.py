"""Pengujian unit untuk modul keamanan dan verifikasi token (app/api/security.py)."""

from app.api.security import verify_api_token


def test_verify_api_token_exact_match():
    """Memastikan token yang cocok mengembalikan True."""
    assert verify_api_token("sireader-secure-bearer-12345", "sireader-secure-bearer-12345") is True


def test_verify_api_token_with_whitespace():
    """Memastikan token dengan whitespace di awal/akhir tetap divalidasi dengan aman."""
    assert verify_api_token("  token-with-spaces  ", "token-with-spaces") is True


def test_verify_api_token_wrong_token():
    """Memastikan token yang salah mengembalikan False."""
    assert verify_api_token("wrong-token-xyz", "sireader-secure-bearer-12345") is False


def test_verify_api_token_none_provided():
    """Memastikan provided_token None mengembalikan False tanpa exception."""
    assert verify_api_token(None, "expected-token-123") is False


def test_verify_api_token_empty_strings():
    """Memastikan token string kosong mengembalikan False."""
    assert verify_api_token("", "expected-token-123") is False
    assert verify_api_token("provided-token", "") is False
    assert verify_api_token("", "") is False


def test_verify_api_token_constant_time_safety():
    """Memastikan token dengan panjang berbeda tetap ditolak tanpa memicu exception."""
    assert verify_api_token("short", "much-longer-token-string-value") is False
