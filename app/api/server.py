"""Application factory dan entry point Uvicorn untuk API Gateway mandiri."""
from contextlib import asynccontextmanager
import logging
from typing import AsyncIterator

import uvicorn
from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
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


def create_app(*, manage_database_lifecycle: bool = True) -> FastAPI:
    """Lifecycle dinonaktifkan hanya untuk test yang mengganti dependency DB."""

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if manage_database_lifecycle:
            validate_local_bind_host(api_settings.API_HOST)
            ro_session_factory.initialize()
        try:
            yield
        finally:
            if manage_database_lifecycle:
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
