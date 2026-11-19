from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.application.episode_service import get_episode
from app.core.config import Settings
from app.core.errors import AppError
from app.domain.enums import (
    AudioSegmentStatus,
    EpisodeStatus,
    JobState,
    SegmentReviewStatus,
    VoiceApprovalStatus,
)
from app.domain.text_processing import sha256_bytes
from app.domain.voice_profile import make_audio_cache_key
from app.infrastructure.db.models import (
    AudioSegment,
    Character,
    Episode,
    Novel,
    ProviderCall,
    RenderJob,
    ScriptSegment,
    VoicePerformance,
    VoiceProfile,
)
from app.infrastructure.storage.local import LocalStorage
from app.infrastructure.tts.base import SynthesisRequest, TTSProvider, TTSProviderError
from app.infrastructure.tts.cosyvoice import get_tts_provider

logger = logging.getLogger(__name__)


def _resolve_active_voice_profiles(db: Session, novel_id: str) -> tuple[dict[str, VoiceProfile], dict[str, VoiceProfile]]:
    """Return (by_character_id, by_narrator_key) maps of active locked VoiceProfiles."""
    profiles = list(
        db.scalars(
            select(VoiceProfile).where(
                VoiceProfile.novel_id == novel_id,
                VoiceProfile.is_active.is_(True),
                VoiceProfile.approval_status == VoiceApprovalStatus.LOCKED.value,
                VoiceProfile.voice_id.is_not(None),
            )
        ).all()
    )
    by_char: dict[str, VoiceProfile] = {}
    by_narrator: dict[str, VoiceProfile] = {}
    for vp in profiles:
        if vp.character_id:
            by_char[vp.character_id] = vp
        elif vp.narrator_key:
            by_narrator[vp.narrator_key] = vp
    return by_char, by_narrator


def _check_missing_voices_for_episode(
    db: Session,
    ep: Episode,
    segments: list[ScriptSegment],
) -> list[str]:
    by_char, by_narrator = _resolve_active_voice_profiles(db, ep.novel_id)
    missing: list[str] = []
    seen_keys: set[str] = set()

    for seg in segments:
        if seg.review_status == SegmentReviewStatus.SKIP.value:
            continue
        if seg.speaker_character_id:
            if seg.speaker_character_id not in by_char and seg.speaker_character_id not in seen_keys:
                seen_keys.add(seg.speaker_character_id)
                char = db.get(Character, seg.speaker_character_id)
                missing.append(f"character:{char.canonical_name if char else seg.speaker_character_id}")
        else:
            nk = seg.narrator_key or "default"
            if nk not in by_narrator and f"narrator:{nk}" not in seen_keys:
                seen_keys.add(f"narrator:{nk}")
                missing.append(f"narrator:{nk}")
    return missing


