from __future__ import annotations

import base64
import hashlib
import io
import logging
import re
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

# Official CosyVoice emotion -> Chinese performance instruction mapping
EMOTION_INSTRUCTION_MAP: dict[str, str] = {
    "neutral": "",
    "happy": "语气欢快愉悦",
    "sad": "语气低沉悲伤",
    "angry": "语气愤怒严厉",
    "fearful": "语气紧张恐惧",
    "tense": "语气紧绷克制",
    "calm": "语气平静沉稳",
    "cold": "语气冷淡疏离",
    "gentle": "语气柔和温暖",
    "excited": "语气激动昂扬",
    "whisper": "轻声低语",
    "mocking": "语气带着讥讽",
}

ALLOWED_FORMATS = frozenset({"wav", "mp3", "pcm"})
PREFIX_RE = re.compile(r"^[a-zA-Z0-9]{1,10}$")


def build_cosyvoice_instruction(
    emotion: str = "neutral",
    intensity: float = 0.5,
    instruction: str | None = None,
) -> str:
    """Convert internal emotion/intensity/instruction into an official CosyVoice instruction string."""
    parts: list[str] = []
    explicit = (instruction or "").strip()
    if explicit:
        parts.append(explicit)
    emo = (emotion or "neutral").strip().lower()
    emo_phrase = EMOTION_INSTRUCTION_MAP.get(emo, "")
    if emo_phrase and emo_phrase not in explicit:
        if intensity >= 0.75:
            parts.append(f"强烈地，{emo_phrase}")
        elif intensity <= 0.3:
            parts.append(f"轻微地，{emo_phrase}")
        else:
            parts.append(emo_phrase)
    return "，".join(p for p in parts if p)[:200]


def make_voice_prefix(seed_text: str, requested_prefix: str | None = None) -> str:
    """Generate an official DashScope voice-enrollment prefix (1-10 ASCII alphanumeric chars)."""
    if requested_prefix:
        cleaned = re.sub(r"[^a-zA-Z0-9]", "", requested_prefix)[:10]
        if cleaned:
            return cleaned.lower()
    digest = hashlib.sha256(seed_text.encode("utf-8")).hexdigest()[:8]
    return f"cv{digest}"


def _normalize_language_hints(language: str) -> list[str]:
    lang = (language or "zh-CN").strip().lower()
    if lang.startswith("zh"):
        return ["zh"]
    if lang.startswith("en"):
        return ["en"]
    if lang.startswith("ja"):
        return ["ja"]
    if lang.startswith("ko"):
        return ["ko"]
    short = lang.split("-", 1)[0][:4]
    return [short or "zh"]


def _validate_and_measure_audio(audio_bytes: bytes, audio_format: str, fallback_chars: int) -> int:
    if not audio_bytes or len(audio_bytes) < 16:
        raise TTSProviderError(
            "EMPTY_AUDIO_RESPONSE",
            "CosyVoice returned empty or truncated audio bytes",
            retryable=True,
        )
    fmt = audio_format.lower()
    if fmt == "wav":
        if audio_bytes[:4] != b"RIFF":
            raise TTSProviderError(
                "INVALID_AUDIO_FORMAT",
                "Expected WAV RIFF header from CosyVoice audio payload",
                retryable=True,
            )
        try:
            with wave.open(io.BytesIO(audio_bytes), "rb") as wf:
                rate = wf.getframerate()
                frames = wf.getnframes()
                if rate <= 0 or frames <= 0:
                    raise TTSProviderError(
                        "EMPTY_AUDIO_RESPONSE",
                        "WAV audio contains zero frames",
                        retryable=True,
                    )
                return max(1, int(round((frames / rate) * 1000)))
        except TTSProviderError:
            raise
        except Exception as exc:
            raise TTSProviderError(
                "CORRUPTED_AUDIO_RESPONSE",
                f"Failed to decode WAV audio: {exc}",
                retryable=True,
            ) from exc
    return max(250, int(round((max(1, fallback_chars) / 4.3) * 1000)))


