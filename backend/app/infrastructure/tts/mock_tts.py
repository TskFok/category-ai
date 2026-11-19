from __future__ import annotations

import hashlib
import io
import math
import struct
import time
import uuid
import wave

from app.core.config import Settings
from app.domain.voice_profile import estimate_tts_cost
from app.infrastructure.tts.base import (
    SynthesisRequest,
    SynthesisResult,
    TTSProvider,
    TTSProviderError,
    VoiceDesignResult,
)


def generate_pcm_wav_bytes(
    *,
    duration_ms: int,
    frequency_hz: float = 330.0,
    sample_rate: int = 24000,
    amplitude: float = 0.18,
) -> bytes:
    """
    Generate valid 16-bit mono PCM WAV audio bytes with gentle fade-in/out.
    Produces real playable audio in browsers and passes WAV integrity checks.
    """
    num_frames = max(1, int(sample_rate * (duration_ms / 1000.0)))
    fade_frames = min(num_frames // 8, int(sample_rate * 0.02))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        frames = bytearray()
        two_pi_f = 2.0 * math.pi * frequency_hz / sample_rate
        for i in range(num_frames):
            env = 1.0
            if fade_frames > 0:
                if i < fade_frames:
                    env = i / fade_frames
                elif i > num_frames - fade_frames:
                    env = (num_frames - i) / fade_frames
            sample = int(32767 * amplitude * env * math.sin(two_pi_f * i))
            frames += struct.pack("<h", sample)
        wf.writeframes(bytes(frames))
    return buf.getvalue()


def _voice_frequency(voice_id: str, pitch: float = 1.0) -> float:
    digest = int(hashlib.sha256(voice_id.encode("utf-8")).hexdigest()[:8], 16)
    base_hz = 220.0 + (digest % 260)  # 220Hz - 480Hz distinct per voice_id
    return max(120.0, min(880.0, base_hz * max(0.5, min(2.0, pitch))))


class MockTTSProvider(TTSProvider):
    """
    Deterministic offline TTS provider for CI, local development, and automated tests.
    Produces valid PCM WAV files with distinct voice_id-dependent frequencies.
    """

    provider_name = "cosyvoice_mock"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def synthesize(self, req: SynthesisRequest) -> SynthesisResult:
        t0 = time.monotonic()
        text = (req.text or "").strip()
        if not text:
            raise TTSProviderError("EMPTY_TEXT", "Synthesis text must not be empty")
        if not req.voice_id:
            raise TTSProviderError("MISSING_VOICE_ID", "voice_id is required for synthesis")

        # Keep duration realistic yet fast for tests (150ms per 5 chars, clamped 250ms..2500ms)
        base_ms = max(250, min(2500, int((len(text) / max(0.5, req.rate)) * 45)))
        freq = _voice_frequency(req.voice_id, req.pitch)
        wav_bytes = generate_pcm_wav_bytes(
            duration_ms=base_ms,
            frequency_hz=freq,
            sample_rate=self.settings.audio_sample_rate,
        )
        cost = estimate_tts_cost(len(text), self.settings.tts_price_per_10k_chars)
        latency_ms = max(2, int((time.monotonic() - t0) * 1000))
        return SynthesisResult(
            audio_bytes=wav_bytes,
            audio_format="wav",
            duration_ms=base_ms,
            request_id=f"mock-tts-{uuid.uuid4().hex[:12]}",
            provider=self.provider_name,
            model=self.settings.cosyvoice_model,
            input_chars=len(text),
            estimated_cost=cost,
            actual_cost=cost,
            latency_ms=latency_ms,
            attempts=1,
        )

    def design_voice(
        self,
        *,
        voice_prompt: str,
        preview_text: str,
        language: str = "zh-CN",
    ) -> VoiceDesignResult:
        t0 = time.monotonic()
        prompt = (voice_prompt or "").strip()
        if not prompt:
            raise TTSProviderError("EMPTY_VOICE_PROMPT", "voice_prompt must not be empty")

        digest = hashlib.sha256(f"{language}:{prompt}".encode("utf-8")).hexdigest()[:12]
        voice_id = f"cv35-mock-{digest}"
        sample_ms = 800
        wav_bytes = generate_pcm_wav_bytes(
            duration_ms=sample_ms,
            frequency_hz=_voice_frequency(voice_id),
            sample_rate=self.settings.audio_sample_rate,
        )
        latency_ms = max(2, int((time.monotonic() - t0) * 1000))
        return VoiceDesignResult(
            voice_id=voice_id,
            sample_audio_bytes=wav_bytes,
            sample_format="wav",
            duration_ms=sample_ms,
            request_id=f"mock-vd-{uuid.uuid4().hex[:12]}",
            provider=self.provider_name,
            model=self.settings.cosyvoice_model,
            latency_ms=latency_ms,
        )
