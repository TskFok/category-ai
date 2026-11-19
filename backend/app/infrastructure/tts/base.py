from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


class TTSProviderError(Exception):
    def __init__(self, error_type: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.retryable = retryable


@dataclass(frozen=True)
class SynthesisRequest:
    text: str
    voice_id: str
    emotion: str = "neutral"
    intensity: float = 0.5
    rate: float = 1.0
    pitch: float = 1.0
    instruction: str | None = None
    output_format: str = "wav"


@dataclass(frozen=True)
class SynthesisResult:
    audio_bytes: bytes
    audio_format: str
    duration_ms: int
    request_id: str
    provider: str
    model: str
    input_chars: int
    estimated_cost: float
    actual_cost: float
    latency_ms: int
    attempts: int = 1
    billed_characters: int | None = None
    currency: str = "CNY"
    cost_kind: str = "estimated"  # "estimated" | "billed"
    is_mock: bool = False


@dataclass(frozen=True)
class VoiceDesignResult:
    voice_id: str
    sample_audio_bytes: bytes
    sample_format: str
    duration_ms: int
    request_id: str
    provider: str
    model: str
    latency_ms: int
    provider_status: str = "OK"
    prefix: str = ""
    is_mock: bool = False


class TTSProvider(ABC):
    provider_name: str = "base"
    is_mock: bool = False

    @abstractmethod
    def synthesize(self, req: SynthesisRequest) -> SynthesisResult:
        """Synthesize a single speaker segment into audio bytes."""

    @abstractmethod
    def design_voice(
        self,
        *,
        voice_prompt: str,
        preview_text: str,
        language: str = "zh-CN",
        prefix: str | None = None,
    ) -> VoiceDesignResult:
        """Create a reusable voice_id from a voice design prompt and return preview audio."""

    def query_voice_status(self, voice_id: str) -> str:
        """Query the deployment status (DEPLOYING / OK / UNDEPLOYED / UNKNOWN) of a designed voice."""
        _ = voice_id
        return "OK"
