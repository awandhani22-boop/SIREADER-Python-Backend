from __future__ import annotations

import pytest

from app.workers import maintenance


def test_function_maintenance_database_url_accepts_dedicated_pooler_identity():
    parsed = maintenance.validate_function_maintenance_database_url(
        "postgresql://sireader_workforce_maint.projectref:secret@db.example.com:6543/postgres?sslmode=verify-full",
        environ={},
    )
    assert parsed.drivername == "postgresql+psycopg"
    assert parsed.username == "sireader_workforce_maint.projectref"
    assert parsed.database == "postgres"


def test_function_maintenance_database_url_allows_explicit_local_staging():
    parsed = maintenance.validate_function_maintenance_database_url(
        "postgresql+psycopg://sireader_workforce_maint:secret@127.0.0.1:55439/sireader_stage",
        environ={},
    )
    assert parsed.host == "127.0.0.1"


@pytest.mark.parametrize(
    "url",
    [
        None,
        "",
        "postgresql://sireader_workforce_cmd:secret@localhost:5432/sireader",
        "postgresql://sireader_workforce_maint@localhost:5432/sireader",
        "postgresql://sireader_workforce_maint:secret@localhost:5432/sireader#fragment",
        "postgresql://sireader_workforce_maint:secret@localhost:5432/sireader?host=127.0.0.1",
        "postgresql://sireader_workforce_maint:secret@localhost:5432/sireader?connect_timeout=1",
        "postgresql://sireader_workforce_maint:secret@db.example.com:5432/sireader",
    ],
)
def test_function_maintenance_database_url_rejects_unsafe_inputs(url):
    with pytest.raises(RuntimeError):
        maintenance.validate_function_maintenance_database_url(url, environ={})


def test_function_maintenance_database_url_rejects_libpq_environment_override():
    with pytest.raises(RuntimeError, match="override"):
        maintenance.validate_function_maintenance_database_url(
            "postgresql://sireader_workforce_maint:secret@localhost:5432/sireader",
            environ={"PGHOST": "remote.example"},
        )


def test_function_maintenance_never_accepts_caller_supplied_time(monkeypatch):
    def should_not_connect(_url):
        raise AssertionError("Database engine must not be created for a caller-supplied timestamp.")

    monkeypatch.setattr(maintenance, "_create_function_maintenance_engine", should_not_connect)
    with pytest.raises(ValueError, match="database time"):
        maintenance._run_database_maintenance_function(
            "postgresql://sireader_workforce_maint:secret@localhost:5432/sireader",
            now=maintenance.datetime.now(),
        )


@pytest.mark.parametrize("batch_size", [0, 501, True, False, 1.5])
def test_function_maintenance_enforces_integer_batch_limit_before_connection(monkeypatch, batch_size):
    def should_not_connect(_url):
        raise AssertionError("Database engine must not be created for an invalid batch size.")

    monkeypatch.setattr(maintenance, "_create_function_maintenance_engine", should_not_connect)
    with pytest.raises(ValueError, match="batch size"):
        maintenance._run_database_maintenance_function(
            "postgresql://sireader_workforce_maint:secret@localhost:5432/sireader",
            batch_size=batch_size,
        )
