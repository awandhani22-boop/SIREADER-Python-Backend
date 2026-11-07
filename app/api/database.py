"""Konfigurasi database read-only untuk SIREADER API Gateway.

Menerapkan:
1. psycopg 3 driver sinkron dan SQLAlchemy sync engine.
2. Dedicated read-only connection pool (pool_size=2, max_overflow=0, pool_timeout=10s, pool_pre_ping=True).
3. Transaksi read-only PostgreSQL (postgresql_readonly=True dan options=-c default_transaction_read_only=on).
4. Interceptor event SQLAlchemy sebagai defense-in-depth penolakan operasi DML/DDL.
5. Lazy initialization: import modul tidak mencoba koneksi database live (fail-closed jika URL belum ada).
6. Thread-safe singleton session factory dengan perlindungan mutasi reinitialization dan concurrent first-request.

SECURITY BOUNDARY CRITICAL NOTICE:
Interceptor SQLAlchemy di modul ini beroperasi murni sebagai mekanisme DEFENSE-IN-DEPTH
di level aplikasi Python. Pengecekan kata kunci awalan (prefix keyword check) TIDAK
dapat menjamin pemblokiran seluruh variasi mutasi DML/DDL, seperti mutasi dalam Common
Table Expressions (CTE) bertingkat (misal: `WITH u AS (UPDATE ...) SELECT * FROM u`)
atau query dengan komentar SQL di awal (misal: `/* comment */ INSERT INTO ...`).
Oleh karena itu, PostgreSQL dedicated read-only role (`sireader_api_ro`) yang dikonfigurasi
secara ketat di level database tetap merupakan security boundary utama yang mutlak.
"""

import threading
from typing import Any, Callable
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.config import api_settings


# Daftar kata kerja mutasi SQL yang diperiksa oleh interceptor defense-in-depth
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
    """Interceptor level aplikasi (defense-in-depth) untuk menolak perintah mutasi DML/DDL.

    BATASAN & ATURAN KEAMANAN:
    1. Interceptor ini HANYA merupakan lapisan pertahanan tambahan (defense-in-depth)
       di sisi aplikasi Python dan BUKAN pengganti hak akses database.
    2. Pengecekan awalan token (prefix check) hanya mengenali kata kunci SQL terdaftar
       pada posisi token pertama. Mekanisme ini TIDAK menjamin pemblokiran sintaks kompleks,
       seperti:
       - Data-modifying statements di dalam Common Table Expressions (CTE), contoh:
         `WITH updated AS (UPDATE runtime_control_state SET ...) SELECT * FROM updated`
       - Statement dengan komentar SQL atau whitespace sebelum keyword, contoh:
         `/* bypass attempt */ INSERT INTO ...`
       - Stored procedure atau function call yang melakukan mutasi internal.
    3. Dedicated database role PostgreSQL (`sireader_api_ro`) dengan hak SELECT-only
       pada tabel/view allowlist dan parameter transaksi `default_transaction_read_only=on`
       adalah security boundary primer yang menegakkan isolasi read-only secara tuntas.
    """
    clean_statement = statement.strip()
    if not clean_statement:
        return

    first_keyword = clean_statement.split()[0].upper()
    if first_keyword in BLOCKED_SQL_MUTATION_KEYWORDS:
        raise PermissionError(
            f"Operasi mutasi '{first_keyword}' ditolak oleh interceptor read-only SIREADER API. "
            "Layanan API Gateway beroperasi secara eksklusif dalam mode 100% READ-ONLY. "
            "Catatan: PostgreSQL role permissions (sireader_api_ro) tetap merupakan security boundary primer."
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
    """Factory lazy singleton untuk sessionmaker read-only dengan perlindungan konkurensi.

    Karakteristik:
    1. Thread-safe initialization: Menggunakan threading.Lock untuk mencegah
       instansiasi engine ganda saat beberapa request pertama tiba secara bersamaan.
    2. Zero connection leaks: Reinisialisasi secara eksplisit mendisposisi (dispose)
       engine dan connection pool lama sebelum membuat yang baru.
    3. Lazy initialization: Import modul tidak membuka koneksi database atau membaca URL.
    4. Clean shutdown: Metode close() mendisposisi engine dan mereset sessionmaker.
    """

    def __init__(self) -> None:
        self._engine: Engine | None = None
        self._sessionmaker: sessionmaker[Session] | None = None
        self._lock = threading.Lock()

    def _initialize_locked(self, database_url: str | None = None) -> None:
        """Inisialisasi engine dan session factory internal (pemanggil harus memegang _lock)."""
        if self._engine is not None:
            self._engine.dispose()
            self._engine = None
            self._sessionmaker = None

        self._engine = create_ro_engine(database_url=database_url)
        self._sessionmaker = sessionmaker(
            bind=self._engine,
            autoflush=False,
            autocommit=False,
            expire_on_commit=False,
        )

    def initialize(self, database_url: str | None = None) -> None:
        """Menginisialisasi engine dan session factory secara eksplisit (thread-safe)."""
        with self._lock:
            self._initialize_locked(database_url=database_url)

    @property
    def is_initialized(self) -> bool:
        with self._lock:
            return self._sessionmaker is not None

    def get_session(self) -> Session:
        """Mengembalikan sesi baru secara thread-safe.

        Menggunakan double-checked locking agar lazy initialization aman terhadap
        concurrent first requests tanpa overhead lock pada setiap pemanggilan berikutnya.
        """
        if self._sessionmaker is None:
            with self._lock:
                if self._sessionmaker is None:
                    self._initialize_locked()
        assert self._sessionmaker is not None
        return self._sessionmaker()

    def close(self) -> None:
        """Membersihkan engine dan koneksi pool secara thread-safe."""
        with self._lock:
            if self._engine is not None:
                self._engine.dispose()
                self._engine = None
                self._sessionmaker = None


# Singleton factory global untuk runtime API
ro_session_factory = ReadOnlySessionFactory()
