"""FastAPI Dependencies untuk SIREADER API Gateway.

CATATAN ARSITEKTUR KRITIS:
Seluruh dependensi database dan autentikasi dideklarasikan menggunakan 'def' sinkron
(BUKAN 'async def'). Karena psycopg dan SQLAlchemy engine menggunakan driver sinkron,
FastAPI secara otomatis menjalankan fungsi 'def' di dalam worker threadpool AnyIO,
sehingga operasi blocking I/O tidak memblokir main event loop asyncio.
"""

from typing import Generator
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.api.config import api_settings
from app.api.database import ro_session_factory
from app.api.security import verify_api_token


# Skema HTTP Bearer tanpa auto-error agar menghasilkan format respons 401 yang terkontrol
bearer_scheme = HTTPBearer(auto_error=False)


def get_ro_session() -> Generator[Session, None, None]:
    """Menyediakan sesi database read-only untuk setiap request.

    Siklus hidup:
    1. Membuka sesi dari connection pool read-only (pool_size=2).
    2. Menyerahkan sesi ke handler route ('yield').
    3. Selalu menutup dan mengembalikan koneksi ke pool ('finally').
    """
    session = ro_session_factory.get_session()
    try:
        yield session
    finally:
        session.close()


def verify_bearer_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> str:
    """Memvalidasi token Bearer server-to-server.

    Raises:
        HTTPException 401: Jika token tidak disertakan atau tidak valid.

    Returns:
        str: Token yang telah divalidasi.
    """
    if not credentials or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authorization header or invalid Bearer scheme",
            headers={"WWW-Authenticate": "Bearer"},
        )

    expected_token = api_settings.get_api_auth_token()

    if not verify_api_token(credentials.credentials, expected_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return credentials.credentials
