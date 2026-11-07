"""Pengujian unit untuk modul konfigurasi API Gateway (app/api/config.py).

Memastikan prinsip fail-closed terpenuhi saat credential tidak tersedia,
dan memastikan tidak ada fallback ke DATABASE_URL utama.
"""

import pytest
from app.api.config import ApiSettings


def test_api_config_fail_closed_without_database_url():
    """Memastikan get_api_database_url() fail-closed jika SIREADER_API_DATABASE_URL tidak diatur."""
    settings = ApiSettings(SIREADER_API_DATABASE_URL=None)
    with pytest.raises(RuntimeError) as exc_info:
        settings.get_api_database_url()
    assert "SIREADER_API_DATABASE_URL belum dikonfigurasi" in str(exc_info.value)
    assert "fail-closed" in str(exc_info.value)


def test_api_config_fail_closed_with_empty_database_url():
    """Memastikan string kosong atau whitespace pada SIREADER_API_DATABASE_URL tetap fail-closed."""
    settings = ApiSettings(SIREADER_API_DATABASE_URL="   ")
    with pytest.raises(RuntimeError) as exc_info:
        settings.get_api_database_url()
    assert "fail-closed" in str(exc_info.value)


def test_api_config_fail_closed_without_auth_token():
    """Memastikan get_api_auth_token() fail-closed jika SIREADER_API_AUTH_TOKEN tidak diatur."""
    settings = ApiSettings(SIREADER_API_AUTH_TOKEN=None)
    with pytest.raises(RuntimeError) as exc_info:
        settings.get_api_auth_token()
    assert "SIREADER_API_AUTH_TOKEN belum dikonfigurasi" in str(exc_info.value)
    assert "fail-closed" in str(exc_info.value)


def test_api_config_database_url_driver_standardization():
    """Memastikan prefix driver postgresql:// otomatis distandarisasi ke postgresql+psycopg://."""
    settings = ApiSettings(
        SIREADER_API_DATABASE_URL="postgresql://mock_user:mock_pass@localhost:5432/mock_db"
    )
    clean_url = settings.get_api_database_url()
    assert clean_url.startswith("postgresql+psycopg://")
    assert "mock_user" in clean_url


def test_api_config_cors_origins_parsing():
    """Memastikan parsing string CORS origins menghasilkan list string bersih."""
    settings = ApiSettings(
        API_CORS_ORIGINS="http://localhost:5173, http://127.0.0.1:5173, https://office.sireader.local "
    )
    origins = settings.cors_origins_list
    assert origins == [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "https://office.sireader.local",
    ]


def test_api_config_no_fallback_to_main_database_url():
    """Memastikan ApiSettings tidak pernah menggunakan atribut DATABASE_URL utama."""
    settings = ApiSettings(SIREADER_API_DATABASE_URL=None)
    # Memastikan tidak ada atribut default DATABASE_URL di ApiSettings
    assert not hasattr(settings, "DATABASE_URL")
    # Memastikan fail-closed tetap terjadi
    with pytest.raises(RuntimeError):
        settings.get_api_database_url()
