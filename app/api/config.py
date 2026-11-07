"""Konfigurasi mandiri untuk SIREADER Read-Only API Gateway.

File ini terpisah secara ketat dari app/config.py dan app/database/connection.py.
API Gateway DILARANG menggunakan DATABASE_URL utama dengan hak tulis.
Jika SIREADER_API_DATABASE_URL belum diatur, live database connection wajib fail-closed.
"""

from typing import List
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ApiSettings(BaseSettings):
    """Pengaturan runtime untuk layanan Read-Only API Gateway."""

    # Credential database khusus read-only (role sireader_api_ro)
    # DILARANG mengisi default ke DATABASE_URL utama.
    SIREADER_API_DATABASE_URL: str | None = Field(
        default=None,
        description="Connection string database khusus role read-only (sireader_api_ro).",
    )

    # Token autentikasi server-to-server (Bearer token)
    # DILARANG diekspos ke frontend atau bundle JavaScript statis.
    SIREADER_API_AUTH_TOKEN: str | None = Field(
        default=None,
        description="Pre-shared bearer token untuk autentikasi API Gateway.",
    )

    # Pengaturan server HTTP Uvicorn
    API_HOST: str = Field(default="127.0.0.1", description="Host listener API (default loopback).")
    API_PORT: int = Field(default=8000, description="Port listener API.")

    # CORS Allowlist (Hanya origin lokal frontend yang disetujui)
    API_CORS_ORIGINS: str = Field(
        default="http://localhost:5173,http://127.0.0.1:5173",
        description="Daftar origin CORS yang diizinkan, dipisahkan koma.",
    )

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8-sig",
        extra="ignore",
    )

    @property
    def cors_origins_list(self) -> List[str]:
        """Mengembalikan daftar origin CORS sebagai list string."""
        return [origin.strip() for origin in self.API_CORS_ORIGINS.split(",") if origin.strip()]

    def get_api_database_url(self) -> str:
        """Mengambil database URL khusus read-only dengan validasi fail-closed.

        Raises:
            RuntimeError: Jika SIREADER_API_DATABASE_URL tidak diatur.
        """
        raw_url = self.SIREADER_API_DATABASE_URL
        if not raw_url or not raw_url.strip():
            raise RuntimeError(
                "SIREADER_API_DATABASE_URL belum dikonfigurasi di environment. "
                "Koneksi live database ditutup secara aman (fail-closed) untuk "
                "mencegah penggunaan akun database utama yang memiliki hak tulis."
            )

        clean_url = raw_url.strip()
        # Standarisasi driver PostgreSQL ke psycopg 3
        if clean_url.startswith("postgresql://"):
            clean_url = clean_url.replace("postgresql://", "postgresql+psycopg://", 1)

        return clean_url

    def get_api_auth_token(self) -> str:
        """Mengambil secret token autentikasi API dengan validasi fail-closed.

        Raises:
            RuntimeError: Jika SIREADER_API_AUTH_TOKEN tidak diatur.
        """
        token = self.SIREADER_API_AUTH_TOKEN
        if not token or not token.strip():
            raise RuntimeError(
                "SIREADER_API_AUTH_TOKEN belum dikonfigurasi di environment. "
                "Autentikasi API ditutup secara aman (fail-closed)."
            )
        return token.strip()


# Instance singleton pengaturan API
api_settings = ApiSettings()
