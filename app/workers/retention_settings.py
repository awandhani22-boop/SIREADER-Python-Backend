"""Small, credential-independent settings for workforce payload retention."""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class WorkerPayloadRetentionSettings(BaseSettings):
    WORKER_TASK_PAYLOAD_RETENTION_DAYS: int = Field(default=30, ge=1, le=365)
    WORKER_REVIEW_PAYLOAD_RETENTION_DAYS: int = Field(default=90, ge=1, le=365)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8-sig",
        extra="ignore",
    )


retention_settings = WorkerPayloadRetentionSettings()
