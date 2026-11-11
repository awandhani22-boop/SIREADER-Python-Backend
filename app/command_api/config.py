"""Fail-closed configuration for the isolated workforce command API."""
import os
import re
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

def _is_role_identity(username: str | None, expected: str) -> bool:
    return bool(username and (username == expected or username.startswith(expected + ".")))


def _validate_database_target(url, *, identity_name: str) -> None:
    if not url.password:
        raise RuntimeError(f"{identity_name} database password is missing.")
    if not url.database:
        raise RuntimeError(f"{identity_name} database URL must name an explicit database.")
    forbidden_parameters = {
        "host", "hostaddr", "service", "servicefile",
        "target_session_attrs", "load_balance_hosts",
    }
    normalized_query_keys = {str(key).lower() for key in url.query}
    if forbidden_parameters.intersection(normalized_query_keys):
        raise RuntimeError(f"{identity_name} database URL contains a forbidden connection override.")
    if normalized_query_keys - {"sslmode"}:
        raise RuntimeError(f"{identity_name} database URL permits only the sslmode query option.")
    if "#" in url.render_as_string(hide_password=False):
        raise RuntimeError(f"{identity_name} database URL fragments are not supported.")
    host = (url.host or "").strip("[]").lower()
    if host not in {"localhost", "127.0.0.1", "::1"}:
        sslmode = str(url.query.get("sslmode", "")).lower()
        if sslmode not in {"require", "verify-ca", "verify-full"}:
            raise RuntimeError(f"{identity_name} remote database connections require TLS.")


class WorkforceCommandSettings(BaseSettings):
    SIREADER_WORKFORCE_COMMAND_DATABASE_URL: str | None = Field(default=None)
    SIREADER_WORKFORCE_REVIEW_DATABASE_URL: str | None = Field(default=None)
    SIREADER_WORKFORCE_COMMAND_AUTH_TOKEN: str | None = Field(default=None)
    SIREADER_API_AUTH_TOKEN: str | None = Field(default=None, exclude=True, repr=False)
    WORKFORCE_COMMAND_HOST: str = Field(default="127.0.0.1")
    WORKFORCE_COMMAND_PORT: int = Field(default=8010)
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8-sig", extra="ignore")

    def get_database_url(self) -> str:
        raw = self.SIREADER_WORKFORCE_COMMAND_DATABASE_URL
        if not raw or not raw.strip():
            raise RuntimeError("SIREADER_WORKFORCE_COMMAND_DATABASE_URL is not configured.")
        try:
            url = make_url(raw.strip())
        except Exception as exc:
            raise RuntimeError("Workforce command database URL is invalid.") from exc
        if url.drivername == "postgresql":
            url = url.set(drivername="postgresql+psycopg")
        if url.drivername != "postgresql+psycopg":
            raise RuntimeError("Workforce command database must use PostgreSQL with psycopg 3.")
        if not _is_role_identity(url.username, "sireader_workforce_cmd"):
            raise RuntimeError("Workforce command database must authenticate as sireader_workforce_cmd.")
        _validate_database_target(url, identity_name="Workforce command")
        return url.render_as_string(hide_password=False)

    def get_review_database_url(self) -> str:
        raw = self.SIREADER_WORKFORCE_REVIEW_DATABASE_URL
        if not raw or not raw.strip():
            raise RuntimeError(
                "SIREADER_WORKFORCE_REVIEW_DATABASE_URL is required when the database review-function boundary is enabled."
            )
        try:
            url = make_url(raw.strip())
        except Exception as exc:
            raise RuntimeError("Workforce review database URL is invalid.") from exc
        if url.drivername == "postgresql":
            url = url.set(drivername="postgresql+psycopg")
        if url.drivername != "postgresql+psycopg":
            raise RuntimeError("Workforce review database must use PostgreSQL with psycopg 3.")
        if not _is_role_identity(url.username, "sireader_workforce_review"):
            raise RuntimeError("Workforce review database must authenticate as sireader_workforce_review.")
        _validate_database_target(url, identity_name="Workforce review")
        return url.render_as_string(hide_password=False)

    def get_auth_token(self) -> str:
        token = self.SIREADER_WORKFORCE_COMMAND_AUTH_TOKEN
        if not token or len(token) < 32 or len(token) > 256:
            raise RuntimeError("SIREADER_WORKFORCE_COMMAND_AUTH_TOKEN must contain 32..256 characters.")
        if not re.fullmatch(r"[A-Za-z0-9._~+/-]{32,256}", token):
            raise RuntimeError("Workforce command token must use 32..256 ASCII URL-safe characters.")
        read_only_token = (self.SIREADER_API_AUTH_TOKEN or os.getenv("SIREADER_API_AUTH_TOKEN", "")).strip()
        if read_only_token and token == read_only_token:
            raise RuntimeError("Workforce command token must be distinct from the read-only API token.")
        return token

    def validate_listener(self) -> None:
        if self.WORKFORCE_COMMAND_HOST != "127.0.0.1":
            raise RuntimeError("Workforce command API must bind exactly to 127.0.0.1.")
        if self.WORKFORCE_COMMAND_PORT != 8010:
            raise RuntimeError("Workforce command API must use the fixed port 8010.")
