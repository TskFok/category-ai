from __future__ import annotations

import base64
import io
import logging
import time
import uuid
import wave

import httpx

from app.core.config import Settings
from app.domain.voice_profile import estimate_tts_cost
from app.infrastructure.tts.base import (
    SynthesisRequest,
    SynthesisResult,
    TTSProvider,
    TTSProviderError,
    VoiceDesignResult,
)
from app.infrastructure.tts.mock_tts import MockTTSProvider

logger = logging.getLogger(__name__)


def _measure_audio_duration_ms(audio_bytes: bytes, audio_format: str, fallback_chars: int) -> int:
    if audio_format.lower() == "wav" and len(audio_bytes) >= 44 and audio_bytes[:4] == b"RIFF":
        try:
            with wave.open(io.BytesIO(audio_bytes), "rb") as wf:
                rate = wf.getframerate() or 24000
                frames = wf.getnframes()
                return max(1, int(round((frames / rate) * 1000)))
        except Exception:
            pass
    # Fallback estimate based on Chinese speech rate (~4.3 chars/sec)
    return max(250, int(round((max(1, fallback_chars) / 4.3) * 1000)))


class CosyVoiceTTSProvider(TTSProvider):
    """
    TTS-01: CosyVoice v3.5 Flash HTTP adapter.
    - Configurable base URL, model, timeout, retries.
    - Handles 429 rate limits, 5xx errors, network timeouts, and empty audio responses.
    - Never exposes API keys to the frontend.
    """

    provider_name = "cosyvoice"

    def __init__(self, settings: Settings, http_client: httpx.Client | None = None) -> None:
        self.settings = settings
        self._http = http_client

    def _post(self, url: str, headers: dict[str, str], json_body: dict) -> httpx.Response:
        if self._http is not None:
            return self._http.post(url, headers=headers, json=json_body)
        with httpx.Client(timeout=self.settings.tts_timeout_seconds) as client:
            return client.post(url, headers=headers, json=json_body)

    def synthesize(self, req: SynthesisRequest) -> SynthesisResult:
        text = (req.text or "").strip()
        if not text:
            raise TTSProviderError("EMPTY_TEXT", "Synthesis text must not be empty")
        if not req.voice_id:
            raise TTSProviderError("MISSING_VOICE_ID", "voice_id is required for synthesis")
        if len(text) > self.settings.tts_max_chars_per_request:
            raise TTSProviderError(
                "TEXT_TOO_LONG",
                f"Text length {len(text)} exceeds limit {self.settings.tts_max_chars_per_request}",
            )

        url = f"{self.settings.cosyvoice_base_url.rstrip('/')}/services/aigc/multimodal-generation/generation"
        headers = {
            "Authorization": f"Bearer {self.settings.cosyvoice_api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.settings.cosyvoice_model,
            "input": {
                "text": text,
                "voice": req.voice_id,
                "emotion": req.emotion,
                "instruction": req.instruction or "",
            },
            "parameters": {
                "format": req.output_format,
                "sample_rate": self.settings.audio_sample_rate,
                "rate": req.rate,
                "pitch": req.pitch,
                "emotion_intensity": req.intensity,
            },
        }

        max_attempts = self.settings.tts_max_retries + 1
        last_err: TTSProviderError | None = None

        for attempt in range(1, max_attempts + 1):
            t0 = time.monotonic()
            try:
                resp = self._post(url, headers=headers, json_body=payload)
                latency_ms = int((time.monotonic() - t0) * 1000)

                if resp.status_code == 429:
                    raise TTSProviderError("RATE_LIMIT_429", "CosyVoice rate limit exceeded", retryable=True)
                if resp.status_code >= 500:
                    raise TTSProviderError(
                        f"HTTP_{resp.status_code}",
                        f"CosyVoice server error {resp.status_code}",
                        retryable=True,
                    )
                if resp.status_code >= 400:
                    raise TTSProviderError(
                        f"HTTP_{resp.status_code}",
                        f"CosyVoice client error {resp.status_code}: {resp.text[:300]}",
                        retryable=False,
                    )

                content_type = resp.headers.get("content-type", "").lower()
                request_id = resp.headers.get("x-request-id") or f"cv-{uuid.uuid4().hex[:12]}"
                audio_bytes: bytes

                if "application/json" in content_type:
                    body = resp.json()
                    request_id = body.get("request_id") or request_id
                    audio_b64 = (
                        (body.get("output") or {}).get("audio_base64")
                        or body.get("audio_base64")
                        or ""
                    )
                    if not audio_b64:
                        raise TTSProviderError(
                            "EMPTY_AUDIO_RESPONSE",
                            "CosyVoice JSON response contained no audio data",
                            retryable=True,
                        )
                    audio_bytes = base64.b64decode(audio_b64)
                else:
                    audio_bytes = resp.content

                if not audio_bytes or len(audio_bytes) < 16:
                    raise TTSProviderError(
                        "EMPTY_AUDIO_RESPONSE",
                        "CosyVoice returned empty or truncated audio bytes",
                        retryable=True,
                    )

                duration_ms = _measure_audio_duration_ms(audio_bytes, req.output_format, len(text))
                cost = estimate_tts_cost(len(text), self.settings.tts_price_per_10k_chars)
                return SynthesisResult(
                    audio_bytes=audio_bytes,
                    audio_format=req.output_format,
                    duration_ms=duration_ms,
                    request_id=request_id,
                    provider=self.provider_name,
                    model=self.settings.cosyvoice_model,
                    input_chars=len(text),
                    estimated_cost=cost,
                    actual_cost=cost,
                    latency_ms=latency_ms,
                    attempts=attempt,
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_err = TTSProviderError("NETWORK_ERROR", str(exc), retryable=True)
            except TTSProviderError as exc:
                last_err = exc
                if not exc.retryable:
                    break

            if attempt < max_attempts:
                backoff = min(2 ** (attempt - 1), 8)
                logger.warning(
                    "CosyVoice synthesize failed (attempt %d/%d, %s); retrying in %ss",
                    attempt,
                    max_attempts,
                    last_err.error_type if last_err else "unknown",
                    backoff,
                )
                time.sleep(backoff)

        assert last_err is not None
        raise last_err

    def design_voice(
        self,
        *,
        voice_prompt: str,
        preview_text: str,
        language: str = "zh-CN",
    ) -> VoiceDesignResult:
        url = f"{self.settings.cosyvoice_base_url.rstrip('/')}/services/audio/tts/customization"
        headers = {
            "Authorization": f"Bearer {self.settings.cosyvoice_api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.settings.cosyvoice_model,
            "input": {
                "action": "create_voice",
                "target_model": self.settings.cosyvoice_model,
                "voice_prompt": voice_prompt,
                "preview_text": preview_text,
                "language": language,
            },
        }
        t0 = time.monotonic()
        try:
            resp = self._post(url, headers=headers, json_body=payload)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise TTSProviderError("NETWORK_ERROR", str(exc), retryable=True) from exc

        latency_ms = int((time.monotonic() - t0) * 1000)
        if resp.status_code >= 400:
            raise TTSProviderError(
                f"HTTP_{resp.status_code}",
                f"CosyVoice voice design error {resp.status_code}: {resp.text[:300]}",
                retryable=resp.status_code in (429, 500, 502, 503, 504),
            )
        body = resp.json()
        output = body.get("output") or body
        voice_id = output.get("voice_id")
        if not voice_id:
            raise TTSProviderError("INVALID_VOICE_DESIGN_RESPONSE", "Missing voice_id in response")

        audio_b64 = output.get("preview_audio_base64") or ""
        sample_bytes = base64.b64decode(audio_b64) if audio_b64 else b""
        if not sample_bytes:
            # Synthesize a short preview with the newly created voice_id
            synth = self.synthesize(
                SynthesisRequest(text=preview_text, voice_id=voice_id, output_format="wav")
            )
            sample_bytes = synth.audio_bytes
            duration_ms = synth.duration_ms
        else:
            duration_ms = _measure_audio_duration_ms(sample_bytes, "wav", len(preview_text))

        return VoiceDesignResult(
            voice_id=voice_id,
            sample_audio_bytes=sample_bytes,
            sample_format="wav",
            duration_ms=duration_ms,
            request_id=body.get("request_id") or f"cv-vd-{uuid.uuid4().hex[:12]}",
            provider=self.provider_name,
            model=self.settings.cosyvoice_model,
            latency_ms=latency_ms,
        )


def get_tts_provider(settings: Settings) -> TTSProvider:
    if settings.tts_mock_mode or not settings.cosyvoice_api_key:
        return MockTTSProvider(settings)
    return CosyVoiceTTSProvider(settings)
