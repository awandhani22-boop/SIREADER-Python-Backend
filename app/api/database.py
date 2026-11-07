"""Konfigurasi database read-only untuk SIREADER API Gateway.

Menerapkan:
1. psycopg 3 driver sinkron dan SQLAlchemy sync engine.
2. Dedicated read-only connection pool (pool_size=2, max_overflow=0, pool_timeout=10s, pool_pre_ping=True).
3. Transaksi read-only PostgreSQL (postgresql_readonly=True dan options=-c default_transaction_read_only=on).
4. Interceptor event SQLAlchemy sebagai defense-in-depth penolakan operasi DML/DDL.
5. Lazy initialization: import modul tidak mencoba koneksi database live (fail-closed jika URL belum ada).
"""

from typing import Any, Callable
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.config import api_settings


# Daftar kata kerja mutasi SQL yang diblokir oleh interceptor defense-in-depth
BLOCKED_SQL_MUTATION_KEYWORDS = frozenset({
    "INSERT",
    "UPDATE",
    "DELETE",
    "TRUNCATE",
    "DROP",
    "ALTER",
    "CREATE",
})


def _validate_readonly_statement(
    conn: Any,
    cursor: Any,
    statement: str,
    parameters: Any,
    context: Any,
    executemany: bool,
) -> None:
    """Interceptor level aplikasi (defense-in-depth) untuk mencegah eksekusi mutasi.

    PERINGATAN ARSITEKTUR:
    Interceptor ini adalah defense-in-depth di level kode Python, BUKAN pengganti
    security boundary PostgreSQL role permission (sireader_api_ro).
    """
    clean_statement = statement.strip()
    if not clean_statement:
        return

    first_keyword = clean_statement.split()[0].upper()
    if first_keyword in BLOCKED_SQL_MUTATION_KEYWORDS:
        raise PermissionError(
            f"Operasi mutasi '{first_keyword}' ditolak oleh interceptor read-only SIREADER API. "
            "Layanan API Gateway beroperasi secara eksklusif dalam mode 100% READ-ONLY."
        )


def attach_readonly_interceptor(engine_instance: Engine) -> None:
    """Memasang interceptor defense-in-depth pada instance engine tertentu."""
    event.listen(engine_instance, "before_cursor_execute", _validate_readonly_statement)


def create_ro_engine(
    database_url: str | None = None,
    enforce_interceptor: bool = True,
) -> Engine:
    """Membuat instance SQLAlchemy Engine khusus read-only.

    Args:
        database_url: URL database khusus read-only. Jika None, mengambil dari api_settings.
        enforce_interceptor: Apakah memasang interceptor defense-in-depth.

    Returns:
        Engine: Instance SQLAlchemy Engine terkonfigurasi read-only.

    Raises:
        RuntimeError: Jika database_url tidak tersedia (fail-closed).
    """
    if database_url is None:
        database_url = api_settings.get_api_database_url()

    is_sqlite = database_url.startswith("sqlite")

    if is_sqlite:
        # Konfigurasi pengujian SQLite in-memory
        engine_instance = create_engine(
            database_url,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
            execution_options={"postgresql_readonly": True},
        )
    else:
        # Konfigurasi PostgreSQL Supabase Pooler (psycopg 3)
        engine_instance = create_engine(
            database_url,
            connect_args={
                "sslmode": "require",
                "options": "-c default_transaction_read_only=on",
            },
            pool_size=2,
            max_overflow=0,
            pool_timeout=10,
            pool_recycle=1800,
            pool_pre_ping=True,
            pool_use_lifo=True,
            execution_options={"postgresql_readonly": True},
        )

    if enforce_interceptor:
        attach_readonly_interceptor(engine_instance)

    return engine_instance


class ReadOnlySessionFactory:
    """Factory lazy singleton untuk sessionmaker read-only."""

    def __init__(self) -> None:
        self._engine: Engine | None = None
        self._sessionmaker: sessionmaker[Session] | None = None

    def initialize(self, database_url: str | None = None) -> None:
        """Menginisialisasi engine dan session factory secara eksplisit."""
        self._engine = create_ro_engine(database_url=database_url)
        self._sessionmaker = sessionmaker(
            bind=self._engine,
            autoflush=False,
            autocommit=False,
            expire_on_commit=False,
        )

    @property
    def is_initialized(self) -> bool:
        return self._sessionmaker is not None

    def get_session(self) -> Session:
        """Mengembalikan sesi baru. Menginisialisasi otomatis jika belum ada."""
        if self._sessionmaker is None:
            self.initialize()
        assert self._sessionmaker is not None
        return self._sessionmaker()

    def close(self) -> None:
        """Membersihkan engine dan koneksi pool."""
        if self._engine:
            self._engine.dispose()
            self._engine = None
            self._sessionmaker = None


# Singleton factory global untuk runtime API
ro_session_factory = ReadOnlySessionFactory()
