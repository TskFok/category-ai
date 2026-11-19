from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: Literal["development", "test", "production"] = "development"
    public_base_url: str = "http://localhost:8080"
    log_level: str = "INFO"
    cors_origins: str = "http://localhost:5173,http://localhost:8080"

    # Auth (ADR-07: V1 single admin with role boundary preserved)
    admin_api_token: str = ""

    # Database & Redis
    database_url: str = "sqlite:///./data/ncke_dev.db"
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "redis://localhost:6379/0"
    celery_result_backend: str = "redis://localhost:6379/1"

    # DeepSeek LLM configuration (ADR-10: configurable, never hardcoded in business logic)
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_api_key: str = ""
    deepseek_extract_model: str = "deepseek-flash"
    deepseek_analysis_model: str = "deepseek-v4-pro"
    deepseek_mock_mode: bool = False

    llm_max_concurrency: int = Field(default=4, ge=1, le=64)
    llm_timeout_seconds: float = Field(default=90.0, gt=0)
    llm_max_retries: int = Field(default=3, ge=0, le=10)
    job_budget_limit: float = Field(default=50.0, ge=0.0)

    # Pricing per 1M tokens (USD)
    price_extract_input_per_1m: float = 0.14
    price_extract_output_per_1m: float = 0.28
    price_analysis_input_per_1m: float = 0.55
    price_analysis_output_per_1m: float = 2.19

    # Storage & upload limits
    storage_root: str = "./data/storage"
    max_upload_bytes: int = 52_428_800  # 50 MiB

    # Chunking defaults (Section 7.1)
    default_chunk_size: int = 4500
    default_chunk_overlap: int = 400

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def storage_path(self) -> Path:
        return Path(self.storage_root).resolve()


@lru_cache
def get_settings() -> Settings:
    return Settings()
