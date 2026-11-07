"""Utilitas keamanan dan verifikasi autentikasi token SIREADER API Gateway.

Menerapkan perbandingan token berbasis waktu-konstan (constant-time comparison)
untuk mencegah serangan timing attack, tanpa mencatat token di log/console.
"""

import secrets


def verify_api_token(provided_token: str | None, expected_token: str) -> bool:
    """Memverifikasi token autentikasi menggunakan constant-time comparison.

    Args:
        provided_token: Token yang diterima dari header Authorization Bearer.
        expected_token: Token rahasia yang dikonfigurasi di server.

    Returns:
        bool: True jika token cocok persis, False jika berbeda atau None.
    """
    if not provided_token or not expected_token:
        return False

    return secrets.compare_digest(provided_token.strip(), expected_token.strip())
