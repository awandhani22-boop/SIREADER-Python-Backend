"""Fail-closed configuration for the isolated workforce command API."""
import os
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

class WorkforceCommandSettings(BaseSettings):
    SIREADER_WORKFORCE_COMMAND_DATABASE_URL: str | None = Field(default=None)
    SIREADER_WORKFORCE_COMMAND_AUTH_TOKEN: str | None = Field(default=None)
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
        if url.username != "sireader_workforce_cmd":
            raise RuntimeError("Workforce command database must authenticate as sireader_workforce_cmd.")
        if not url.password:
            raise RuntimeError("Workforce command database password is missing.")
        return url.render_as_string(hide_password=False)

    def get_auth_token(self) -> str:
        token = self.SIREADER_WORKFORCE_COMMAND_AUTH_TOKEN
        if not token or len(token) < 32 or len(token) > 256:
            raise RuntimeError("SIREADER_WORKFORCE_COMMAND_AUTH_TOKEN must contain 32..256 characters.")
        if not all(ch.isalnum() or ch in "._~+/-=" for ch in token):
            raise RuntimeError("Workforce command token contains unsupported characters.")
        read_only_token = os.getenv("SIREADER_API_AUTH_TOKEN", "").strip()
        if read_only_token and token == read_only_token:
            raise RuntimeError("Workforce command token must be distinct from the read-only API token.")
        return token

    def validate_listener(self) -> None:
        if self.WORKFORCE_COMMAND_HOST != "127.0.0.1":
            raise RuntimeError("Workforce command API must bind exactly to 127.0.0.1.")
        if self.WORKFORCE_COMMAND_PORT != 8010:
            raise RuntimeError("Workforce command API must use the fixed port 8010.")
