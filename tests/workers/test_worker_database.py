from __future__ import annotations

import pytest

from app.config import settings
from app.workers.database import (
    WorkerDatabaseConfigurationError,
    resolve_worker_database_url,
)


def _configure(monkeypatch, *, url, provider_mode="ANTHROPIC", live_enabled=True):
    monkeypatch.setattr(
        settings,
        "DATABASE_URL",
        "postgresql+psycopg://unit_test@127.0.0.1:5432/sireader_unit_test_unused",
    )
    monkeypatch.setattr(settings, "WORKER_DATABASE_URL", url)
    monkeypatch.setattr(settings, "WORKER_PROVIDER_MODE", provider_mode)
    monkeypatch.setattr(settings, "WORKER_LIVE_PROVIDER_ENABLED", live_enabled)


def test_mock_only_worker_can_use_existing_local_session_factory(monkeypatch):
    _configure(monkeypatch, url=None, provider_mode="MOCK", live_enabled=False)

    assert resolve_worker_database_url() is None


@pytest.mark.parametrize(
    "provider_mode,live_enabled",
    [("ANTHROPIC", True), ("ANTHROPIC", False), ("MOCK", True)],
)
def test_live_or_live_enabled_worker_requires_dedicated_database_url(
    monkeypatch, provider_mode, live_enabled
):
    _configure(monkeypatch, url=None, provider_mode=provider_mode, live_enabled=live_enabled)

    with pytest.raises(WorkerDatabaseConfigurationError) as raised:
        resolve_worker_database_url()

    assert raised.value.code == "WORKER_DATABASE_URL_REQUIRED"


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+psycopg://postgres:secret@db.example.test:5432/postgres?sslmode=require",
        "postgresql+psycopg://postgres.projectref:secret@db.example.test:5432/postgres?sslmode=require",
        "postgresql+psycopg://sireader_api_ro:secret@db.example.test:5432/postgres?sslmode=require",
        "postgresql+psycopg://sireader_workforce_cmd:secret@127.0.0.1:5432/sireader_disposable",
        "postgresql+psycopg://sireader_worker_runtime:secret@127.0.0.1:5432/sireader_disposable",
        "postgresql+psycopg://sireader_worker_runtime.projectref:secret@db.example.test:5432/postgres?sslmode=require",
    ],
)
def test_worker_database_rejects_other_database_identities(monkeypatch, url):
    _configure(monkeypatch, url=url)

    with pytest.raises(WorkerDatabaseConfigurationError) as raised:
        resolve_worker_database_url()

    assert raised.value.code == "WORKER_DATABASE_ROLE_INVALID"


def test_worker_database_accepts_dedicated_supabase_pooler_identity(monkeypatch):
    url = (
        "postgresql+psycopg://sireader_worker_haiku.projectref:secret@"
        "db.example.test:5432/postgres?sslmode=require"
    )
    _configure(monkeypatch, url=url)

    assert resolve_worker_database_url() == url


def test_worker_database_accepts_dedicated_loopback_test_identity(monkeypatch):
    url = "postgresql+psycopg://sireader_worker_haiku:test@127.0.0.1:55442/sireader_test"
    _configure(monkeypatch, url=url)

    assert resolve_worker_database_url() == url


def test_worker_database_rejects_remote_url_without_required_tls(monkeypatch):
    url = "postgresql+psycopg://sireader_worker_haiku:secret@db.example.test:5432/postgres"
    _configure(monkeypatch, url=url)

    with pytest.raises(WorkerDatabaseConfigurationError) as raised:
        resolve_worker_database_url()

    assert raised.value.code == "WORKER_DATABASE_TLS_REQUIRED"


def test_worker_database_rejects_non_postgres_driver(monkeypatch):
    _configure(monkeypatch, url="sqlite:///worker.db")

    with pytest.raises(WorkerDatabaseConfigurationError) as raised:
        resolve_worker_database_url()

    assert raised.value.code == "WORKER_DATABASE_URL_INVALID"


