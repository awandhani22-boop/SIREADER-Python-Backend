"""Isolated database connection policy for identity-bound worker runtimes.

Live workers must never inherit the broad core application DATABASE_URL.
MOCK-only local runs may retain the historical local session factory until a
dedicated worker database identity is provisioned.
"""
from __future__ import annotations

from threading import RLock
import re

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.config import settings


_WORKER_DATABASE_ROLE_NAMES = frozenset({
    "sireader_worker_supervisor", "sireader_worker_haiku", "sireader_worker_sonnet",
    "sireader_worker_opus", "sireader_worker_decision_engineer",
    "sireader_worker_market_monitor", "sireader_worker_data_archivist",
})
_REMOTE_SSL_MODES = frozenset({"require", "verify-ca", "verify-full"})
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_LOCAL_MOCK_DATABASES = frozenset({"sireader_disposable", "sireader_test", "sireader_unit_test_unused"})


def _is_approved_worker_login(username: str) -> bool:
    """Accept one worker-specific login, optionally with a Supabase pooler suffix."""
    parts = username.split(".", 1)
    if parts[0] not in _WORKER_DATABASE_ROLE_NAMES:
        return False
    return len(parts) == 1 or bool(re.fullmatch(r"[a-z0-9-]{6,32}", parts[1]))

class WorkerDatabaseConfigurationError(RuntimeError):
    """Raised when a worker is not configured with an approved DB identity."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _core_database_is_local_mock_target() -> bool:
    """Allow the legacy core DB session only for explicit local disposable/test DBs."""
    raw_core_url = settings.DATABASE_URL
    if not raw_core_url or "#" in raw_core_url:
        return False
    try:
        core = make_url(raw_core_url.strip())
    except Exception:
        return False
    if core.get_backend_name() != "postgresql":
        return False
    if (core.host or "").lower() not in _LOOPBACK_HOSTS:
        return False
    if core.database not in _LOCAL_MOCK_DATABASES:
        return False
    # Do not allow libpq parameters to redirect an apparently local connection.
    if set(core.query) - {"sslmode"}:
        return False
    return True


def resolve_worker_database_url() -> str | None:
    """Return the dedicated worker URL, or None for the MOCK-only fallback.

    The function only parses local configuration; it never opens a connection
    and never includes the URL or username in its errors.
    """
    raw_url = settings.WORKER_DATABASE_URL
    if raw_url is None or not raw_url.strip():
        mock_only = (
            str(settings.WORKER_PROVIDER_MODE).strip().upper() == "MOCK"
            and not bool(settings.WORKER_LIVE_PROVIDER_ENABLED)
        )
        if mock_only and _core_database_is_local_mock_target():
            return None
        raise WorkerDatabaseConfigurationError(
            "WORKER_DATABASE_URL_REQUIRED",
            "Worker execution requires a dedicated WORKER_DATABASE_URL; only MOCK may use an explicit local disposable/test core database.",
        )

    candidate = raw_url.strip()
    if "#" in candidate:
        raise WorkerDatabaseConfigurationError(
            "WORKER_DATABASE_URL_INVALID",
            "WORKER_DATABASE_URL fragments are not supported.",
        )

    try:
        parsed = make_url(candidate)
    except Exception as exc:
        raise WorkerDatabaseConfigurationError(
            "WORKER_DATABASE_URL_INVALID",
            "WORKER_DATABASE_URL is not a valid PostgreSQL connection URL.",
        ) from exc

    if parsed.get_backend_name() != "postgresql":
        raise WorkerDatabaseConfigurationError(
            "WORKER_DATABASE_URL_INVALID",
            "WORKER_DATABASE_URL must use PostgreSQL.",
        )

    if not parsed.database:
        raise WorkerDatabaseConfigurationError(
            "WORKER_DATABASE_DATABASE_INVALID",
            "WORKER_DATABASE_URL must name an explicit database.",
        )

    # Reject libpq parameters that can override the apparent host/target.
    # Only sslmode is accepted for remote transport security.
    unexpected_query_keys = set(parsed.query) - {"sslmode"}
    if unexpected_query_keys:
        raise WorkerDatabaseConfigurationError(
            "WORKER_DATABASE_QUERY_INVALID",
            "WORKER_DATABASE_URL contains unsupported connection parameters.",
        )

    username = parsed.username or ""
    if not _is_approved_worker_login(username):
        raise WorkerDatabaseConfigurationError(
            "WORKER_DATABASE_ROLE_INVALID",
            "Worker database login must use one dedicated per-worker sireader_worker_<worker_id> identity.",
        )

    if not parsed.host:
        raise WorkerDatabaseConfigurationError(
            "WORKER_DATABASE_HOST_INVALID",
            "Worker database URL must include an explicit host.",
        )

    sslmode_value = parsed.query.get("sslmode")
    if sslmode_value is not None and not isinstance(sslmode_value, str):
        raise WorkerDatabaseConfigurationError(
            "WORKER_DATABASE_QUERY_INVALID",
            "WORKER_DATABASE_URL must contain at most one sslmode value.",
        )
    sslmode = (sslmode_value or "").casefold()
    if sslmode and sslmode not in _REMOTE_SSL_MODES:
        raise WorkerDatabaseConfigurationError(
            "WORKER_DATABASE_TLS_REQUIRED",
            "WORKER_DATABASE_URL sslmode must use require, verify-ca, or verify-full.",
        )

    if parsed.host.lower() not in _LOOPBACK_HOSTS:
        if sslmode not in _REMOTE_SSL_MODES:
            raise WorkerDatabaseConfigurationError(
                "WORKER_DATABASE_TLS_REQUIRED",
                "Remote worker database connections must require TLS.",
            )

    return raw_url.strip()


def validate_worker_database_configuration() -> None:
    """Validate the worker DB boundary without opening a database connection."""
    resolve_worker_database_url()


_engine_lock = RLock()
_engine_url: str | None = None
_worker_engine = None
_worker_session_factory = None


def _dedicated_worker_session_factory(url: str):
    """Create/reuse one bounded SQLAlchemy pool for the dedicated worker URL."""
    global _engine_url, _worker_engine, _worker_session_factory

    with _engine_lock:
        if _engine_url == url and _worker_session_factory is not None:
            return _worker_session_factory

        if _worker_engine is not None:
            _worker_engine.dispose()

        _worker_engine = create_engine(
            url,
            connect_args={"connect_timeout": 10},
            pool_size=2,
            max_overflow=1,
            pool_timeout=10,
            pool_recycle=1800,
            pool_pre_ping=True,
            pool_use_lifo=True,
        )
        _worker_session_factory = sessionmaker(
            bind=_worker_engine,
            autoflush=False,
            autocommit=False,
        )
        _engine_url = url
        return _worker_session_factory


def WorkerSessionLocal():
    """Return a session; the broad core DB fallback is MOCK-only."""
    url = resolve_worker_database_url()
    if url is None:
        # Lazy import prevents a live worker from even selecting this factory.
        from app.database.connection import SessionLocal as CoreSessionLocal

        return CoreSessionLocal()
    return _dedicated_worker_session_factory(url)()
