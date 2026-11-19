from __future__ import annotations

import array
import io
import logging
import shutil
import subprocess
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


class AudioPipelineError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class FFmpegUnavailableError(AudioPipelineError):
    def __init__(self, ffmpeg_bin: str, requested_format: str) -> None:
        super().__init__(
            "FFMPEG_UNAVAILABLE",
            f"FFmpeg binary '{ffmpeg_bin}' is not available to encode '{requested_format}'. "
            "Install FFmpeg or explicitly export as WAV.",
        )


@dataclass(frozen=True)
class MergeInputSegment:
    audio_path: Path
    speaker_key: str
    duration_ms: int


@dataclass(frozen=True)
class WavMetadata:
    sample_rate: int
    channels: int
    sampwidth: int
    num_frames: int
    duration_ms: int


def inspect_wav_bytes(raw: bytes) -> WavMetadata:
    """Validate that raw bytes are a valid, non-empty, decodable PCM WAV."""
    if not raw or len(raw) < 44 or raw[:4] != b"RIFF":
        raise AudioPipelineError("INVALID_AUDIO_FORMAT", "Audio payload is not a valid RIFF WAV file")
    try:
        with wave.open(io.BytesIO(raw), "rb") as wf:
            sr = wf.getframerate()
            ch = wf.getnchannels()
            sw = wf.getsampwidth()
            nf = wf.getnframes()
            if sr <= 0 or ch <= 0 or sw not in (1, 2, 3, 4) or nf <= 0:
                raise AudioPipelineError("EMPTY_AUDIO_FILE", "WAV file has invalid parameters or zero frames")
            dur_ms = max(1, int(round((nf / sr) * 1000)))
            return WavMetadata(sample_rate=sr, channels=ch, sampwidth=sw, num_frames=nf, duration_ms=dur_ms)
    except AudioPipelineError:
        raise
    except Exception as exc:
        raise AudioPipelineError("CORRUPTED_AUDIO_FILE", f"Unable to decode WAV audio: {exc}") from exc


def inspect_wav_file(path: Path) -> WavMetadata:
    if not path.exists() or path.stat().st_size < 44:
        raise AudioPipelineError("INVALID_AUDIO_FORMAT", f"Audio file missing or truncated: {path.name}")
    try:
        with wave.open(str(path), "rb") as wf:
            sr = wf.getframerate()
            ch = wf.getnchannels()
            sw = wf.getsampwidth()
            nf = wf.getnframes()
            if sr <= 0 or ch <= 0 or sw not in (1, 2, 3, 4) or nf <= 0:
                raise AudioPipelineError("EMPTY_AUDIO_FILE", f"WAV file has zero frames: {path.name}")
            dur_ms = max(1, int(round((nf / sr) * 1000)))
            return WavMetadata(sample_rate=sr, channels=ch, sampwidth=sw, num_frames=nf, duration_ms=dur_ms)
    except AudioPipelineError:
        raise
    except Exception as exc:
        raise AudioPipelineError("CORRUPTED_AUDIO_FILE", f"Unable to decode WAV file {path.name}: {exc}") from exc


