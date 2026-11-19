from __future__ import annotations

import hashlib


def make_audio_cache_key(
    *,
    text_hash: str,
    voice_id: str,
    provider: str,
    target_model: str,
    emotion: str = "neutral",
    intensity: float = 0.5,
    rate: float = 1.0,
    pitch: float = 1.0,
    instruction: str | None = None,
) -> str:
    """
    TTS-01: Deterministic cache key based on normalized text hash, fixed voice_id,
    full synthesis parameters, and provider/model version.
    """
    raw = (
        f"{provider}|{target_model}|{voice_id}|{text_hash}|"
        f"{emotion}|{intensity:.2f}|{rate:.2f}|{pitch:.2f}|{(instruction or '').strip()}"
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def estimate_tts_cost(char_count: int, price_per_10k_chars: float = 0.80) -> float:
    if char_count <= 0:
        return 0.0
    return round((char_count / 10_000.0) * price_per_10k_chars, 6)