class CosyVoiceTTSProvider(TTSProvider):
    """
    P0-01 & P0-02: Official DashScope CosyVoice Non-Streaming HTTP API v1 adapter.
    - Synthesize endpoint: /services/audio/tts/SpeechSynthesizer (configurable)
    - Voice design endpoint: /services/audio/tts/customization with model="voice-enrollment"
    - Parses official output.audio.url, downloads & validates audio immediately,
      and records usage.characters for billing accuracy.
    - Never exposes API keys to the frontend.
    """

    provider_name = "cosyvoice"
    is_mock = False

    def __init__(self, settings: Settings, http_client: httpx.Client | None = None) -> None:
        self.settings = settings
        self._http = http_client

    def _post(self, url: str, headers: dict[str, str], json_body: dict) -> httpx.Response:
        if self._http is not None:
            return self._http.post(url, headers=headers, json=json_body)
        with httpx.Client(timeout=self.settings.tts_timeout_seconds) as client:
            return client.post(url, headers=headers, json=json_body)

    def _get(self, url: str) -> httpx.Response:
        if self._http is not None:
            return self._http.get(url)
        with httpx.Client(timeout=self.settings.tts_timeout_seconds, follow_redirects=True) as client:
            return client.get(url)

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

        out_fmt = (req.output_format or "wav").lower()
        if out_fmt not in ALLOWED_FORMATS:
            raise TTSProviderError("INVALID_FORMAT", f"Unsupported audio format '{out_fmt}'")

        endpoint = self.settings.cosyvoice_tts_endpoint.lstrip("/")
        url = f"{self.settings.cosyvoice_base_url.rstrip('/')}/{endpoint}"
        headers = {
            "Authorization": f"Bearer {self.settings.cosyvoice_api_key}",
            "Content-Type": "application/json",
        }

        # Whitelisted official input fields only (P0-01)
        input_obj: dict[str, object] = {
            "text": text,
            "voice": req.voice_id,
            "format": out_fmt,
            "sample_rate": self.settings.audio_sample_rate,
            "rate": round(max(0.5, min(2.0, float(req.rate))), 2),
            "pitch": round(max(0.5, min(2.0, float(req.pitch))), 2),
        }
        eff_instruction = build_cosyvoice_instruction(
            emotion=req.emotion,
            intensity=req.intensity,
            instruction=req.instruction,
        )
        if eff_instruction:
            input_obj["instruction"] = eff_instruction

        payload = {
            "model": self.settings.cosyvoice_model,
            "input": input_obj,
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
                billed_characters: int | None = None
                audio_bytes: bytes

                if "application/json" in content_type:
                    body = resp.json()
                    request_id = body.get("request_id") or request_id
                    if body.get("code") and not body.get("output"):
                        err_code = str(body.get("code"))
                        err_msg = str(body.get("message") or err_code)
                        retryable = err_code in ("Throttling", "InternalError", "SystemError")
                        raise TTSProviderError(err_code, err_msg, retryable=retryable)

                    usage = body.get("usage") or {}
                    if isinstance(usage.get("characters"), int):
                        billed_characters = int(usage["characters"])

                    output = body.get("output") or {}
                    audio_obj = output.get("audio")
                    audio_url = audio_obj.get("url") if isinstance(audio_obj, dict) else None

                    if audio_url:
                        dl_resp = self._get(audio_url)
                        if dl_resp.status_code in (403, 404, 410):
                            raise TTSProviderError(
                                "AUDIO_URL_EXPIRED",
                                f"Audio download URL unavailable ({dl_resp.status_code})",
                                retryable=True,
                            )
                        if dl_resp.status_code >= 500:
                            raise TTSProviderError(
                                f"AUDIO_DOWNLOAD_HTTP_{dl_resp.status_code}",
                                f"Audio download server error {dl_resp.status_code}",
                                retryable=True,
                            )
                        if dl_resp.status_code >= 400:
                            raise TTSProviderError(
                                f"AUDIO_DOWNLOAD_HTTP_{dl_resp.status_code}",
                                f"Audio download client error {dl_resp.status_code}",
                                retryable=False,
                            )
                        audio_bytes = dl_resp.content
                    else:
                        audio_b64 = (
                            (audio_obj.get("data") if isinstance(audio_obj, dict) else None)
                            or output.get("audio_base64")
                            or body.get("audio_base64")
                            or ""
                        )
                        if not audio_b64:
                            raise TTSProviderError(
                                "EMPTY_AUDIO_RESPONSE",
                                "CosyVoice JSON response contained neither output.audio.url nor audio data",
                                retryable=True,
                            )
                        audio_bytes = base64.b64decode(audio_b64)
                else:
                    audio_bytes = resp.content

                duration_ms = _validate_and_measure_audio(audio_bytes, out_fmt, len(text))
                est_cost = estimate_tts_cost(len(text), self.settings.tts_price_per_10k_chars)
                if billed_characters is not None:
                    actual_cost = estimate_tts_cost(billed_characters, self.settings.tts_price_per_10k_chars)
                    cost_kind = "billed"
                else:
                    actual_cost = est_cost
                    cost_kind = "estimated"

                return SynthesisResult(
                    audio_bytes=audio_bytes,
                    audio_format=out_fmt,
                    duration_ms=duration_ms,
                    request_id=request_id,
                    provider=self.provider_name,
                    model=self.settings.cosyvoice_model,
                    input_chars=len(text),
                    estimated_cost=est_cost,
                    actual_cost=actual_cost,
                    latency_ms=latency_ms,
                    attempts=attempt,
                    billed_characters=billed_characters,
                    currency=self.settings.tts_currency,
                    cost_kind=cost_kind,
                    is_mock=False,
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
        prefix: str | None = None,
    ) -> VoiceDesignResult:
        prompt = (voice_prompt or "").strip()
        if not prompt:
            raise TTSProviderError("EMPTY_VOICE_PROMPT", "voice_prompt must not be empty")
        if len(prompt) > 500:
            raise TTSProviderError(
                "VOICE_PROMPT_TOO_LONG",
                f"voice_prompt length {len(prompt)} exceeds DashScope limit of 500 characters",
            )

        preview = (preview_text or "").strip()
        if len(preview) < 15 or len(preview) > 200:
            raise TTSProviderError(
                "INVALID_PREVIEW_TEXT_LENGTH",
                f"preview_text length {len(preview)} must be between 15 and 200 characters",
            )

        eff_prefix = make_voice_prefix(f"{language}:{prompt}", requested_prefix=prefix)
        if not PREFIX_RE.match(eff_prefix):
            raise TTSProviderError("INVALID_VOICE_PREFIX", "prefix must be 1-10 alphanumeric characters")

        endpoint = self.settings.cosyvoice_voice_design_endpoint.lstrip("/")
        url = f"{self.settings.cosyvoice_base_url.rstrip('/')}/{endpoint}"
        headers = {
            "Authorization": f"Bearer {self.settings.cosyvoice_api_key}",
            "Content-Type": "application/json",
        }
        # P0-02: Official DashScope voice-enrollment protocol
        payload = {
            "model": self.settings.cosyvoice_voice_enrollment_model,
            "input": {
                "action": "create_voice",
                "target_model": self.settings.cosyvoice_model,
                "prefix": eff_prefix,
                "voice_prompt": prompt,
                "preview_text": preview,
                "language_hints": _normalize_language_hints(language),
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

        provider_status = str(output.get("status") or "OK").upper()

        # Official structure: output.preview_audio.data (Base64 WAV)
        preview_audio_obj = output.get("preview_audio")
        audio_b64 = ""
        if isinstance(preview_audio_obj, dict):
            audio_b64 = preview_audio_obj.get("data") or ""
        if not audio_b64:
            audio_b64 = output.get("preview_audio_base64") or ""

        sample_bytes = base64.b64decode(audio_b64) if audio_b64 else b""
        if not sample_bytes:
            if provider_status == "OK":
                synth = self.synthesize(
                    SynthesisRequest(text=preview, voice_id=voice_id, output_format="wav")
                )
                sample_bytes = synth.audio_bytes
                duration_ms = synth.duration_ms
            else:
                raise TTSProviderError(
                    "MISSING_PREVIEW_AUDIO",
                    f"Voice {voice_id} returned status={provider_status} without preview_audio.data",
                    retryable=True,
                )
        else:
            duration_ms = _validate_and_measure_audio(sample_bytes, "wav", len(preview))

        return VoiceDesignResult(
            voice_id=voice_id,
            sample_audio_bytes=sample_bytes,
            sample_format="wav",
            duration_ms=duration_ms,
            request_id=body.get("request_id") or f"cv-vd-{uuid.uuid4().hex[:12]}",
            provider=self.provider_name,
            model=self.settings.cosyvoice_model,
            latency_ms=latency_ms,
            provider_status=provider_status,
            prefix=eff_prefix,
            is_mock=False,
        )

    def query_voice_status(self, voice_id: str) -> str:
        endpoint = self.settings.cosyvoice_voice_design_endpoint.lstrip("/")
        url = f"{self.settings.cosyvoice_base_url.rstrip('/')}/{endpoint}"
        headers = {
            "Authorization": f"Bearer {self.settings.cosyvoice_api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.settings.cosyvoice_voice_enrollment_model,
            "input": {
                "action": "query_voice",
                "voice_id": voice_id,
            },
        }
        try:
            resp = self._post(url, headers=headers, json_body=payload)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise TTSProviderError("NETWORK_ERROR", str(exc), retryable=True) from exc
        if resp.status_code >= 400:
            raise TTSProviderError(
                f"HTTP_{resp.status_code}",
                f"CosyVoice query_voice error {resp.status_code}: {resp.text[:300]}",
                retryable=resp.status_code in (429, 500, 502, 503, 504),
            )
        body = resp.json()
        output = body.get("output") or body
        return str(output.get("status") or "UNKNOWN").upper()


def get_tts_provider(settings: Settings) -> TTSProvider:
    """
    P0-08: Never silently fall back to MockTTSProvider in production when credentials
    are missing or TTS_MOCK_MODE is enabled.
    """
    if settings.app_env == "production":
        if settings.tts_mock_mode:
            raise TTSProviderError(
                "PRODUCTION_MOCK_BLOCKED",
                "TTS_MOCK_MODE=true is not permitted in production for synthesis/voice design",
                retryable=False,
            )
        if not (settings.cosyvoice_api_key or "").strip():
            raise TTSProviderError(
                "MISSING_PROVIDER_CREDENTIALS",
                "COSYVOICE_API_KEY must be configured in production; mock fallback is disabled",
                retryable=False,
            )
        return CosyVoiceTTSProvider(settings)

    if settings.tts_mock_mode or not (settings.cosyvoice_api_key or "").strip():
        return MockTTSProvider(settings)
    return CosyVoiceTTSProvider(settings)
