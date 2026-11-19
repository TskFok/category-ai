from __future__ import annotations

import hashlib
import json
import re
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.application.audio_service import _resolve_active_voice_profiles, _resolve_voice_for_segment
from app.application.episode_service import get_episode
from app.core.config import Settings
from app.core.errors import AppError
from app.domain.enums import AudioSegmentStatus, SegmentReviewStatus
from app.infrastructure.audio.ffmpeg_pipeline import (
    AudioPipelineError,
    FFmpegUnavailableError,
    MergeInputSegment,
    merge_episode_audio_to_file,
)
from app.infrastructure.db.models import (
    AudioExport,
    AudioSegment,
    Episode,
    EpisodePlan,
    Novel,
    ScriptSegment,
)
from app.infrastructure.storage.local import LocalStorage


def _safe_filename(name: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", name.strip())
    return cleaned[:80] or "untitled"


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _collect_episode_merge_inputs(
    db: Session,
    storage: LocalStorage,
    settings: Settings,
    ep: Episode,
) -> tuple[list[MergeInputSegment], list[dict], bool]:
    """
    Collect and strictly validate all current READY audio segments for an episode.
    Verifies:
    - AudioSegment.status == READY
    - AudioSegment.script_revision == ScriptSegment.revision
    - AudioSegment.voice_id & voice_profile_version match active locked VoiceProfile (P0-05)
    - Returns (merge_inputs, manifest_segs, contains_mock).
    """
    segments = list(
        db.scalars(
            select(ScriptSegment)
            .where(ScriptSegment.episode_id == ep.id)
            .order_by(ScriptSegment.order_no.asc())
        ).all()
    )
    active_segs = [s for s in segments if s.review_status != SegmentReviewStatus.SKIP.value]
    if not active_segs:
        raise AppError(
            code="EMPTY_EPISODE_SCRIPT",
            message=f"Episode {ep.episode_no} has no active script segments to export",
            status_code=400,
        )

    seg_ids = [s.id for s in active_segs]
    cur_audios = {
        a.script_segment_id: a
        for a in db.scalars(
            select(AudioSegment).where(
                AudioSegment.script_segment_id.in_(seg_ids),
                AudioSegment.is_current.is_(True),
            )
        ).all()
    }
    by_char, by_narrator = _resolve_active_voice_profiles(db, ep.novel_id)

    blockers: list[dict] = []
    merge_inputs: list[MergeInputSegment] = []
    manifest_segs: list[dict] = []
    contains_mock = False

    for seg in active_segs:
        aud = cur_audios.get(seg.id)
        vp = _resolve_voice_for_segment(seg, by_char, by_narrator, settings)

        if not aud or aud.status != AudioSegmentStatus.READY.value or not aud.audio_ref:
            blockers.append(
                {
                    "episode_no": ep.episode_no,
                    "segment_id": seg.id,
                    "order_no": seg.order_no,
                    "status": aud.status if aud else "missing",
                    "reason": "audio missing or not ready",
                }
            )
            continue

        # P0-05 / BR-08: Reject stale audio whose voice version or script revision drifted
        if aud.script_revision != seg.revision:
            blockers.append(
                {
                    "episode_no": ep.episode_no,
                    "segment_id": seg.id,
                    "order_no": seg.order_no,
                    "status": "stale_script_revision",
                    "reason": f"audio script_revision {aud.script_revision} != current {seg.revision}",
                }
            )
            continue

        if vp and (aud.voice_id != vp.voice_id or aud.voice_profile_version != vp.profile_version):
            blockers.append(
                {
                    "episode_no": ep.episode_no,
                    "segment_id": seg.id,
                    "order_no": seg.order_no,
                    "status": "stale_voice_version",
                    "reason": (
                        f"audio voice ({aud.voice_id}@v{aud.voice_profile_version}) "
                        f"!= active ({vp.voice_id}@v{vp.profile_version})"
                    ),
                }
            )
            continue

        if aud.is_mock:
            contains_mock = True

        path = storage.get_audio_path(aud.audio_ref)
        speaker_key = seg.speaker_character_id or f"narrator:{seg.narrator_key or 'default'}"
        merge_inputs.append(
            MergeInputSegment(
                audio_path=path,
                speaker_key=speaker_key,
                duration_ms=aud.duration_ms,
            )
        )
        manifest_segs.append(
            {
                "order_no": seg.order_no,
                "segment_id": seg.id,
                "chapter_id": seg.chapter_id,
                "start_offset": seg.start_offset,
                "end_offset": seg.end_offset,
                "speaker_key": speaker_key,
                "voice_id": aud.voice_id,
                "voice_profile_version": aud.voice_profile_version,
                "script_revision": aud.script_revision,
                "provider": aud.provider,
                "model": aud.model,
                "is_mock": aud.is_mock,
                "duration_ms": aud.duration_ms,
                "checksum": aud.checksum,
                "review_status": seg.review_status,
            }
        )

    if blockers:
        raise AppError(
            code="EXPORT_BLOCKED_INCOMPLETE_AUDIO",
            message=f"Episode {ep.episode_no} has {len(blockers)} unready/stale audio segment(s); render or regenerate before exporting",
            status_code=409,
            details={"blocking_segments": blockers[:50]},
        )

    return merge_inputs, manifest_segs, contains_mock


def _compute_snapshot_hash(manifest_episodes: list[dict]) -> str:
    parts = []
    for ep in manifest_episodes:
        for seg in ep.get("segments", []):
            parts.append(
                f"{seg['segment_id']}:{seg['script_revision']}:{seg['voice_id']}:"
                f"{seg['voice_profile_version']}:{seg['checksum']}"
            )
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def create_audio_export(
    db: Session,
    storage: LocalStorage,
    settings: Settings,
    *,
    novel_id: str,
    episode_id: str | None = None,
    plan_id: str | None = None,
    fmt: str = "mp3",
    allow_wav_fallback: bool = False,
) -> AudioExport:
    """
    EXPORT-01, P0-07, P0-08 & P1-03:
    - Single episode: mp3 or wav + manifest.json (written directly to disk).
    - Whole novel (or plan): disk-backed streaming ZIP containing `<novel_title>/<01_ep_title>.<ext>` + `manifest.json`.
    - Rejects mock audio in production unless allow_mock_audio_export=True.
    - Rejects MP3 export when FFmpeg is unavailable unless allow_wav_fallback=True.
    """
    novel = db.get(Novel, novel_id)
    if not novel:
        raise AppError(code="NOVEL_NOT_FOUND", message="Novel not found", status_code=404)

    req_fmt = fmt.lower()
    if req_fmt not in ("mp3", "wav", "zip"):
        raise AppError(code="INVALID_EXPORT_FORMAT", message="Format must be mp3, wav, or zip", status_code=400)

    export = AudioExport(
        novel_id=novel.id,
        episode_id=episode_id,
        plan_id=plan_id,
        format=req_fmt,
        actual_format=req_fmt,
        status="processing",
    )
    db.add(export)
    db.flush()

    try:
        if req_fmt in ("mp3", "wav"):
            if not episode_id:
                raise AppError(
                    code="EPISODE_ID_REQUIRED",
                    message="episode_id is required for single-episode mp3/wav export",
                    status_code=400,
                )
            ep = get_episode(db, episode_id)
            if ep.novel_id != novel.id:
                raise AppError(code="EPISODE_MISMATCH", message="Episode does not belong to novel", status_code=400)

            merge_inputs, manifest_segs, is_mock = _collect_episode_merge_inputs(db, storage, settings, ep)
            if is_mock and settings.app_env == "production" and not settings.allow_mock_audio_export:
                raise AppError(
                    code="MOCK_AUDIO_EXPORT_BLOCKED",
                    message="Exporting mock-synthesized audio is blocked in production",
                    status_code=400,
                )

            rel_ref, target_path = storage.prepare_audio_path(f"{novel.id}/exports/{export.id}.{req_fmt}")
            actual_path, total_ms, actual_fmt = merge_episode_audio_to_file(
                merge_inputs,
                target_path,
                output_format=req_fmt,
                ffmpeg_bin=settings.ffmpeg_bin,
                bitrate=settings.audio_bitrate,
                sample_rate=settings.audio_sample_rate,
                gap_same_ms=settings.audio_gap_same_speaker_ms,
                gap_change_ms=settings.audio_gap_speaker_change_ms,
                allow_wav_fallback=allow_wav_fallback,
            )
            if actual_path != target_path:
                rel_ref = f"audio/{novel.id}/exports/{export.id}.{actual_fmt}"

            file_size = actual_path.stat().st_size
            file_checksum = _sha256_file(actual_path)
            ep_entry = {
                "episode_id": ep.id,
                "episode_no": ep.episode_no,
                "title": ep.title,
                "duration_ms": total_ms,
                "segment_count": len(manifest_segs),
                "segments": manifest_segs,
            }
            snapshot_hash = _compute_snapshot_hash([ep_entry])
            manifest = {
                "export_id": export.id,
                "novel_id": novel.id,
                "novel_title": novel.title,
                "source_version": novel.source_version,
                "data_revision": novel.data_revision,
                "provider_mode": "mock" if is_mock else "real",
                "tts_model": settings.cosyvoice_model,
                "requested_format": req_fmt,
                "actual_format": actual_fmt,
                "checksum": file_checksum,
                "source_snapshot_hash": snapshot_hash,
                "exported_at": datetime.now(timezone.utc).isoformat(),
                "episodes": [ep_entry],
            }
            manifest_bytes = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
            manifest_ref, _ = storage.save_audio_bytes(
                f"{novel.id}/exports/{export.id}_manifest.json",
                manifest_bytes,
            )

            export.format = actual_fmt
            export.actual_format = actual_fmt
            export.file_ref = rel_ref
            export.manifest_ref = manifest_ref
            export.file_size_bytes = file_size
            export.duration_ms = total_ms
            export.checksum = file_checksum
            export.source_snapshot_hash = snapshot_hash
            export.is_mock = is_mock
            export.status = "ready"
        else:
            # P1-03: Disk-backed streaming ZIP archive (no giant in-memory BytesIO)
            if plan_id:
                target_plan = db.get(EpisodePlan, plan_id)
            else:
                target_plan = db.scalar(
                    select(EpisodePlan)
                    .where(EpisodePlan.novel_id == novel.id)
                    .order_by(EpisodePlan.plan_version.desc())
                    .limit(1)
                )
            if not target_plan:
                raise AppError(code="NO_EPISODE_PLAN", message="No episode plan found for novel", status_code=404)

            episodes = list(
                db.scalars(
                    select(Episode)
                    .where(Episode.plan_id == target_plan.id)
                    .order_by(Episode.episode_no.asc())
                ).all()
            )
            if not episodes:
                raise AppError(code="NO_EPISODES", message="Episode plan has no episodes", status_code=400)

            export.plan_id = target_plan.id
            zip_rel_ref, zip_target_path = storage.prepare_audio_path(f"{novel.id}/exports/{export.id}.zip")
            total_ms = 0
            manifest_eps: list[dict] = []
            folder = _safe_filename(novel.title)
            any_mock = False
            ep_out_fmt = settings.audio_output_format.lower()

            with tempfile.TemporaryDirectory(prefix="ncke_zip_export_") as tmpdir:
                tmp_root = Path(tmpdir)
                with zipfile.ZipFile(zip_target_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                    for ep in episodes:
                        merge_inputs, manifest_segs, ep_is_mock = _collect_episode_merge_inputs(
                            db, storage, settings, ep
                        )
                        if ep_is_mock:
                            any_mock = True
                            if settings.app_env == "production" and not settings.allow_mock_audio_export:
                                raise AppError(
                                    code="MOCK_AUDIO_EXPORT_BLOCKED",
                                    message="Exporting mock-synthesized audio is blocked in production",
                                    status_code=400,
                                )

                        ep_tmp_target = tmp_root / f"ep_{ep.episode_no:04d}.{ep_out_fmt}"
                        actual_ep_path, ep_ms, actual_ext = merge_episode_audio_to_file(
                            merge_inputs,
                            ep_tmp_target,
                            output_format=ep_out_fmt,
                            ffmpeg_bin=settings.ffmpeg_bin,
                            bitrate=settings.audio_bitrate,
                            sample_rate=settings.audio_sample_rate,
                            gap_same_ms=settings.audio_gap_same_speaker_ms,
                            gap_change_ms=settings.audio_gap_speaker_change_ms,
                            allow_wav_fallback=allow_wav_fallback or (settings.app_env != "production"),
                        )
                        entry_name = f"{folder}/{ep.episode_no:02d}_{_safe_filename(ep.title)}.{actual_ext}"
                        zf.write(actual_ep_path, arcname=entry_name)
                        ep_checksum = _sha256_file(actual_ep_path)
                        actual_ep_path.unlink(missing_ok=True)

                        total_ms += ep_ms
                        manifest_eps.append(
                            {
                                "episode_id": ep.id,
                                "episode_no": ep.episode_no,
                                "title": ep.title,
                                "archive_path": entry_name,
                                "audio_format": actual_ext,
                                "checksum": ep_checksum,
                                "duration_ms": ep_ms,
                                "segment_count": len(manifest_segs),
                                "segments": manifest_segs,
                            }
                        )

                    snapshot_hash = _compute_snapshot_hash(manifest_eps)
                    manifest = {
                        "export_id": export.id,
                        "novel_id": novel.id,
                        "novel_title": novel.title,
                        "plan_id": target_plan.id,
                        "plan_version": target_plan.plan_version,
                        "source_version": novel.source_version,
                        "data_revision": novel.data_revision,
                        "provider_mode": "mock" if any_mock else "real",
                        "tts_model": settings.cosyvoice_model,
                        "total_duration_ms": total_ms,
                        "source_snapshot_hash": snapshot_hash,
                        "exported_at": datetime.now(timezone.utc).isoformat(),
                        "episodes": manifest_eps,
                    }
                    manifest_bytes = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
                    zf.writestr(f"{folder}/manifest.json", manifest_bytes)

            file_size = zip_target_path.stat().st_size
            zip_checksum = _sha256_file(zip_target_path)
            manifest_ref, _ = storage.save_audio_bytes(
                f"{novel.id}/exports/{export.id}_manifest.json",
                manifest_bytes,
            )
            export.file_ref = zip_rel_ref
            export.actual_format = "zip"
            export.manifest_ref = manifest_ref
            export.file_size_bytes = file_size
            export.duration_ms = total_ms
            export.checksum = zip_checksum
            export.source_snapshot_hash = snapshot_hash
            export.is_mock = any_mock
            export.status = "ready"

        db.commit()
        db.refresh(export)
        return export
    except FFmpegUnavailableError as exc:
        db.rollback()
        raise AppError(code=exc.code, message=str(exc), status_code=422) from exc
    except AudioPipelineError as exc:
        db.rollback()
        raise AppError(code=exc.code, message=str(exc), status_code=422) from exc
    except AppError:
        db.rollback()
        raise
    except Exception as exc:
        export.status = "failed"
        export.error_message = str(exc)[:500]
        db.commit()
        raise AppError(
            code="AUDIO_EXPORT_FAILED",
            message=f"Audio export failed: {exc}",
            status_code=500,
        ) from exc


def list_audio_exports(db: Session, novel_id: str) -> list[AudioExport]:
    if not db.get(Novel, novel_id):
        raise AppError(code="NOVEL_NOT_FOUND", message="Novel not found", status_code=404)
    return list(
        db.scalars(
            select(AudioExport)
            .where(AudioExport.novel_id == novel_id)
            .order_by(AudioExport.created_at.desc())
        ).all()
    )


def get_audio_export_file(db: Session, storage: LocalStorage, export_id: str) -> tuple[AudioExport, Path]:
    exp = db.get(AudioExport, export_id)
    if not exp or not exp.file_ref or exp.status != "ready":
        raise AppError(code="AUDIO_EXPORT_NOT_FOUND", message="Audio export file not ready or not found", status_code=404)
    return exp, storage.get_audio_path(exp.file_ref)
