"""Utilitas keamanan token untuk SIREADER API Gateway."""
import secrets


def verify_api_token(provided_token: str | None, expected_token: str) -> bool:
    """Membandingkan token secara exact dan constant-time."""
    if not provided_token or not expected_token:
        return False
    return secrets.compare_digest(provided_token, expected_token)
