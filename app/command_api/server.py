"""FastAPI application factory for the isolated workforce command service."""
from collections.abc import Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session, sessionmaker

from app.command_api.config import WorkforceCommandSettings
from app.command_api.database import (
    create_command_engine, create_command_session_factory,
    validate_command_database_security,
)
from app.command_api.routes import router
from app.command_api.security import RequestBodyLimitMiddleware, SlidingWindowRateLimiter


def create_command_app(
    *,
    session_factory: sessionmaker[Session] | Callable[[], Session] | None = None,
    token: str | None = None,
    settings: WorkforceCommandSettings | None = None,
    validate_database: bool = True,
    rate_limit: int = 30,
) -> FastAPI:
    injected = session_factory is not None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine = None
        if injected:
            if not token or len(token) < 32:
                raise RuntimeError("Test-injected command API requires a 32-character token.")
            app.state.command_token = token
            app.state.session_factory = session_factory
            app.state.allow_test_client = True
        else:
            config = settings or WorkforceCommandSettings()
            config.validate_listener()
            command_token = config.get_auth_token()
            database_url = config.get_database_url()
            engine = create_command_engine(database_url)
            factory = create_command_session_factory(engine)
            with factory() as db:
                if validate_database:
                    validate_command_database_security(db)
            app.state.command_token = command_token
            app.state.session_factory = factory
            app.state.allow_test_client = False
        app.state.rate_limiter = SlidingWindowRateLimiter(limit=rate_limit, window_seconds=60.0)
        try:
            yield
        finally:
            if engine is not None:
                engine.dispose()

    app = FastAPI(
        title="SIREADER Workforce Command API",
        version="1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def enforce_loopback_boundary(request: Request, call_next):
        allowed_test = bool(getattr(request.app.state, "allow_test_client", False))
        host = request.headers.get("host", "")
        peer = request.client.host if request.client else ""
        if allowed_test and host in {"testserver", "localhost"} and peer in {"testclient", "127.0.0.1"}:
            return await call_next(request)
        if host != "127.0.0.1:8010" or peer not in {"127.0.0.1", "::1"}:
            return JSONResponse(status_code=403, content={"detail": "LOOPBACK_ONLY"})
        return await call_next(request)

    app.include_router(router)
    app.add_middleware(RequestBodyLimitMiddleware, max_bytes=20 * 1024)
    return app
