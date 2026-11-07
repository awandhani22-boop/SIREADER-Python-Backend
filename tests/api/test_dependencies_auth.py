"""Pengujian unit dan integrasi dependensi autentikasi Bearer token (app/api/dependencies.py)."""

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from app.api.config import api_settings
from app.api.dependencies import verify_bearer_token

TEST_SECRET_TOKEN = "test-sireader-bearer-token-xyz789"


def test_auth_missing_credentials_raises_401():
    """Memastikan credentials=None memicu HTTPException 401."""
    with pytest.raises(HTTPException) as exc_info:
        verify_bearer_token(None)
    assert exc_info.value.status_code == 401
    assert "Missing authorization header" in exc_info.value.detail
    assert exc_info.value.headers.get("WWW-Authenticate") == "Bearer"


def test_auth_invalid_scheme_raises_401():
    """Memastikan skema non-Bearer (misal: Basic) memicu HTTPException 401."""
    creds = HTTPAuthorizationCredentials(scheme="Basic", credentials="username:password")
    with pytest.raises(HTTPException) as exc_info:
        verify_bearer_token(creds)
    assert exc_info.value.status_code == 401
    assert "invalid Bearer scheme" in exc_info.value.detail


def test_auth_invalid_token_raises_401(monkeypatch):
    """Memastikan token yang tidak cocok memicu HTTPException 401."""
    monkeypatch.setattr(api_settings, "SIREADER_API_AUTH_TOKEN", TEST_SECRET_TOKEN)
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials="wrong-token-abc")
    with pytest.raises(HTTPException) as exc_info:
        verify_bearer_token(creds)
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Invalid authentication credentials"
    assert exc_info.value.headers.get("WWW-Authenticate") == "Bearer"


def test_auth_valid_token_succeeds(monkeypatch):
    """Memastikan token yang cocok mengembalikan string token yang valid."""
    monkeypatch.setattr(api_settings, "SIREADER_API_AUTH_TOKEN", TEST_SECRET_TOKEN)
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=TEST_SECRET_TOKEN)
    result = verify_bearer_token(creds)
    assert result == TEST_SECRET_TOKEN


def test_auth_fail_closed_without_server_token(monkeypatch):
    """Memastikan jika server belum mengonfigurasi SIREADER_API_AUTH_TOKEN, akses gagal secara fail-closed."""
    monkeypatch.setattr(api_settings, "SIREADER_API_AUTH_TOKEN", None)
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials="any-token")
    with pytest.raises(RuntimeError) as exc_info:
        verify_bearer_token(creds)
    assert "SIREADER_API_AUTH_TOKEN belum dikonfigurasi" in str(exc_info.value)
    assert "fail-closed" in str(exc_info.value)
