"""Pengujian exact constant-time token verification."""
import pytest

from app.api.security import verify_api_token

EXPECTED = "sireader-secure-bearer-12345"


def test_verify_api_token_exact_match():
    assert verify_api_token(EXPECTED, EXPECTED) is True


@pytest.mark.parametrize("provided", [" token", "token ", " token "])
def test_verify_api_token_rejects_added_whitespace(provided):
    assert verify_api_token(provided, "token") is False


def test_verify_api_token_wrong_token():
    assert verify_api_token("wrong-token-xyz", EXPECTED) is False


def test_verify_api_token_none_provided():
    assert verify_api_token(None, EXPECTED) is False


def test_verify_api_token_empty_strings():
    assert verify_api_token("", EXPECTED) is False
    assert verify_api_token("provided-token", "") is False
    assert verify_api_token("", "") is False


def test_verify_api_token_does_not_normalize_either_argument():
    assert verify_api_token(" token ", "token") is False
    assert verify_api_token("token", " token ") is False


def test_verify_api_token_rejects_different_lengths():
    assert verify_api_token("short", "much-longer-token-string-value") is False
