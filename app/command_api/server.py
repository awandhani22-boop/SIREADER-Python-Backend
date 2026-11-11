"""FastAPI application factory for the isolated workforce command service."""
from collections.abc import Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session, sessionmaker

from app.command_api.config import WorkforceCommandSettings
from app.command_api.database import (
    create_command_engine,
    create_command_session_factory,
    validate_command_database_security,
    validate_command_function_security,
    validate_review_function_security,
)
from app.command_api.database_commands import (
    command_functions_available,
    review_functions_available,
)
from app.command_api.routes import router
from app.command_api.security import RequestBodyLimitMiddleware, SlidingWindowRateLimiter


def create_command_app(
    *,
    session_factory: sessionmaker[Session] | Callable[[], Session] | None = None,
    review_session_factory: sessionmaker[Session] | Callable[[], Session] | None = None,
    token: str | None = None,
    settings: WorkforceCommandSettings | None = None,
    validate_database: bool = True,
    rate_limit: int = 30,
) -> FastAPI:
    injected = session_factory is not None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        command_engine = None
        review_engine = None
        if injected:
            if not token or len(token) < 32:
                raise RuntimeError("Test-injected command API requires a 32-character token.")
            app.state.command_token = token
            app.state.session_factory = session_factory
            # Test-only/legacy mode keeps one injected SQLite or disposable
            # session factory. Production function mode always uses a distinct
            # review database login and never falls back to the command login.
            app.state.review_session_factory = review_session_factory or session_factory
            app.state.require_dedicated_review_identity = False
            app.state.allow_test_client = True
        else:
            config = settings or WorkforceCommandSettings()
            config.validate_listener()
            command_token = config.get_auth_token()
            database_url = config.get_database_url()
            command_engine = create_command_engine(database_url)
            command_factory = create_command_session_factory(command_engine)
            function_layer = False
            with command_factory() as db:
                if validate_database:
                    function_layer = command_functions_available(db)
                    if function_layer:
                        validate_command_function_security(db)
                        if not review_functions_available(db):
                            raise RuntimeError("The trusted review database function layer is incomplete.")
                    else:
                        validate_command_database_security(db)

            if function_layer:
                # Separate credential and pool for adjudication. The route
                # fails closed if a production review identity is missing.
                review_url = config.get_review_database_url()
                review_engine = create_command_engine(review_url)
                review_factory = create_command_session_factory(review_engine)
                with review_factory() as review_db:
                    if validate_database:
                        if not review_functions_available(review_db):
                            raise RuntimeError("The trusted review database functions are unavailable to the review identity.")
                        validate_review_function_security(review_db)
            else:
                # Compatibility for the already accepted single-user local
                # Phase 5 command database. New production function mode never
                # takes this fallback.
                review_factory = command_factory

            app.state.command_token = command_token
            app.state.session_factory = command_factory
            app.state.review_session_factory = review_factory
            app.state.require_dedicated_review_identity = function_layer
            app.state.allow_test_client = False

        app.state.rate_limiter = SlidingWindowRateLimiter(limit=rate_limit, window_seconds=60.0)
        try:
            yield
        finally:
            if review_engine is not None:
                review_engine.dispose()
            if command_engine is not None:
                command_engine.dispose()

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
