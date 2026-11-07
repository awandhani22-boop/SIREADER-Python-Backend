"""Pengujian siklus hidup koneksi database dan penolakan mutasi (app/api/database.py)."""

import pytest
from sqlalchemy import text
from app.api.database import (
    create_ro_engine,
    ReadOnlySessionFactory,
)
from app.api.dependencies import get_ro_session


def test_create_ro_engine_sqlite_in_memory():
    """Memastikan engine read-only dapat dibuat di memori dengan execution_options yang benar."""
    engine = create_ro_engine("sqlite:///:memory:", enforce_interceptor=False)
    assert engine is not None
    exec_options = engine.get_execution_options()
    assert exec_options.get("postgresql_readonly") is True

    with engine.connect() as conn:
        val = conn.execute(text("SELECT 100")).scalar()
        assert val == 100

    engine.dispose()


def test_interceptor_allows_select_queries():
    """Memastikan interceptor mengizinkan kueri SELECT normal."""
    engine = create_ro_engine("sqlite:///:memory:", enforce_interceptor=True)

    with engine.connect() as conn:
        result = conn.execute(text("SELECT 1 AS num, 'active' AS status")).fetchone()
        assert result is not None
        assert result[0] == 1
        assert result[1] == "active"

    engine.dispose()


@pytest.mark.parametrize("mutation_sql", [
    "INSERT INTO dummy_table (col) VALUES (1)",
    "UPDATE dummy_table SET col = 2",
    "DELETE FROM dummy_table WHERE col = 1",
    "TRUNCATE TABLE dummy_table",
    "DROP TABLE dummy_table",
    "ALTER TABLE dummy_table ADD COLUMN col2 INT",
    "CREATE TABLE dummy_table (col INT)",
])
def test_interceptor_blocks_mutations(mutation_sql):
    """Memastikan interceptor defense-in-depth menolak seluruh operasi DML/DDL."""
    engine = create_ro_engine("sqlite:///:memory:", enforce_interceptor=True)

    with engine.connect() as conn:
        with pytest.raises(PermissionError) as exc_info:
            conn.execute(text(mutation_sql))
        assert "ditolak oleh interceptor read-only SIREADER API" in str(exc_info.value)

    engine.dispose()


def test_ro_session_factory_lifecycle():
    """Memastikan siklus inisialisasi, akuisisi sesi, dan pembersihan pada ReadOnlySessionFactory."""
    factory = ReadOnlySessionFactory()
    assert factory.is_initialized is False

    factory.initialize(database_url="sqlite:///:memory:")
    assert factory.is_initialized is True

    session = factory.get_session()
    assert session is not None
    # Jalankan query read-only sederhana
    val = session.execute(text("SELECT 42")).scalar()
    assert val == 42
    session.close()

    factory.close()
    assert factory.is_initialized is False


def test_get_ro_session_dependency_always_closes():
    """Memastikan get_ro_session generator selalu menutup sesi bahkan jika consumer mengalami exception."""
    factory = ReadOnlySessionFactory()
    factory.initialize(database_url="sqlite:///:memory:")

    # Inject factory ke dependensi
    import app.api.dependencies as deps
    orig_factory = deps.ro_session_factory
    deps.ro_session_factory = factory

    closed_called = False

    try:
        gen = get_ro_session()
        session = next(gen)

        # Monitor pemanggilan session.close()
        orig_close = session.close

        def tracking_close():
            nonlocal closed_called
            closed_called = True
            orig_close()

        session.close = tracking_close

        # Simulasikan exception di handler route
        try:
            gen.throw(ValueError("Simulated handler error"))
        except ValueError:
            pass

        # Sesi harus sudah tertutup (session.close() terpanggil via finally)
        assert closed_called is True

    finally:
        deps.ro_session_factory = orig_factory
        factory.close()
