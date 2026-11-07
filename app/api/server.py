"""Application factory dan entry point Uvicorn untuk API Gateway mandiri."""
from collections.abc import Mapping
from contextlib import asynccontextmanager
import logging
from typing import Any, AsyncIterator

import uvicorn
from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.api.config import api_settings
from app.api.database import ro_session_factory
from app.api.dependencies import verify_bearer_token
from app.api.routes import (
    audit_router,
    market_watches_router,
    pipeline_router,
    system_router,
    telemetry_router,
)

logger = logging.getLogger(__name__)


def validate_local_bind_host(host: str) -> str:
    if host != "127.0.0.1":
        raise RuntimeError("API Gateway lokal hanya boleh bind ke 127.0.0.1.")
    return host


def validate_ro_database_identity(identity: Mapping[str, Any] | None) -> None:
    """Fail closed unless the DB connection uses the dedicated least-privilege role."""
    if identity is None:
        raise RuntimeError("Cannot identify the API database role.")
    if identity.get("role_name") != "sireader_api_ro":
        raise RuntimeError(
            "SIREADER_API_DATABASE_URL must authenticate specifically as sireader_api_ro."
        )
    if identity.get("transaction_read_only") not in {"on", True}:
        raise RuntimeError("PostgreSQL connection is not in default read-only transaction mode.")

    required_false = (
        "rolsuper", "rolinherit", "rolcreatedb",
        "rolcreaterole", "rolreplication", "rolbypassrls",
    )
    if identity.get("rolcanlogin") is not True:
        raise RuntimeError("sireader_api_ro must have LOGIN.")
    if any(identity.get(field) is not False for field in required_false):
        raise RuntimeError(
            "sireader_api_ro role attributes violate the required least-privilege policy."
        )


def _validate_live_database_session() -> None:
    """Verify authenticated role attributes and transaction mode before serving requests."""
    session = ro_session_factory.get_session()
    try:
        identity = session.execute(
            text(
                """
                SELECT
                    current_user AS role_name,
                    current_setting('transaction_read_only') AS transaction_read_only,
                    r.rolcanlogin,
                    r.rolsuper,
                    r.rolinherit,
                    r.rolcreatedb,
                    r.rolcreaterole,
                    r.rolreplication,
                    r.rolbypassrls
                FROM pg_roles AS r
                WHERE r.rolname = current_user
                """
            )
        ).mappings().one_or_none()
        validate_ro_database_identity(identity)
    finally:
        session.close()


def create_app(*, manage_database_lifecycle: bool = True) -> FastAPI:
    """Lifecycle dinonaktifkan hanya untuk test yang mengganti dependency DB."""

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        initialized = False
        try:
            if manage_database_lifecycle:
                validate_local_bind_host(api_settings.API_HOST)
                ro_session_factory.initialize()
                initialized = True
                _validate_live_database_session()
            yield
        finally:
            if manage_database_lifecycle and initialized:
                ro_session_factory.close()

    app = FastAPI(
        title="SIREADER Read-Only Observability API",
        version="3.1.0",
        docs_url="/docs",
        redoc_url=None,
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=api_settings.cors_origins_list,
        allow_credentials=False,
        allow_methods=["GET"],
        allow_headers=["Authorization", "Content-Type"],
    )

    @app.exception_handler(SQLAlchemyError)
    def database_error_handler(_: Request, exc: SQLAlchemyError) -> JSONResponse:
        logger.error("SIREADER API database operation failed: %s", type(exc).__name__)
        return JSONResponse(status_code=503, content={"detail": "Read-only database is unavailable."})

    @app.exception_handler(RuntimeError)
    def configuration_error_handler(_: Request, exc: RuntimeError) -> JSONResponse:
        logger.error("SIREADER API configuration/lifecycle error: %s", str(exc))
        return JSONResponse(status_code=503, content={"detail": "API Gateway configuration is unavailable."})

    for route in (
        system_router,
        telemetry_router,
        pipeline_router,
        market_watches_router,
        audit_router,
    ):
        app.include_router(route, dependencies=[Depends(verify_bearer_token)])
    return app


app = create_app()


def run() -> None:
    host = validate_local_bind_host(api_settings.API_HOST)
    if not 1 <= api_settings.API_PORT <= 65535:
        raise RuntimeError("API_PORT harus berada pada rentang 1..65535.")
    uvicorn.run(app, host=host, port=api_settings.API_PORT, workers=1)


if __name__ == "__main__":
    run()
