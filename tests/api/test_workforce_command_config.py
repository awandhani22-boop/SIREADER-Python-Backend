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



def test_review_database_requires_separate_review_role_and_password():
    config = WorkforceCommandSettings(
        SIREADER_WORKFORCE_REVIEW_DATABASE_URL=(
            "postgresql+psycopg://sireader_workforce_cmd:secret@localhost:5432/sireader"
        )
    )
    with pytest.raises(RuntimeError, match="sireader_workforce_review"):
        config.get_review_database_url()


def test_review_database_accepts_pooler_identity_with_project_suffix():
    config = WorkforceCommandSettings(
        SIREADER_WORKFORCE_REVIEW_DATABASE_URL=(
            "postgresql+psycopg://sireader_workforce_review.projectref:secret@localhost:5432/sireader"
        )
    )
    url = config.get_review_database_url()
    assert "sireader_workforce_review.projectref" in url


def test_remote_review_database_requires_tls():
    config = WorkforceCommandSettings(
        SIREADER_WORKFORCE_REVIEW_DATABASE_URL=(
            "postgresql+psycopg://sireader_workforce_review:secret@db.example:5432/sireader"
        )
    )
    with pytest.raises(RuntimeError, match="TLS"):
        config.get_review_database_url()


def test_remote_command_database_requires_tls():
    config = WorkforceCommandSettings(
        SIREADER_WORKFORCE_COMMAND_DATABASE_URL=(
            "postgresql+psycopg://sireader_workforce_cmd:secret@db.example:5432/sireader"
        )
    )
    with pytest.raises(RuntimeError, match="TLS"):
        config.get_database_url()


@pytest.mark.parametrize("parameter", ["host", "hostaddr", "service", "servicefile", "target_session_attrs"])
def test_command_database_rejects_connection_rerouting_parameters(parameter):
    config = WorkforceCommandSettings(
        SIREADER_WORKFORCE_COMMAND_DATABASE_URL=(
            "postgresql+psycopg://sireader_workforce_cmd:secret@localhost:5432/sireader"
            f"?{parameter}=127.0.0.1"
        )
    )
    with pytest.raises(RuntimeError, match="override"):
        config.get_database_url()


def test_review_database_is_required_when_no_default_is_configured():
    config = WorkforceCommandSettings()
    with pytest.raises(RuntimeError, match="SIREADER_WORKFORCE_REVIEW_DATABASE_URL"):
        config.get_review_database_url()


@pytest.mark.parametrize("parameter", ["options", "application_name", "connect_timeout"])
def test_command_and_review_database_urls_reject_unreviewed_query_options(parameter):
    command = WorkforceCommandSettings(
        SIREADER_WORKFORCE_COMMAND_DATABASE_URL=(
            "postgresql+psycopg://sireader_workforce_cmd:secret@localhost:5432/sireader"
            f"?{parameter}=1"
        )
    )
    review = WorkforceCommandSettings(
        SIREADER_WORKFORCE_REVIEW_DATABASE_URL=(
            "postgresql+psycopg://sireader_workforce_review:secret@localhost:5432/sireader"
            f"?{parameter}=1"
        )
    )
    with pytest.raises(RuntimeError, match="sslmode"):
        command.get_database_url()
    with pytest.raises(RuntimeError, match="sslmode"):
        review.get_review_database_url()
