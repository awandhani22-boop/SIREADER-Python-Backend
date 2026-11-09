"""Fail-closed configuration tests for the dedicated workforce command process."""
import pytest
from app.command_api.config import WorkforceCommandSettings


def test_command_database_requires_the_dedicated_role():
    config = WorkforceCommandSettings(
        SIREADER_WORKFORCE_COMMAND_DATABASE_URL="postgresql+psycopg://postgres:secret@localhost:5432/sireader"
    )
    with pytest.raises(RuntimeError, match="sireader_workforce_cmd"):
        config.get_database_url()


def test_command_database_requires_a_password():
    config = WorkforceCommandSettings(
        SIREADER_WORKFORCE_COMMAND_DATABASE_URL="postgresql+psycopg://sireader_workforce_cmd@localhost:5432/sireader"
    )
    with pytest.raises(RuntimeError, match="password"):
        config.get_database_url()


def test_command_database_rejects_non_postgres_driver():
    config = WorkforceCommandSettings(
        SIREADER_WORKFORCE_COMMAND_DATABASE_URL="sqlite:///command.db"
    )
    with pytest.raises(RuntimeError, match="PostgreSQL"):
        config.get_database_url()


@pytest.mark.parametrize("token", ["", "short", "x" * 31, "x" * 257, "é" * 40, "x" * 32 + " "])
def test_command_token_must_be_bounded_ascii_url_safe(token):
    config = WorkforceCommandSettings(SIREADER_WORKFORCE_COMMAND_AUTH_TOKEN=token)
    with pytest.raises(RuntimeError):
        config.get_auth_token()


def test_command_token_is_separate_from_read_only_token(monkeypatch):
    token = "phase5-command-token-test-0123456789"
    monkeypatch.setenv("SIREADER_API_AUTH_TOKEN", token)
    config = WorkforceCommandSettings(SIREADER_WORKFORCE_COMMAND_AUTH_TOKEN=token)
    with pytest.raises(RuntimeError, match="distinct"):
        config.get_auth_token()


def test_listener_is_fixed_to_loopback_and_8010():
    config = WorkforceCommandSettings(WORKFORCE_COMMAND_HOST="0.0.0.0")
    with pytest.raises(RuntimeError, match="127.0.0.1"):
        config.validate_listener()