def create_render_job(
    db: Session,
    settings: Settings,
    *,
    episode_id: str,
    budget_limit: float | None = None,
    idempotency_key: str | None = None,
) -> RenderJob:
    ep = get_episode(db, episode_id)

    if idempotency_key:
        existing = db.scalar(
            select(RenderJob).where(
                RenderJob.episode_id == ep.id,
                RenderJob.idempotency_key == idempotency_key,
            )
        )
        if existing:
            return existing

    segments = list(
        db.scalars(
            select(ScriptSegment)
            .where(ScriptSegment.episode_id == ep.id)
            .order_by(ScriptSegment.order_no.asc())
        ).all()
    )
    if not segments:
        raise AppError(
            code="NO_SCRIPT_SEGMENTS",
            message="Analyze episode script before starting audio rendering",
            status_code=400,
        )

    # VOICE-01: Block synthesis if any active speaker/narrator lacks a locked voice_id
    missing = _check_missing_voices_for_episode(db, ep, segments)
    if missing:
        raise AppError(
            code="MISSING_LOCKED_VOICES",
            message=f"All speakers must have a locked voice_id before rendering. Missing: {', '.join(missing)}",
            status_code=400,
            details={"missing_voices": missing},
        )

    by_char, by_narrator = _resolve_active_voice_profiles(db, ep.novel_id)
    snapshot = {
        "characters": {cid: {"profile_id": vp.id, "voice_id": vp.voice_id, "version": vp.profile_version} for cid, vp in by_char.items()},
        "narrators": {nk: {"profile_id": vp.id, "voice_id": vp.voice_id, "version": vp.profile_version} for nk, vp in by_narrator.items()},
    }
    novel = db.get(Novel, ep.novel_id)
    active_segs = [s for s in segments if s.review_status != SegmentReviewStatus.SKIP.value]

    job = RenderJob(
        novel_id=ep.novel_id,
        episode_id=ep.id,
        status=JobState.PENDING.value,
        budget_limit=budget_limit if budget_limit is not None else settings.job_budget_limit,
        spent_cost=0.0,
        source_revision=novel.data_revision if novel else 1,
        profile_snapshot_json=snapshot,
        progress_json={
            "total_segments": len(active_segs),
            "completed_segments": 0,
            "cached_segments": 0,
            "failed_segments": 0,
        },
        idempotency_key=idempotency_key,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def _synthesize_single_segment(
    db: Session,
    storage: LocalStorage,
    settings: Settings,
    tts: TTSProvider,
    *,
    seg: ScriptSegment,
    vp: VoiceProfile,
    render_job: RenderJob | None = None,
    force_regenerate: bool = False,
) -> tuple[AudioSegment, bool]:
    """
    Synthesize one ScriptSegment with cache reuse (TTS-01) and budget guard (OPS-01).
    Returns (AudioSegment, was_cache_hit).
    """
    latest_perf = db.scalar(
        select(VoicePerformance)
        .where(VoicePerformance.segment_id == seg.id)
        .order_by(VoicePerformance.version.desc())
        .limit(1)
    )
    instruction = latest_perf.instruction if latest_perf else None
    eff_rate = seg.speech_rate or vp.default_rate
    eff_pitch = latest_perf.pitch if latest_perf else vp.pitch

    assert vp.voice_id is not None
    cache_key = make_audio_cache_key(
        text_hash=seg.text_hash,
        voice_id=vp.voice_id,
        provider=tts.provider_name,
        target_model=vp.target_model,
        emotion=seg.emotion,
        intensity=seg.emotion_intensity,
        rate=eff_rate,
        pitch=eff_pitch,
        instruction=instruction,
    )

    if not force_regenerate:
        cached = db.scalar(
            select(AudioSegment)
            .where(
                AudioSegment.cache_key == cache_key,
                AudioSegment.status == AudioSegmentStatus.READY.value,
                AudioSegment.audio_ref.is_not(None),
            )
            .order_by(AudioSegment.created_at.desc())
            .limit(1)
        )
        if cached and cached.audio_ref:
            try:
                storage.get_audio_path(cached.audio_ref)
                # If cached belongs to this exact segment and is already current, return as-is
                if cached.script_segment_id == seg.id and cached.is_current:
                    return cached, True
                # Mark previous current audio as non-current and link cached audio ref
                for old in db.scalars(
                    select(AudioSegment).where(
                        AudioSegment.script_segment_id == seg.id,
                        AudioSegment.is_current.is_(True),
                    )
                ).all():
                    old.is_current = False
                reused = AudioSegment(
                    novel_id=seg.novel_id,
                    episode_id=seg.episode_id,
                    script_segment_id=seg.id,
                    render_job_id=render_job.id if render_job else None,
                    voice_profile_id=vp.id,
                    voice_profile_version=vp.profile_version,
                    voice_id=vp.voice_id,
                    audio_ref=cached.audio_ref,
                    audio_format=cached.audio_format,
                    duration_ms=cached.duration_ms,
                    file_size_bytes=cached.file_size_bytes,
                    checksum=cached.checksum,
                    cache_key=cache_key,
                    is_current=True,
                    status=AudioSegmentStatus.READY.value,
                )
                db.add(reused)
                db.flush()
                return reused, True
            except AppError:
                pass  # file missing on disk; synthesize fresh

    # Budget check before EVERY paid TTS call (OPS-01)
    if render_job and render_job.budget_limit > 0 and render_job.spent_cost >= render_job.budget_limit:
        raise TTSProviderError(
            "BUDGET_LIMIT_REACHED",
            f"Render job budget limit reached (${render_job.spent_cost:.4f} >= ${render_job.budget_limit:.4f})",
        )

    req = SynthesisRequest(
        text=seg.text_content,
        voice_id=vp.voice_id,
        emotion=seg.emotion,
        intensity=seg.emotion_intensity,
        rate=eff_rate,
        pitch=eff_pitch,
        instruction=instruction,
        output_format="wav",
    )
    result = tts.synthesize(req)
    checksum = sha256_bytes(result.audio_bytes)
    rel_ref, size_bytes = storage.save_audio_bytes(
        f"{seg.novel_id}/episodes/{seg.episode_id}/{seg.id}_{checksum[:12]}.{result.audio_format}",
        result.audio_bytes,
    )

    db.add(
        ProviderCall(
            novel_id=seg.novel_id,
            render_job_id=render_job.id if render_job else None,
            provider=result.provider,
            model=result.model,
            operation="synthesize",
            request_id=result.request_id,
            input_chars=result.input_chars,
            audio_duration_ms=result.duration_ms,
            estimated_cost=result.estimated_cost,
            actual_cost=result.actual_cost,
            latency_ms=result.latency_ms,
            attempt=result.attempts,
            status="ok",
        )
    )
    if render_job:
        render_job.spent_cost = round(render_job.spent_cost + result.actual_cost, 6)

    # Mark previous current AudioSegments for this script segment as historical (rollback preserved)
    for old in db.scalars(
        select(AudioSegment).where(
            AudioSegment.script_segment_id == seg.id,
            AudioSegment.is_current.is_(True),
        )
    ).all():
        old.is_current = False

    aud = AudioSegment(
        novel_id=seg.novel_id,
        episode_id=seg.episode_id,
        script_segment_id=seg.id,
        render_job_id=render_job.id if render_job else None,
        voice_profile_id=vp.id,
        voice_profile_version=vp.profile_version,
        voice_id=vp.voice_id,
        audio_ref=rel_ref,
        audio_format=result.audio_format,
        duration_ms=result.duration_ms,
        file_size_bytes=size_bytes,
        checksum=checksum,
        cache_key=cache_key,
        is_current=True,
        status=AudioSegmentStatus.READY.value,
    )
    db.add(aud)
    db.flush()
    return aud, False


def execute_render_job(
    db: Session,
    storage: LocalStorage,
    settings: Settings,
    render_job_id: str,
    tts_provider: TTSProvider | None = None,
) -> RenderJob:
    job = db.get(RenderJob, render_job_id)
    if not job:
        raise AppError(code="RENDER_JOB_NOT_FOUND", message="Render job not found", status_code=404)

    job.status = JobState.RUNNING.value
    db.commit()

    ep = get_episode(db, job.episode_id)
    segments = list(
        db.scalars(
            select(ScriptSegment)
            .where(ScriptSegment.episode_id == ep.id)
            .order_by(ScriptSegment.order_no.asc())
        ).all()
    )
    active_segs = [s for s in segments if s.review_status != SegmentReviewStatus.SKIP.value]
    by_char, by_narrator = _resolve_active_voice_profiles(db, ep.novel_id)
    tts = tts_provider or get_tts_provider(settings)

    completed = 0
    cached_hits = 0
    failed = 0
    errors: list[str] = []

    for seg in active_segs:
        vp = (
            by_char.get(seg.speaker_character_id)
            if seg.speaker_character_id
            else by_narrator.get(seg.narrator_key or "default")
        )
        if not vp or not vp.voice_id:
            failed += 1
            errors.append(f"Segment #{seg.order_no}: missing locked voice_id")
            continue

        try:
            _aud, is_cache = _synthesize_single_segment(
                db, storage, settings, tts, seg=seg, vp=vp, render_job=job
            )
            completed += 1
            if is_cache:
                cached_hits += 1
            db.commit()
        except TTSProviderError as exc:
            failed += 1
            errors.append(f"Segment #{seg.order_no}: {exc.error_type}")
            db.add(
                ProviderCall(
                    novel_id=seg.novel_id,
                    render_job_id=job.id,
                    provider=tts.provider_name,
                    model=settings.cosyvoice_model,
                    operation="synthesize",
                    input_chars=len(seg.text_content),
                    status="error",
                    error_type=exc.error_type,
                )
            )
            db.add(
                AudioSegment(
                    novel_id=seg.novel_id,
                    episode_id=seg.episode_id,
                    script_segment_id=seg.id,
                    render_job_id=job.id,
                    voice_profile_id=vp.id,
                    voice_profile_version=vp.profile_version,
                    voice_id=vp.voice_id or "",
                    is_current=False,
                    status=AudioSegmentStatus.FAILED.value,
                    error_message=str(exc)[:500],
                )
            )
            db.commit()
            if exc.error_type == "BUDGET_LIMIT_REACHED":
                job.status = JobState.PAUSED.value
                job.error_summary = str(exc)
                break
        except Exception as exc:
            logger.exception("Render failed for segment %s", seg.id)
            failed += 1
            errors.append(f"Segment #{seg.order_no}: {type(exc).__name__}")
            db.commit()

    job.progress_json = {
        "total_segments": len(active_segs),
        "completed_segments": completed,
        "cached_segments": cached_hits,
        "failed_segments": failed,
    }
    if job.status == JobState.RUNNING.value:
        if failed == 0:
            job.status = JobState.COMPLETED.value
            ep.status = EpisodeStatus.AUDIO_READY.value
        elif completed > 0:
            job.status = JobState.PARTIAL_FAILED.value
            job.error_summary = "; ".join(errors[:5])
        else:
            job.status = JobState.FAILED.value
            job.error_summary = "; ".join(errors[:5])
    db.commit()
    db.refresh(job)
    return job


def regenerate_script_segment(
    db: Session,
    storage: LocalStorage,
    settings: Settings,
    *,
    segment_id: str,
    force: bool = True,
) -> AudioSegment:
    """
    AUDIO-01: Regenerate audio for ONLY this single ScriptSegment.
    All other segments' audio files and checksums remain untouched.
    """
    seg = db.get(ScriptSegment, segment_id)
    if not seg:
        raise AppError(code="SEGMENT_NOT_FOUND", message="Script segment not found", status_code=404)

    by_char, by_narrator = _resolve_active_voice_profiles(db, seg.novel_id)
    vp = (
        by_char.get(seg.speaker_character_id)
        if seg.speaker_character_id
        else by_narrator.get(seg.narrator_key or "default")
    )
    if not vp or not vp.voice_id:
        raise AppError(
            code="MISSING_LOCKED_VOICE",
            message="Speaker for this segment does not have a locked voice profile",
            status_code=400,
        )

    tts = get_tts_provider(settings)
    aud, _cached = _synthesize_single_segment(
        db, storage, settings, tts, seg=seg, vp=vp, render_job=None, force_regenerate=force
    )
    db.commit()
    db.refresh(aud)
    return aud


def get_render_job(db: Session, job_id: str) -> RenderJob:
    job = db.get(RenderJob, job_id)
    if not job:
        raise AppError(code="RENDER_JOB_NOT_FOUND", message="Render job not found", status_code=404)
    return job


def list_render_jobs(db: Session, episode_id: str) -> list[RenderJob]:
    get_episode(db, episode_id)
    return list(
        db.scalars(
            select(RenderJob)
            .where(RenderJob.episode_id == episode_id)
            .order_by(RenderJob.created_at.desc())
        ).all()
    )


def get_audio_segment_file(db: Session, storage: LocalStorage, audio_id: str) -> tuple[AudioSegment, Path]:
    aud = db.get(AudioSegment, audio_id)
    if not aud or not aud.audio_ref:
        raise AppError(code="AUDIO_NOT_FOUND", message="Audio segment not found", status_code=404)
    return aud, storage.get_audio_path(aud.audio_ref)


def get_provider_usage_summary(db: Session, novel_id: str) -> dict:
    rows = db.execute(
        select(
            ProviderCall.provider,
            ProviderCall.model,
            ProviderCall.operation,
            func.count(ProviderCall.id),
            func.coalesce(func.sum(ProviderCall.input_chars), 0),
            func.coalesce(func.sum(ProviderCall.audio_duration_ms), 0),
            func.coalesce(func.sum(ProviderCall.actual_cost), 0.0),
        )
        .where(ProviderCall.novel_id == novel_id)
        .group_by(ProviderCall.provider, ProviderCall.model, ProviderCall.operation)
    ).all()

    items = []
    total_chars = 0
    total_ms = 0
    total_cost = 0.0
    for prov, model, op_name, cnt, chars, dur_ms, cost in rows:
        chars = int(chars)
        dur_ms = int(dur_ms)
        cost = round(float(cost), 6)
        total_chars += chars
        total_ms += dur_ms
        total_cost += cost
        items.append(
            {
                "provider": prov,
                "model": model,
                "operation": op_name,
                "call_count": int(cnt),
                "input_chars": chars,
                "audio_duration_ms": dur_ms,
                "actual_cost": cost,
            }
        )
    return {
        "novel_id": novel_id,
        "total_input_chars": total_chars,
        "total_audio_duration_ms": total_ms,
        "total_actual_cost": round(total_cost, 6),
        "by_operation": items,
    }
