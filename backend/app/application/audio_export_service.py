from __future__ import annotations

import io
import json
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.application.episode_service import get_episode
from app.core.config import Settings
from app.core.errors import AppError
from app.domain.enums import AudioSegmentStatus, SegmentReviewStatus
from app.infrastructure.audio.ffmpeg_pipeline import MergeInputSegment, merge_episode_audio
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


def _collect_episode_merge_inputs(
    db: Session,
    storage: LocalStorage,
    ep: Episode,
) -> tuple[list[MergeInputSegment], list[dict]]:
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

    blockers: list[dict] = []
    merge_inputs: list[MergeInputSegment] = []
    manifest_segs: list[dict] = []

    for seg in active_segs:
        aud = cur_audios.get(seg.id)
        if not aud or aud.status != AudioSegmentStatus.READY.value or not aud.audio_ref:
            blockers.append(
                {
                    "episode_no": ep.episode_no,
                    "segment_id": seg.id,
                    "order_no": seg.order_no,
                    "status": aud.status if aud else "missing",
                }
            )
            continue
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

    return merge_inputs, manifest_segs


def create_audio_export(
    db: Session,
    storage: LocalStorage,
    settings: Settings,
    *,
    novel_id: str,
    episode_id: str | None = None,
    plan_id: str | None = None,
    fmt: str = "mp3",
) -> AudioExport:
    """
    EXPORT-01:
    - Single episode: mp3 or wav + manifest.json
    - Whole novel (or plan): zip containing `<novel_title>/<01_ep_title>.<ext>` + `manifest.json`
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
        format=req_fmt,
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

            merge_inputs, manifest_segs = _collect_episode_merge_inputs(db, storage, ep)
            audio_bytes, total_ms, actual_fmt = merge_episode_audio(
                merge_inputs,
                output_format=req_fmt,
                ffmpeg_bin=settings.ffmpeg_bin,
                bitrate=settings.audio_bitrate,
                sample_rate=settings.audio_sample_rate,
                gap_same_ms=settings.audio_gap_same_speaker_ms,
                gap_change_ms=settings.audio_gap_speaker_change_ms,
            )
            file_ref, file_size = storage.save_audio_bytes(
                f"{novel.id}/exports/{export.id}.{actual_fmt}",
                audio_bytes,
            )
            manifest = {
                "export_id": export.id,
                "novel_id": novel.id,
                "novel_title": novel.title,
                "source_version": novel.source_version,
                "data_revision": novel.data_revision,
                "tts_model": settings.cosyvoice_model,
                "requested_format": req_fmt,
                "actual_format": actual_fmt,
                "exported_at": datetime.now(timezone.utc).isoformat(),
                "episodes": [
                    {
                        "episode_id": ep.id,
                        "episode_no": ep.episode_no,
                        "title": ep.title,
                        "duration_ms": total_ms,
                        "segment_count": len(manifest_segs),
                        "segments": manifest_segs,
                    }
                ],
            }
            manifest_bytes = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
            manifest_ref, _ = storage.save_audio_bytes(
                f"{novel.id}/exports/{export.id}_manifest.json",
                manifest_bytes,
            )

            export.format = actual_fmt
            export.file_ref = file_ref
            export.manifest_ref = manifest_ref
            export.file_size_bytes = file_size
            export.duration_ms = total_ms
            export.status = "ready"
        else:
            # ZIP archive of all episodes in specified plan (or latest plan)
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

            zip_buf = io.BytesIO()
            total_ms = 0
            manifest_eps = []
            folder = _safe_filename(novel.title)

            with zipfile.ZipFile(zip_buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                for ep in episodes:
                    merge_inputs, manifest_segs = _collect_episode_merge_inputs(db, storage, ep)
                    ep_bytes, ep_ms, actual_ext = merge_episode_audio(
                        merge_inputs,
                        output_format=settings.audio_output_format,
                        ffmpeg_bin=settings.ffmpeg_bin,
                        bitrate=settings.audio_bitrate,
                        sample_rate=settings.audio_sample_rate,
                        gap_same_ms=settings.audio_gap_same_speaker_ms,
                        gap_change_ms=settings.audio_gap_speaker_change_ms,
                    )
                    entry_name = f"{folder}/{ep.episode_no:02d}_{_safe_filename(ep.title)}.{actual_ext}"
                    zf.writestr(entry_name, ep_bytes)
                    total_ms += ep_ms
                    manifest_eps.append(
                        {
                            "episode_id": ep.id,
                            "episode_no": ep.episode_no,
                            "title": ep.title,
                            "archive_path": entry_name,
                            "duration_ms": ep_ms,
                            "segment_count": len(manifest_segs),
                            "segments": manifest_segs,
                        }
                    )

                manifest = {
                    "export_id": export.id,
                    "novel_id": novel.id,
                    "novel_title": novel.title,
                    "plan_id": target_plan.id,
                    "plan_version": target_plan.plan_version,
                    "source_version": novel.source_version,
                    "data_revision": novel.data_revision,
                    "tts_model": settings.cosyvoice_model,
                    "total_duration_ms": total_ms,
                    "exported_at": datetime.now(timezone.utc).isoformat(),
                    "episodes": manifest_eps,
                }
                manifest_bytes = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
                zf.writestr(f"{folder}/manifest.json", manifest_bytes)

            file_ref, file_size = storage.save_audio_bytes(
                f"{novel.id}/exports/{export.id}.zip",
                zip_buf.getvalue(),
            )
            manifest_ref, _ = storage.save_audio_bytes(
                f"{novel.id}/exports/{export.id}_manifest.json",
                manifest_bytes,
            )
            export.file_ref = file_ref
            export.manifest_ref = manifest_ref
            export.file_size_bytes = file_size
            export.duration_ms = total_ms
            export.status = "ready"

        db.commit()
        db.refresh(export)
        return export
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
