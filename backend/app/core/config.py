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

    # Auth (FIX-04: strong token required in production; optional reviewer/reader tokens)
    admin_api_token: str = ""
    reviewer_api_token: str = ""
    reader_api_token: str = ""

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

    # S5 character analysis coverage (FIX-01)
    s5_deep_analysis_top_n: int = Field(default=10, ge=1, le=500)
    s5_analyze_all_characters: bool = True

    # Pricing per 1M tokens (USD)
    price_extract_input_per_1m: float = 0.14
    price_extract_output_per_1m: float = 0.28
    price_analysis_input_per_1m: float = 0.55
    price_analysis_output_per_1m: float = 2.19

    # Storage & upload limits
    storage_root: str = "./data/storage"
    audio_storage_root: str = ""
    max_upload_bytes: int = 52_428_800  # 50 MiB

    # Chunking defaults (Section 7.1)
    default_chunk_size: int = 4500
    default_chunk_overlap: int = 400

    # Episode & Script defaults (EP-01, SCRIPT-01)
    episode_target_minutes: int = Field(default=25, ge=5, le=120)
    episode_chars_per_minute: int = Field(default=260, ge=100, le=600)
    speaker_review_threshold: float = Field(default=0.80, ge=0.0, le=1.0)
    allow_low_confidence_auto_pass: bool = False

    # TTS & Audio Pipeline (VOICE-01, TTS-01, EXPORT-01)
    tts_provider: str = "cosyvoice"
    cosyvoice_api_key: str = ""
    cosyvoice_base_url: str = "https://dashscope.aliyuncs.com/api/v1"
    cosyvoice_tts_endpoint: str = "/services/audio/tts/SpeechSynthesizer"
    cosyvoice_voice_design_endpoint: str = "/services/audio/tts/customization"
    cosyvoice_voice_enrollment_model: str = "voice-enrollment"
    cosyvoice_model: str = "cosyvoice-v3.5-flash"
    tts_mock_mode: bool = True
    tts_max_concurrency: int = Field(default=2, ge=1, le=32)
    tts_max_retries: int = Field(default=3, ge=0, le=10)
    tts_timeout_seconds: float = Field(default=120.0, gt=0)
    tts_max_chars_per_request: int = Field(default=400, ge=50, le=2000)
    tts_unit_safety_margin: int = Field(default=20, ge=0, le=200)
    # 2026-11-19 Beijing region CosyVoice v3.5 Flash public rate: 0.80 CNY / 10k chars
    tts_price_per_10k_chars: float = 0.80
    tts_currency: str = "CNY"
    llm_currency: str = "USD"
    tts_daily_budget_limit: float = Field(default=100.0, ge=0.0)
    allow_mock_audio_export: bool = False

    # Short-lived signed media token TTL (seconds)
    media_token_ttl_seconds: int = Field(default=300, ge=30, le=3600)

    ffmpeg_bin: str = "ffmpeg"
    audio_output_format: str = "mp3"
    audio_bitrate: str = "128k"
    audio_sample_rate: int = 24000
    audio_gap_same_speaker_ms: int = 250
    audio_gap_speaker_change_ms: int = 500

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def storage_path(self) -> Path:
        return Path(self.storage_root).resolve()

    @property
    def audio_storage_path(self) -> Path:
        if self.audio_storage_root:
            return Path(self.audio_storage_root).resolve()
        return (self.storage_path / "audio").resolve()


@lru_cache
def get_settings() -> Settings:
    return Settings()