def _decode_frames_to_mono_int16(frames: bytes, *, channels: int, sampwidth: int) -> array.array:
    """Convert raw PCM frames (8/16/24/32-bit, N channels) into signed 16-bit mono samples."""
    if sampwidth == 2:
        samples = array.array("h")
        samples.frombytes(frames)
        if channels == 1:
            return samples
        mono = array.array("h")
        total_frames = len(samples) // channels
        for i in range(total_frames):
            acc = 0
            base = i * channels
            for c in range(channels):
                acc += samples[base + c]
            mono.append(max(-32768, min(32767, acc // channels)))
        return mono

    if sampwidth == 1:
        # 8-bit PCM WAV is unsigned [0..255]
        mono = array.array("h")
        total_frames = len(frames) // channels
        for i in range(total_frames):
            acc = 0
            base = i * channels
            for c in range(channels):
                acc += (frames[base + c] - 128) << 8
            mono.append(max(-32768, min(32767, acc // channels)))
        return mono

    if sampwidth == 3:
        mono = array.array("h")
        stride = 3 * channels
        total_frames = len(frames) // stride
        for i in range(total_frames):
            acc = 0
            base = i * stride
            for c in range(channels):
                off = base + c * 3
                val = int.from_bytes(frames[off : off + 3], byteorder="little", signed=True)
                acc += val >> 8
            mono.append(max(-32768, min(32767, acc // channels)))
        return mono

    if sampwidth == 4:
        samples32 = array.array("i")
        samples32.frombytes(frames)
        mono = array.array("h")
        total_frames = len(samples32) // channels
        for i in range(total_frames):
            acc = 0
            base = i * channels
            for c in range(channels):
                acc += samples32[base + c] >> 16
            mono.append(max(-32768, min(32767, acc // channels)))
        return mono

    raise AudioPipelineError("UNSUPPORTED_SAMPLE_WIDTH", f"Unsupported PCM sample width: {sampwidth}")


def _resample_int16(samples: array.array, src_rate: int, dst_rate: int) -> array.array:
    """Linear-interpolation resampler for mono 16-bit PCM (no external C dependencies)."""
    if src_rate == dst_rate or not samples:
        return samples
    src_len = len(samples)
    dst_len = max(1, int(round(src_len * (dst_rate / src_rate))))
    out = array.array("h")
    ratio = src_rate / dst_rate
    for i in range(dst_len):
        src_pos = i * ratio
        idx0 = int(src_pos)
        if idx0 >= src_len - 1:
            out.append(samples[-1])
        else:
            frac = src_pos - idx0
            v = int(round(samples[idx0] * (1.0 - frac) + samples[idx0 + 1] * frac))
            out.append(max(-32768, min(32767, v)))
    return out


def _generate_silence_wav_frames(duration_ms: int, sample_rate: int, channels: int = 1, sampwidth: int = 2) -> bytes:
    num_frames = max(0, int(sample_rate * (duration_ms / 1000.0)))
    return b"\x00" * (num_frames * channels * sampwidth)


def normalize_wav_file_to_pcm_frames(
    audio_path: Path,
    *,
    target_sample_rate: int = 24000,
    target_channels: int = 1,
    target_sampwidth: int = 2,
) -> tuple[bytes, int]:
    """
    P0-07: Validate and normalize a WAV file to standard mono 16-bit PCM at target_sample_rate.
    Returns (normalized_pcm_frames, duration_ms).
    """
    meta = inspect_wav_file(audio_path)
    with wave.open(str(audio_path), "rb") as wf:
        raw_frames = wf.readframes(wf.getnframes())

    if (
        meta.sample_rate == target_sample_rate
        and meta.channels == target_channels
        and meta.sampwidth == target_sampwidth
    ):
        return raw_frames, meta.duration_ms

    mono = _decode_frames_to_mono_int16(raw_frames, channels=meta.channels, sampwidth=meta.sampwidth)
    resampled = _resample_int16(mono, meta.sample_rate, target_sample_rate)
    dur_ms = max(1, int(round((len(resampled) / target_sample_rate) * 1000)))
    return resampled.tobytes(), dur_ms


def _merge_wav_to_file_normalized(
    segments: list[MergeInputSegment],
    out_wav_path: Path,
    *,
    sample_rate: int = 24000,
    gap_same_ms: int = 250,
    gap_change_ms: int = 500,
) -> int:
    """
    Stream-write normalized 16-bit mono PCM WAV to disk (bounded memory) with exact silence gaps.
    Normalizes any segment whose sample_rate / channels / sampwidth differs from target.
    Returns total_duration_ms.
    """
    total_ms = 0
    with wave.open(str(out_wav_path), "wb") as out_wf:
        out_wf.setnchannels(1)
        out_wf.setsampwidth(2)
        out_wf.setframerate(sample_rate)

        for idx, seg in enumerate(segments):
            if idx > 0:
                prev_speaker = segments[idx - 1].speaker_key
                gap_ms = gap_same_ms if prev_speaker == seg.speaker_key else gap_change_ms
                if gap_ms > 0:
                    out_wf.writeframes(_generate_silence_wav_frames(gap_ms, sample_rate, 1, 2))
                    total_ms += gap_ms

            norm_frames, seg_ms = normalize_wav_file_to_pcm_frames(
                seg.audio_path,
                target_sample_rate=sample_rate,
            )
            out_wf.writeframes(norm_frames)
            total_ms += seg_ms

    # Post-merge integrity verification
    final_meta = inspect_wav_file(out_wav_path)
    return max(total_ms, final_meta.duration_ms)


def merge_wav_bytes_list(
    wav_chunks: list[bytes],
    *,
    sample_rate: int = 24000,
    gap_ms: int = 120,
) -> tuple[bytes, int]:
    """Concatenate multiple in-memory WAV chunks (e.g. SynthesisUnits of one segment) into normalized WAV bytes."""
    if not wav_chunks:
        raise AudioPipelineError("EMPTY_SEGMENTS", "Cannot merge empty audio unit list")
    if len(wav_chunks) == 1:
        meta = inspect_wav_bytes(wav_chunks[0])
        if meta.sample_rate == sample_rate and meta.channels == 1 and meta.sampwidth == 2:
            return wav_chunks[0], meta.duration_ms

    out_buf = io.BytesIO()
    total_ms = 0
    with wave.open(out_buf, "wb") as out_wf:
        out_wf.setnchannels(1)
        out_wf.setsampwidth(2)
        out_wf.setframerate(sample_rate)
        for idx, raw in enumerate(wav_chunks):
            if idx > 0 and gap_ms > 0:
                out_wf.writeframes(_generate_silence_wav_frames(gap_ms, sample_rate, 1, 2))
                total_ms += gap_ms
            meta = inspect_wav_bytes(raw)
            with wave.open(io.BytesIO(raw), "rb") as wf:
                frames = wf.readframes(wf.getnframes())
            if meta.sample_rate == sample_rate and meta.channels == 1 and meta.sampwidth == 2:
                out_wf.writeframes(frames)
                total_ms += meta.duration_ms
            else:
                mono = _decode_frames_to_mono_int16(frames, channels=meta.channels, sampwidth=meta.sampwidth)
                resampled = _resample_int16(mono, meta.sample_rate, sample_rate)
                out_wf.writeframes(resampled.tobytes())
                total_ms += max(1, int(round((len(resampled) / sample_rate) * 1000)))
    return out_buf.getvalue(), total_ms


def merge_episode_audio_to_file(
    segments: list[MergeInputSegment],
    output_path: Path,
    *,
    output_format: str = "mp3",
    ffmpeg_bin: str = "ffmpeg",
    bitrate: str = "128k",
    sample_rate: int = 24000,
    gap_same_ms: int = 250,
    gap_change_ms: int = 500,
    allow_wav_fallback: bool = False,
) -> tuple[Path, int, str]:
    """
    P0-07 & P1-03:
    Stream-merge ordered episode audio segments directly to disk with normalized PCM,
    strict format validation, and explicit FFmpeg error handling (never silently downgrading
    MP3 to WAV unless allow_wav_fallback=True).
    Returns (actual_output_path, total_duration_ms, actual_format).
    """
    if not segments:
        raise AudioPipelineError("EMPTY_SEGMENTS", "Cannot merge an empty list of audio segments")

    fmt = output_format.lower()
    if fmt not in ("wav", "mp3"):
        raise AudioPipelineError("INVALID_FORMAT", f"Unsupported output format '{fmt}'")

    ffmpeg_path = shutil.which(ffmpeg_bin)
    if fmt == "mp3" and not ffmpeg_path:
        if not allow_wav_fallback:
            raise FFmpegUnavailableError(ffmpeg_bin, fmt)
        logger.warning("FFmpeg binary '%s' not found; explicit WAV fallback enabled", ffmpeg_bin)
        fmt = "wav"
        output_path = output_path.with_suffix(".wav")

    output_path.parent.mkdir(parents=True, exist_ok=True)

    if fmt == "wav":
        total_ms = _merge_wav_to_file_normalized(
            segments,
            output_path,
            sample_rate=sample_rate,
            gap_same_ms=gap_same_ms,
            gap_change_ms=gap_change_ms,
        )
        return output_path, total_ms, "wav"

    # MP3 encoding via FFmpeg from disk-backed normalized intermediate WAV
    with tempfile.TemporaryDirectory(prefix="ncke_ffmpeg_") as tmpdir:
        in_wav = Path(tmpdir) / "merged_norm.wav"
        total_ms = _merge_wav_to_file_normalized(
            segments,
            in_wav,
            sample_rate=sample_rate,
            gap_same_ms=gap_same_ms,
            gap_change_ms=gap_change_ms,
        )
        assert ffmpeg_path is not None
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
            str(output_path),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode != 0 or not output_path.exists() or output_path.stat().st_size < 16:
            logger.error("FFmpeg encoding failed (rc=%d): %s", proc.returncode, proc.stderr[:500])
            raise AudioPipelineError("FFMPEG_ENCODE_FAILED", f"FFmpeg encoding failed: {proc.stderr[:300]}")
        return output_path, total_ms, "mp3"


def merge_episode_audio(
    segments: list[MergeInputSegment],
    *,
    output_format: str = "mp3",
    ffmpeg_bin: str = "ffmpeg",
    bitrate: str = "128k",
    sample_rate: int = 24000,
    gap_same_ms: int = 250,
    gap_change_ms: int = 500,
    allow_wav_fallback: bool = False,
) -> tuple[bytes, int, str]:
    """Convenience wrapper returning bytes from merge_episode_audio_to_file."""
    with tempfile.TemporaryDirectory(prefix="ncke_merge_") as tmpdir:
        target = Path(tmpdir) / f"episode.{output_format.lower()}"
        actual_path, total_ms, actual_fmt = merge_episode_audio_to_file(
            segments,
            target,
            output_format=output_format,
            ffmpeg_bin=ffmpeg_bin,
            bitrate=bitrate,
            sample_rate=sample_rate,
            gap_same_ms=gap_same_ms,
            gap_change_ms=gap_change_ms,
            allow_wav_fallback=allow_wav_fallback,
        )
        return actual_path.read_bytes(), total_ms, actual_fmt
