from __future__ import annotations

import io
import logging
import shutil
import subprocess
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MergeInputSegment:
    audio_path: Path
    speaker_key: str
    duration_ms: int


def _generate_silence_wav_frames(duration_ms: int, sample_rate: int, channels: int = 1, sampwidth: int = 2) -> bytes:
    num_frames = max(0, int(sample_rate * (duration_ms / 1000.0)))
    return b"\x00" * (num_frames * channels * sampwidth)


def _merge_wav_pure_python(
    segments: list[MergeInputSegment],
    *,
    sample_rate: int = 24000,
    gap_same_ms: int = 250,
    gap_change_ms: int = 500,
) -> tuple[bytes, int]:
    """
    Deterministic pure-Python PCM WAV concatenation with speaker-change silence gaps.
    Used directly for WAV exports or as fallback when system `ffmpeg` is not installed.
    """
    out_frames = bytearray()
    eff_rate = sample_rate
    eff_channels = 1
    eff_sampwidth = 2
    total_ms = 0

    for idx, seg in enumerate(segments):
        if idx > 0:
            prev_speaker = segments[idx - 1].speaker_key
            gap_ms = gap_same_ms if prev_speaker == seg.speaker_key else gap_change_ms
            if gap_ms > 0:
                out_frames += _generate_silence_wav_frames(gap_ms, eff_rate, eff_channels, eff_sampwidth)
                total_ms += gap_ms

        raw = seg.audio_path.read_bytes()
        with wave.open(io.BytesIO(raw), "rb") as wf:
            if idx == 0:
                eff_rate = wf.getframerate()
                eff_channels = wf.getnchannels()
                eff_sampwidth = wf.getsampwidth()
            frames = wf.readframes(wf.getnframes())
            out_frames += frames
            seg_ms = int(round((wf.getnframes() / max(1, wf.getframerate())) * 1000))
            total_ms += seg_ms

    buf = io.BytesIO()
    with wave.open(buf, "wb") as out_wf:
        out_wf.setnchannels(eff_channels)
        out_wf.setsampwidth(eff_sampwidth)
        out_wf.setframerate(eff_rate)
        out_wf.writeframes(bytes(out_frames))
    return buf.getvalue(), total_ms


def merge_episode_audio(
    segments: list[MergeInputSegment],
    *,
    output_format: str = "mp3",
    ffmpeg_bin: str = "ffmpeg",
    bitrate: str = "128k",
    sample_rate: int = 24000,
    gap_same_ms: int = 250,
    gap_change_ms: int = 500,
) -> tuple[bytes, int, str]:
    """
    EXPORT-01:
    Concatenate ordered episode audio segments with speaker-change silence gaps,
    loudness normalization, and encoding to MP3 or WAV.
    Uses real FFmpeg when available; falls back cleanly to pure-Python WAV merge
    when FFmpeg is unavailable in the host environment.
    """
    if not segments:
        raise ValueError("Cannot merge an empty list of audio segments")

    # First build a normalized intermediate WAV with exact silence gaps
    merged_wav_bytes, total_ms = _merge_wav_pure_python(
        segments,
        sample_rate=sample_rate,
        gap_same_ms=gap_same_ms,
        gap_change_ms=gap_change_ms,
    )

    fmt = output_format.lower()
    if fmt == "wav":
        return merged_wav_bytes, total_ms, "wav"

    ffmpeg_path = shutil.which(ffmpeg_bin)
    if not ffmpeg_path:
        logger.warning(
            "FFmpeg binary '%s' not found; returning PCM WAV bytes for requested format '%s'",
            ffmpeg_bin,
            fmt,
        )
        return merged_wav_bytes, total_ms, "wav"

    with tempfile.TemporaryDirectory(prefix="ncke_ffmpeg_") as tmpdir:
        in_wav = Path(tmpdir) / "merged.wav"
        out_file = Path(tmpdir) / f"output.{fmt}"
        in_wav.write_bytes(merged_wav_bytes)

        cmd = [
            ffmpeg_path,
            "-y",
            "-i",
            str(in_wav),
            "-af",
            "loudnorm=I=-16:TP=-1.5:LRA=11",
            "-ar",
            str(sample_rate),
            "-ac",
            "1",
            "-b:a",
            bitrate,
            str(out_file),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode != 0 or not out_file.exists():
            logger.error("FFmpeg encoding failed (rc=%d): %s", proc.returncode, proc.stderr[:500])
            raise RuntimeError(f"FFmpeg encoding failed: {proc.stderr[:300]}")
        return out_file.read_bytes(), total_ms, fmt