def test_worker_database_errors_never_echo_connection_url(monkeypatch):
    secret_url = (
        "postgresql+psycopg://postgres:do-not-leak@db.example.test:5432/postgres?sslmode=require"
    )
    _configure(monkeypatch, url=secret_url)

    with pytest.raises(WorkerDatabaseConfigurationError) as raised:
        resolve_worker_database_url()

    assert "do-not-leak" not in str(raised.value)
    assert "db.example.test" not in str(raised.value)



@pytest.mark.parametrize(
    "url",
    [
        "postgresql+psycopg://sireader_worker_haiku:test@127.0.0.1:55442/sireader_test?host=db.example.test&sslmode=require",
        "postgresql+psycopg://sireader_worker_haiku:test@127.0.0.1:55442/sireader_test?hostaddr=203.0.113.10",
        "postgresql+psycopg://sireader_worker_haiku:test@127.0.0.1:55442/sireader_test?service=remote",
    ],
)
def test_worker_database_rejects_libpq_target_override_parameters(monkeypatch, url):
    _configure(monkeypatch, url=url)

    with pytest.raises(WorkerDatabaseConfigurationError) as raised:
        resolve_worker_database_url()

    assert raised.value.code == "WORKER_DATABASE_QUERY_INVALID"


def test_worker_database_rejects_url_fragment(monkeypatch):
    _configure(
        monkeypatch,
        url="postgresql+psycopg://sireader_worker_haiku:test@127.0.0.1:55442/sireader_test#remote",
    )

    with pytest.raises(WorkerDatabaseConfigurationError) as raised:
        resolve_worker_database_url()

    assert raised.value.code == "WORKER_DATABASE_URL_INVALID"


def test_worker_database_requires_explicit_database_name(monkeypatch):
    _configure(
        monkeypatch,
        url="postgresql+psycopg://sireader_worker_haiku:test@127.0.0.1:55442",
    )

    with pytest.raises(WorkerDatabaseConfigurationError) as raised:
        resolve_worker_database_url()

    assert raised.value.code == "WORKER_DATABASE_DATABASE_INVALID"


def test_worker_database_accepts_case_insensitive_tls_mode(monkeypatch):
    url = (
        "postgresql+psycopg://sireader_worker_haiku.projectref:secret@"
        "db.example.test:5432/postgres?sslmode=VERIFY-FULL"
    )
    _configure(monkeypatch, url=url)

    assert resolve_worker_database_url() == url



@pytest.mark.parametrize(
    "core_url",
    [
        "postgresql+psycopg://postgres.projectref:secret@db.example.test:5432/postgres?sslmode=require",
        "postgresql+psycopg://postgres:secret@127.0.0.1:5432/postgres",
        "postgresql+psycopg://sireader_dev:secret@127.0.0.1:5432/other_database",
        "postgresql+psycopg://sireader_dev:secret@127.0.0.1:5432/sireader_disposable?host=db.example.test",
    ],
)
def test_mock_fallback_rejects_remote_or_non_disposable_core_database(monkeypatch, core_url):
    _configure(monkeypatch, url=None, provider_mode="MOCK", live_enabled=False)
    monkeypatch.setattr(settings, "DATABASE_URL", core_url)

    with pytest.raises(WorkerDatabaseConfigurationError) as raised:
        resolve_worker_database_url()

    assert raised.value.code == "WORKER_DATABASE_URL_REQUIRED"


@pytest.mark.parametrize(
    "core_url",
    [
        "postgresql+psycopg://sireader_dev:test@127.0.0.1:5432/sireader_test",
        "postgresql+psycopg://sireader_dev:test@localhost:5432/sireader_disposable",
    ],
)
def test_mock_fallback_accepts_only_explicit_local_disposable_database(monkeypatch, core_url):
    _configure(monkeypatch, url=None, provider_mode="MOCK", live_enabled=False)
    monkeypatch.setattr(settings, "DATABASE_URL", core_url)

    assert resolve_worker_database_url() is None
