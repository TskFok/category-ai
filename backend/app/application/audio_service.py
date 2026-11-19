from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.application.episode_service import get_episode, get_episode_plan, list_episodes
from app.application.script_service import SPEAKER_REQUIRED_TYPES, compute_is_render_blocked
from app.core.config import Settings
from app.core.errors import AppError
from app.domain.enums import (
    AudioSegmentStatus,
    EpisodeStatus,
    JobState,
    ReviewStatus,
    SegmentReviewStatus,
    SynthesisUnitStatus,
    VoiceApprovalStatus,
)
from app.domain.script_validation import split_text_to_synthesis_units
from app.domain.text_processing import sha256_bytes
from app.domain.voice_profile import estimate_tts_cost, make_audio_cache_key
from app.infrastructure.audio.ffmpeg_pipeline import inspect_wav_bytes, merge_wav_bytes_list
from app.infrastructure.db.models import (
    AudioSegment,
    BookRenderJob,
    Character,
    Episode,
    EpisodePlan,
    Novel,
    ProviderCall,
    RenderJob,
    ScriptSegment,
    SynthesisUnit,
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


def _find_unresolved_segments(
    db: Session,
    ep: Episode,
    segments: list[ScriptSegment],
    settings: Settings,
) -> list[dict]:
    """
    P0-04: Identify DIALOGUE / INNER_MONOLOGUE segments that are unresolved,
    needs_review, or bound to an invalidated character. Never silently route them to narrator.
    """
    valid_char_ids = set(
        db.scalars(
            select(Character.id).where(
                Character.novel_id == ep.novel_id,
                Character.status != ReviewStatus.SUPERSEDED.value,
            )
        ).all()
    )
    blockers: list[dict] = []
    for seg in segments:
        if seg.review_status == SegmentReviewStatus.SKIP.value:
            continue
        if seg.segment_type in SPEAKER_REQUIRED_TYPES:
            char_invalid = bool(seg.speaker_character_id and seg.speaker_character_id not in valid_char_ids)
            is_blocked = char_invalid or compute_is_render_blocked(
                segment_type=seg.segment_type,
                speaker_character_id=seg.speaker_character_id,
                narrator_key=seg.narrator_key,
                review_status=seg.review_status,
                manual_override=seg.manual_override,
                allow_low_confidence_auto_pass=settings.allow_low_confidence_auto_pass,
            )
            if is_blocked:
                blockers.append(
                    {
                        "segment_id": seg.id,
                        "order_no": seg.order_no,
                        "segment_type": seg.segment_type,
                        "review_status": seg.review_status,
                        "text_preview": seg.text_content[:80],
                        "reason": (
                            "角色已失效/已合并"
                            if char_invalid
                            else "未确认说话人（对话不可静默配为旁白）"
                        ),
                    }
                )
    return blockers


def _resolve_voice_for_segment(
    seg: ScriptSegment,
    by_char: dict[str, VoiceProfile],
    by_narrator: dict[str, VoiceProfile],
    settings: Settings,
) -> VoiceProfile | None:
    """P0-04: Strictly route DIALOGUE/INNER_MONOLOGUE vs NARRATION/SCENE_HEADING."""
    if seg.segment_type in SPEAKER_REQUIRED_TYPES:
        if compute_is_render_blocked(
            segment_type=seg.segment_type,
            speaker_character_id=seg.speaker_character_id,
            narrator_key=seg.narrator_key,
            review_status=seg.review_status,
            manual_override=seg.manual_override,
            allow_low_confidence_auto_pass=settings.allow_low_confidence_auto_pass,
        ):
            return None
        if seg.speaker_character_id:
            return by_char.get(seg.speaker_character_id)
        if seg.manual_override and seg.narrator_key:
            return by_narrator.get(seg.narrator_key)
        return None
    # NARRATION / SCENE_HEADING
    if seg.speaker_character_id:
        return by_char.get(seg.speaker_character_id)
    return by_narrator.get(seg.narrator_key or "default")


def _check_missing_voices_for_episode(
    db: Session,
    ep: Episode,
    segments: list[ScriptSegment],
    settings: Settings,
) -> list[str]:
    by_char, by_narrator = _resolve_active_voice_profiles(db, ep.novel_id)
    missing: list[str] = []
    seen_keys: set[str] = set()

    for seg in segments:
        if seg.review_status == SegmentReviewStatus.SKIP.value:
            continue
        if seg.segment_type in SPEAKER_REQUIRED_TYPES:
            if seg.speaker_character_id:
                if seg.speaker_character_id not in by_char and seg.speaker_character_id not in seen_keys:
                    seen_keys.add(seg.speaker_character_id)
                    char = db.get(Character, seg.speaker_character_id)
                    missing.append(f"character:{char.canonical_name if char else seg.speaker_character_id}")
            elif seg.manual_override and seg.narrator_key:
                nk = seg.narrator_key
                if nk not in by_narrator and f"narrator:{nk}" not in seen_keys:
                    seen_keys.add(f"narrator:{nk}")
                    missing.append(f"narrator:{nk}")
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
    book_render_job_id: str | None = None,
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

    # P0-04: Block rendering if ANY dialogue/inner_monologue speaker is unresolved / needs_review
    unresolved = _find_unresolved_segments(db, ep, segments, settings)
    if unresolved:
        raise AppError(
            code="UNRESOLVED_SPEAKER_SEGMENTS",
            message=(
                f"Episode {ep.episode_no} has {len(unresolved)} unresolved dialogue/inner-monologue "
                "segment(s); confirm speaker attribution before rendering"
            ),
            status_code=409,
            details={"blocking_segments": unresolved[:50]},
        )

    # VOICE-01: Block synthesis if any active speaker/narrator lacks a locked voice_id
    missing = _check_missing_voices_for_episode(db, ep, segments, settings)
    if missing:
        raise AppError(
            code="MISSING_LOCKED_VOICES",
            message=f"All speakers must have a locked voice_id before rendering. Missing: {', '.join(missing)}",
            status_code=400,
            details={"missing_voices": missing},
        )

    by_char, by_narrator = _resolve_active_voice_profiles(db, ep.novel_id)
    snapshot = {
        "characters": {
            cid: {
                "profile_id": vp.id,
                "voice_id": vp.voice_id,
                "version": vp.profile_version,
                "target_model": vp.target_model,
            }
            for cid, vp in by_char.items()
        },
        "narrators": {
            nk: {
                "profile_id": vp.id,
                "voice_id": vp.voice_id,
                "version": vp.profile_version,
                "target_model": vp.target_model,
            }
            for nk, vp in by_narrator.items()
        },
    }
    novel = db.get(Novel, ep.novel_id)
    active_segs = [s for s in segments if s.review_status != SegmentReviewStatus.SKIP.value]

    job = RenderJob(
        novel_id=ep.novel_id,
        episode_id=ep.id,
        book_render_job_id=book_render_job_id,
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
            "currency": settings.tts_currency,
        },
        idempotency_key=idempotency_key,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def _ensure_synthesis_units(
    db: Session,
    seg: ScriptSegment,
    max_chars: int,
) -> list[SynthesisUnit]:
    """P0-06: Create or synchronize SynthesisUnits for a ScriptSegment."""
    raw_units = split_text_to_synthesis_units(seg.text_content, max_chars)
    existing_units = list(
        db.scalars(
            select(SynthesisUnit)
            .where(SynthesisUnit.script_segment_id == seg.id)
            .order_by(SynthesisUnit.unit_index.asc())
        ).all()
    )
    if (
        len(existing_units) == len(raw_units)
        and all(
            eu.unit_index == ru.unit_index and eu.text_hash == ru.text_hash
            for eu, ru in zip(existing_units, raw_units, strict=True)
        )
    ):
        return existing_units

    # Rebuild units when text/max_chars changed
    if existing_units:
        db.execute(delete(SynthesisUnit).where(SynthesisUnit.script_segment_id == seg.id))
        db.flush()

    created: list[SynthesisUnit] = []
    for ru in raw_units:
        unit = SynthesisUnit(
            novel_id=seg.novel_id,
            episode_id=seg.episode_id,
            script_segment_id=seg.id,
            unit_index=ru.unit_index,
            start_offset=ru.start_offset,
            end_offset=ru.end_offset,
            text_content=ru.text_content,
            text_hash=ru.text_hash,
            status=SynthesisUnitStatus.PENDING.value,
            attempt=0,
        )
        db.add(unit)
        created.append(unit)
    db.flush()
    return created


def _check_job_interrupt(db: Session, render_job: RenderJob | None) -> str | None:
    """P1-01: Refresh job state from DB before paid TTS calls to honor pause/cancel."""
    if render_job is None:
        return None
    db.refresh(render_job)
    if render_job.cancel_requested or render_job.status == JobState.CANCELLED.value:
        return "CANCELLED"
    if render_job.paused_at is not None or render_job.status == JobState.PAUSED.value:
        return "PAUSED"
    return None


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
    Synthesize one ScriptSegment via SynthesisUnits (P0-06) with cache reuse (TTS-01),
    pause/cancel guard (P1-01), and budget guard (OPS-01).
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
                if (
                    cached.script_segment_id == seg.id
                    and cached.is_current
                    and cached.voice_profile_version == vp.profile_version
                    and cached.script_revision == seg.revision
                ):
                    return cached, True
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
                    script_revision=seg.revision,
                    render_input_hash=cache_key,
                    voice_id=vp.voice_id,
                    audio_ref=cached.audio_ref,
                    audio_format=cached.audio_format,
                    sample_rate=cached.sample_rate,
                    channels=cached.channels,
                    duration_ms=cached.duration_ms,
                    file_size_bytes=cached.file_size_bytes,
                    checksum=cached.checksum,
                    cache_key=cache_key,
                    is_current=True,
                    is_mock=cached.is_mock,
                    provider=cached.provider,
                    model=cached.model,
                    status=AudioSegmentStatus.READY.value,
                )
                db.add(reused)
                db.flush()
                return reused, True
            except AppError:
                pass  # file missing on disk; synthesize fresh

    # P0-06: Split long segments into TTS-safe SynthesisUnits
    eff_max_chars = max(50, settings.tts_max_chars_per_request - settings.tts_unit_safety_margin)
    units = _ensure_synthesis_units(db, seg, eff_max_chars)
    unit_wav_bytes: list[bytes] = []
    all_units_cached = True

    for unit in units:
        unit_cache_key = make_audio_cache_key(
            text_hash=unit.text_hash,
            voice_id=vp.voice_id,
            provider=tts.provider_name,
            target_model=vp.target_model,
            emotion=seg.emotion,
            intensity=seg.emotion_intensity,
            rate=eff_rate,
            pitch=eff_pitch,
            instruction=instruction,
        )

        # Reuse completed unit on disk if parameters & cache_key match
        if (
            not force_regenerate
            and unit.status == SynthesisUnitStatus.COMPLETED.value
            and unit.cache_key == unit_cache_key
            and unit.audio_ref
        ):
            try:
                u_path = storage.get_audio_path(unit.audio_ref)
                unit_wav_bytes.append(u_path.read_bytes())
                continue
            except AppError:
                pass

        # P1-01: Check pause/cancel before every paid TTS API call
        interrupt = _check_job_interrupt(db, render_job)
        if interrupt == "CANCELLED":
            raise TTSProviderError("JOB_CANCELLED", "Render job cancelled by user", retryable=False)
        if interrupt == "PAUSED":
            raise TTSProviderError("JOB_PAUSED", "Render job paused by user", retryable=False)

        # OPS-01: Budget guard before EVERY paid TTS call
        if render_job and render_job.budget_limit > 0 and render_job.spent_cost >= render_job.budget_limit:
            raise TTSProviderError(
                "BUDGET_LIMIT_REACHED",
                f"Render job budget limit reached ({render_job.spent_cost:.4f} >= {render_job.budget_limit:.4f} {settings.tts_currency})",
            )

        all_units_cached = False
        unit.status = SynthesisUnitStatus.RUNNING.value
        unit.attempt += 1
        db.flush()

        req = SynthesisRequest(
            text=unit.text_content,
            voice_id=vp.voice_id,
            emotion=seg.emotion,
            intensity=seg.emotion_intensity,
            rate=eff_rate,
            pitch=eff_pitch,
            instruction=instruction,
            output_format="wav",
        )
        try:
            result = tts.synthesize(req)
        except TTSProviderError as exc:
            unit.status = SynthesisUnitStatus.FAILED.value
            unit.last_error = f"{exc.error_type}: {exc}"[:500]
            if exc.retryable:
                unit.next_retry_at = datetime.now(timezone.utc) + timedelta(seconds=min(60, 2 ** unit.attempt))
            db.flush()
            raise

        u_checksum = sha256_bytes(result.audio_bytes)
        u_ref, _u_size = storage.save_audio_bytes(
            f"{seg.novel_id}/episodes/{seg.episode_id}/units/{seg.id}_u{unit.unit_index}_{u_checksum[:10]}.{result.audio_format}",
            result.audio_bytes,
        )
        unit.status = SynthesisUnitStatus.COMPLETED.value
        unit.last_error = None
        unit.next_retry_at = None
        unit.audio_ref = u_ref
        unit.audio_format = result.audio_format
        unit.duration_ms = result.duration_ms
        unit.checksum = u_checksum
        unit.cache_key = unit_cache_key
        unit_wav_bytes.append(result.audio_bytes)

        db.add(
            ProviderCall(
                novel_id=seg.novel_id,
                render_job_id=render_job.id if render_job else None,
                provider=result.provider,
                model=result.model,
                operation="synthesize",
                request_id=result.request_id,
                input_chars=result.input_chars,
                billed_characters=result.billed_characters,
                audio_duration_ms=result.duration_ms,
                estimated_cost=result.estimated_cost,
                actual_cost=result.actual_cost,
                currency=result.currency,
                cost_kind=result.cost_kind,
                provider_request_status="ok",
                is_mock=result.is_mock,
                latency_ms=result.latency_ms,
                attempt=result.attempts,
                status="ok",
            )
        )
        if render_job:
            render_job.spent_cost = round(render_job.spent_cost + result.actual_cost, 6)
        db.flush()

    # Merge unit WAVs into normalized segment-level WAV
    final_wav_bytes, total_duration_ms = merge_wav_bytes_list(
        unit_wav_bytes,
        sample_rate=settings.audio_sample_rate,
        gap_ms=120,
    )
    meta = inspect_wav_bytes(final_wav_bytes)
    checksum = sha256_bytes(final_wav_bytes)
    rel_ref, size_bytes = storage.save_audio_bytes(
        f"{seg.novel_id}/episodes/{seg.episode_id}/{seg.id}_{checksum[:12]}.wav",
        final_wav_bytes,
    )

    # Atomically mark previous current AudioSegments as non-current
    for old in db.scalars(
        select(AudioSegment).where(
            AudioSegment.script_segment_id == seg.id,
            AudioSegment.is_current.is_(True),
        )
    ).all():
        old.is_current = False
    db.flush()

    aud = AudioSegment(
        novel_id=seg.novel_id,
        episode_id=seg.episode_id,
        script_segment_id=seg.id,
        render_job_id=render_job.id if render_job else None,
        voice_profile_id=vp.id,
        voice_profile_version=vp.profile_version,
        script_revision=seg.revision,
        render_input_hash=cache_key,
        voice_id=vp.voice_id,
        audio_ref=rel_ref,
        audio_format="wav",
        sample_rate=meta.sample_rate,
        channels=meta.channels,
        duration_ms=max(total_duration_ms, meta.duration_ms),
        file_size_bytes=size_bytes,
        checksum=checksum,
        cache_key=cache_key,
        is_current=True,
        is_mock=tts.is_mock,
        provider=tts.provider_name,
        model=vp.target_model,
        status=AudioSegmentStatus.READY.value,
    )
    db.add(aud)
    db.flush()
    return aud, all_units_cached


def execute_render_job(
    db: Session,
    storage: LocalStorage,
    settings: Settings,
    render_job_id: str,
    tts_provider: TTSProvider | None = None,
    only_failed: bool = False,
) -> RenderJob:
    job = db.get(RenderJob, render_job_id)
    if not job:
        raise AppError(code="RENDER_JOB_NOT_FOUND", message="Render job not found", status_code=404)

    if job.cancel_requested or job.status == JobState.CANCELLED.value:
        job.status = JobState.CANCELLED.value
        db.commit()
        return job

    job.status = JobState.RUNNING.value
    job.paused_at = None
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

    try:
        tts = tts_provider or get_tts_provider(settings)
    except TTSProviderError as exc:
        job.status = JobState.FAILED.value
        job.error_summary = f"{exc.error_type}: {exc}"
        db.commit()
        return job

    completed = 0
    cached_hits = 0
    failed = 0
    errors: list[str] = []

    for idx, seg in enumerate(active_segs):
        # P1-01: Check pause/cancel before each segment
        interrupt = _check_job_interrupt(db, job)
        if interrupt == "CANCELLED":
            job.status = JobState.CANCELLED.value
            job.error_summary = "Cancelled by user"
            break
        if interrupt == "PAUSED":
            job.status = JobState.PAUSED.value
            job.resume_cursor = idx
            break

        # If retrying only failed segments, skip already READY current segments
        if only_failed:
            existing_cur = db.scalar(
                select(AudioSegment).where(
                    AudioSegment.script_segment_id == seg.id,
                    AudioSegment.is_current.is_(True),
                    AudioSegment.status == AudioSegmentStatus.READY.value,
                )
            )
            if existing_cur:
                completed += 1
                cached_hits += 1
                continue

        vp = _resolve_voice_for_segment(seg, by_char, by_narrator, settings)
        if not vp or not vp.voice_id:
            failed += 1
            errors.append(f"Segment #{seg.order_no}: unresolved speaker or missing locked voice_id")
            continue

        try:
            _aud, is_cache = _synthesize_single_segment(
                db, storage, settings, tts, seg=seg, vp=vp, render_job=job
            )
            completed += 1
            if is_cache:
                cached_hits += 1
            job.resume_cursor = idx + 1
            job.progress_json = {
                "total_segments": len(active_segs),
                "completed_segments": completed,
                "cached_segments": cached_hits,
                "failed_segments": failed,
                "remaining_segments": max(0, len(active_segs) - completed - failed),
                "currency": settings.tts_currency,
            }
            db.commit()
        except TTSProviderError as exc:
            if exc.error_type == "JOB_CANCELLED":
                job.status = JobState.CANCELLED.value
                job.error_summary = "Cancelled by user"
                db.commit()
                break
            if exc.error_type == "JOB_PAUSED":
                job.status = JobState.PAUSED.value
                job.resume_cursor = idx
                db.commit()
                break
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
                    currency=settings.tts_currency,
                    cost_kind="estimated",
                    is_mock=tts.is_mock,
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
                    script_revision=seg.revision,
                    voice_id=vp.voice_id or "",
                    is_current=False,
                    is_mock=tts.is_mock,
                    provider=tts.provider_name,
                    model=vp.target_model,
                    status=AudioSegmentStatus.FAILED.value,
                    error_message=str(exc)[:500],
                )
            )
            db.commit()
            if exc.error_type == "BUDGET_LIMIT_REACHED":
                job.status = JobState.PAUSED.value
                job.paused_at = datetime.now(timezone.utc)
                job.resume_cursor = idx
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
        "remaining_segments": max(0, len(active_segs) - completed - failed),
        "currency": settings.tts_currency,
    }
    if job.status == JobState.RUNNING.value:
        if failed == 0:
            job.status = JobState.COMPLETED.value
            ep.status = EpisodeStatus.AUDIO_READY.value
            job.error_summary = None
        elif completed > 0:
            job.status = JobState.PARTIAL_FAILED.value
            job.error_summary = "; ".join(errors[:5])
        else:
            job.status = JobState.FAILED.value
            job.error_summary = "; ".join(errors[:5])
    db.commit()
    db.refresh(job)
    return job


def pause_render_job(db: Session, job_id: str) -> RenderJob:
    job = get_render_job(db, job_id)
    if job.status in (JobState.COMPLETED.value, JobState.CANCELLED.value):
        raise AppError(
            code="INVALID_JOB_TRANSITION",
            message=f"Cannot pause render job in state '{job.status}'",
            status_code=409,
        )
    job.status = JobState.PAUSED.value
    job.paused_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(job)
    return job


def cancel_render_job(db: Session, job_id: str) -> RenderJob:
    job = get_render_job(db, job_id)
    if job.status == JobState.COMPLETED.value:
        raise AppError(
            code="INVALID_JOB_TRANSITION",
            message="Cannot cancel an already completed render job",
            status_code=409,
        )
    job.cancel_requested = True
    job.status = JobState.CANCELLED.value
    db.commit()
    db.refresh(job)
    return job


def resume_render_job(db: Session, job_id: str) -> RenderJob:
    job = get_render_job(db, job_id)
    if job.status not in (JobState.PAUSED.value, JobState.PARTIAL_FAILED.value, JobState.FAILED.value):
        raise AppError(
            code="INVALID_JOB_TRANSITION",
            message=f"Cannot resume render job in state '{job.status}'",
            status_code=409,
        )
    job.status = JobState.PENDING.value
    job.paused_at = None
    job.cancel_requested = False
    db.commit()
    db.refresh(job)
    return job


def retry_failed_render_job(db: Session, job_id: str) -> RenderJob:
    job = get_render_job(db, job_id)
    if job.status not in (JobState.PARTIAL_FAILED.value, JobState.FAILED.value, JobState.PAUSED.value):
        raise AppError(
            code="INVALID_JOB_TRANSITION",
            message=f"Cannot retry failed units for render job in state '{job.status}'",
            status_code=409,
        )
    # Reset ONLY failed SynthesisUnits in this episode to PENDING
    for unit in db.scalars(
        select(SynthesisUnit).where(
            SynthesisUnit.episode_id == job.episode_id,
            SynthesisUnit.status == SynthesisUnitStatus.FAILED.value,
        )
    ).all():
        unit.status = SynthesisUnitStatus.PENDING.value
        unit.last_error = None
        unit.next_retry_at = None

    job.status = JobState.PENDING.value
    job.paused_at = None
    job.cancel_requested = False
    job.error_summary = None
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
    vp = _resolve_voice_for_segment(seg, by_char, by_narrator, settings)
    if not vp or not vp.voice_id:
        if seg.segment_type in SPEAKER_REQUIRED_TYPES and compute_is_render_blocked(
            segment_type=seg.segment_type,
            speaker_character_id=seg.speaker_character_id,
            narrator_key=seg.narrator_key,
            review_status=seg.review_status,
            manual_override=seg.manual_override,
            allow_low_confidence_auto_pass=settings.allow_low_confidence_auto_pass,
        ):
            raise AppError(
                code="UNRESOLVED_SPEAKER_SEGMENT",
                message="Cannot regenerate dialogue/inner-monologue segment without confirmed speaker",
                status_code=409,
            )
        raise AppError(
            code="MISSING_LOCKED_VOICE",
            message="Speaker for this segment does not have a locked voice profile",
            status_code=400,
        )

    try:
        tts = get_tts_provider(settings)
    except TTSProviderError as exc:
        raise AppError(code=exc.error_type, message=str(exc), status_code=503) from exc

    aud, _cached = _synthesize_single_segment(
        db, storage, settings, tts, seg=seg, vp=vp, render_job=None, force_regenerate=force
    )
    db.commit()
    db.refresh(aud)
    return aud


def create_book_render_job(
    db: Session,
    settings: Settings,
    *,
    plan_id: str,
    episode_ids: list[str] | None = None,
    budget_limit: float | None = None,
) -> BookRenderJob:
    """P1-02: Create a whole-book batch render job for an EpisodePlan."""
    plan = get_episode_plan(db, plan_id)
    episodes = list_episodes(db, plan.id)
    if episode_ids:
        ep_set = set(episode_ids)
        episodes = [e for e in episodes if e.id in ep_set]
    if not episodes:
        raise AppError(code="NO_EPISODES", message="No episodes selected for book rendering", status_code=400)

    eff_budget = budget_limit if budget_limit is not None else settings.tts_daily_budget_limit
    book_job = BookRenderJob(
        novel_id=plan.novel_id,
        plan_id=plan.id,
        status=JobState.PENDING.value,
        episode_ids_json=[e.id for e in episodes],
        budget_limit=eff_budget,
        spent_cost=0.0,
        currency=settings.tts_currency,
        progress_json={
            "total_episodes": len(episodes),
            "completed_episodes": 0,
            "failed_episodes": 0,
            "current_episode_no": None,
        },
    )
    db.add(book_job)
    db.commit()
    db.refresh(book_job)
    return book_job


def execute_book_render_job(
    db: Session,
    storage: LocalStorage,
    settings: Settings,
    book_job_id: str,
    tts_provider: TTSProvider | None = None,
) -> BookRenderJob:
    book_job = db.get(BookRenderJob, book_job_id)
    if not book_job:
        raise AppError(code="BOOK_RENDER_JOB_NOT_FOUND", message="Book render job not found", status_code=404)

    book_job.status = JobState.RUNNING.value
    db.commit()

    episode_ids: list[str] = list(book_job.episode_ids_json or [])
    completed_eps = 0
    failed_eps = 0
    errors: list[str] = []

    for ep_id in episode_ids:
        db.refresh(book_job)
        if book_job.cancel_requested or book_job.status == JobState.CANCELLED.value:
            book_job.status = JobState.CANCELLED.value
            break
        if book_job.paused_at is not None or book_job.status == JobState.PAUSED.value:
            book_job.status = JobState.PAUSED.value
            break

        remaining_budget = (
            max(0.0, book_job.budget_limit - book_job.spent_cost)
            if book_job.budget_limit > 0
            else 0.0
        )
        if book_job.budget_limit > 0 and remaining_budget <= 0:
            book_job.status = JobState.PAUSED.value
            book_job.paused_at = datetime.now(timezone.utc)
            book_job.error_summary = "Book render budget limit reached"
            break

        ep = db.get(Episode, ep_id)
        if not ep:
            failed_eps += 1
            errors.append(f"Episode {ep_id}: not found")
            continue

        book_job.progress_json = {
            "total_episodes": len(episode_ids),
            "completed_episodes": completed_eps,
            "failed_episodes": failed_eps,
            "current_episode_no": ep.episode_no,
        }
        db.commit()

        try:
            rj = create_render_job(
                db,
                settings,
                episode_id=ep.id,
                budget_limit=remaining_budget if book_job.budget_limit > 0 else 0.0,
                book_render_job_id=book_job.id,
            )
            rj = execute_render_job(db, storage, settings, rj.id, tts_provider=tts_provider)
            book_job.spent_cost = round(book_job.spent_cost + rj.spent_cost, 6)
            if rj.status == JobState.COMPLETED.value:
                completed_eps += 1
            else:
                failed_eps += 1
                errors.append(f"Ep {ep.episode_no}: {rj.status} ({rj.error_summary or ''})")
        except AppError as exc:
            failed_eps += 1
            errors.append(f"Ep {ep.episode_no}: {exc.code} - {exc.message}")
        db.commit()

    book_job.progress_json = {
        "total_episodes": len(episode_ids),
        "completed_episodes": completed_eps,
        "failed_episodes": failed_eps,
        "current_episode_no": None,
    }
    if book_job.status == JobState.RUNNING.value:
        if failed_eps == 0:
            book_job.status = JobState.COMPLETED.value
            book_job.error_summary = None
        elif completed_eps > 0:
            book_job.status = JobState.PARTIAL_FAILED.value
            book_job.error_summary = "; ".join(errors[:5])
        else:
            book_job.status = JobState.FAILED.value
            book_job.error_summary = "; ".join(errors[:5])
    db.commit()
    db.refresh(book_job)
    return book_job


def get_book_render_job(db: Session, book_job_id: str) -> dict:
    book_job = db.get(BookRenderJob, book_job_id)
    if not book_job:
        raise AppError(code="BOOK_RENDER_JOB_NOT_FOUND", message="Book render job not found", status_code=404)
    child_jobs = list(
        db.scalars(
            select(RenderJob)
            .where(RenderJob.book_render_job_id == book_job.id)
            .order_by(RenderJob.created_at.asc())
        ).all()
    )
    return {
        "id": book_job.id,
        "novel_id": book_job.novel_id,
        "plan_id": book_job.plan_id,
        "status": book_job.status,
        "episode_ids": book_job.episode_ids_json,
        "budget_limit": book_job.budget_limit,
        "spent_cost": book_job.spent_cost,
        "currency": book_job.currency,
        "progress_json": book_job.progress_json,
        "error_summary": book_job.error_summary,
        "episode_jobs": [
            {
                "id": cj.id,
                "episode_id": cj.episode_id,
                "status": cj.status,
                "spent_cost": cj.spent_cost,
                "progress_json": cj.progress_json,
                "error_summary": cj.error_summary,
            }
            for cj in child_jobs
        ],
        "created_at": book_job.created_at,
        "updated_at": book_job.updated_at,
    }


def get_novel_audio_readiness(
    db: Session,
    settings: Settings,
    novel_id: str,
    plan_id: str | None = None,
) -> dict:
    """Section 9: Pre-export audio readiness & remaining cost report for a novel."""
    novel = db.get(Novel, novel_id)
    if not novel:
        raise AppError(code="NOVEL_NOT_FOUND", message="Novel not found", status_code=404)

    if plan_id:
        plan = get_episode_plan(db, plan_id)
    else:
        plan = db.scalar(
            select(EpisodePlan)
            .where(EpisodePlan.novel_id == novel.id)
            .order_by(EpisodePlan.plan_version.desc())
            .limit(1)
        )
    if not plan:
        return {
            "novel_id": novel.id,
            "plan_id": None,
            "ready_for_export": False,
            "total_episodes": 0,
            "ready_episodes": 0,
            "remaining_chars": 0,
            "estimated_remaining_cost": 0.0,
            "currency": settings.tts_currency,
            "episodes": [],
        }

    episodes = list_episodes(db, plan.id)
    by_char, by_narrator = _resolve_active_voice_profiles(db, novel.id)
    ep_reports = []
    ready_eps = 0
    total_remaining_chars = 0

    for ep in episodes:
        segs = list(
            db.scalars(
                select(ScriptSegment)
                .where(ScriptSegment.episode_id == ep.id)
                .order_by(ScriptSegment.order_no.asc())
            ).all()
        )
        active_segs = [s for s in segs if s.review_status != SegmentReviewStatus.SKIP.value]
        seg_ids = [s.id for s in active_segs]
        cur_audios = (
            {
                a.script_segment_id: a
                for a in db.scalars(
                    select(AudioSegment).where(
                        AudioSegment.script_segment_id.in_(seg_ids),
                        AudioSegment.is_current.is_(True),
                    )
                ).all()
            }
            if seg_ids
            else {}
        )
        unresolved = _find_unresolved_segments(db, ep, active_segs, settings)
        ready_cnt = 0
        stale_cnt = 0
        missing_cnt = 0
        rem_chars = 0

        for s in active_segs:
            aud = cur_audios.get(s.id)
            vp = _resolve_voice_for_segment(s, by_char, by_narrator, settings)
            if not aud or not aud.audio_ref:
                missing_cnt += 1
                rem_chars += len(s.text_content)
            elif (
                aud.status != AudioSegmentStatus.READY.value
                or aud.script_revision != s.revision
                or (vp and (aud.voice_id != vp.voice_id or aud.voice_profile_version != vp.profile_version))
            ):
                stale_cnt += 1
                rem_chars += len(s.text_content)
            else:
                ready_cnt += 1

        ep_ready = len(active_segs) > 0 and len(unresolved) == 0 and missing_cnt == 0 and stale_cnt == 0
        if ep_ready:
            ready_eps += 1
        total_remaining_chars += rem_chars
        ep_reports.append(
            {
                "episode_id": ep.id,
                "episode_no": ep.episode_no,
                "title": ep.title,
                "active_segments": len(active_segs),
                "ready_segments": ready_cnt,
                "stale_segments": stale_cnt,
                "missing_segments": missing_cnt,
                "unresolved_segments": len(unresolved),
                "remaining_chars": rem_chars,
                "ready_for_export": ep_ready,
            }
        )

    return {
        "novel_id": novel.id,
        "plan_id": plan.id,
        "ready_for_export": len(episodes) > 0 and ready_eps == len(episodes),
        "total_episodes": len(episodes),
        "ready_episodes": ready_eps,
        "remaining_chars": total_remaining_chars,
        "estimated_remaining_cost": estimate_tts_cost(
            total_remaining_chars, settings.tts_price_per_10k_chars
        ),
        "currency": settings.tts_currency,
        "episodes": ep_reports,
    }


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


def get_provider_usage_summary(db: Session, novel_id: str, settings: Settings | None = None) -> dict:
    rows = db.execute(
        select(
            ProviderCall.provider,
            ProviderCall.model,
            ProviderCall.operation,
            ProviderCall.currency,
            ProviderCall.cost_kind,
            func.count(ProviderCall.id),
            func.coalesce(func.sum(ProviderCall.input_chars), 0),
            func.coalesce(func.sum(ProviderCall.billed_characters), 0),
            func.coalesce(func.sum(ProviderCall.audio_duration_ms), 0),
            func.coalesce(func.sum(ProviderCall.estimated_cost), 0.0),
            func.coalesce(func.sum(ProviderCall.actual_cost), 0.0),
        )
        .where(ProviderCall.novel_id == novel_id)
        .group_by(
            ProviderCall.provider,
            ProviderCall.model,
            ProviderCall.operation,
            ProviderCall.currency,
            ProviderCall.cost_kind,
        )
    ).all()

    items = []
    total_chars = 0
    total_billed_chars = 0
    total_ms = 0
    total_est_cost = 0.0
    total_actual_cost = 0.0
    default_currency = settings.tts_currency if settings else "CNY"

    for prov, model, op_name, currency, cost_kind, cnt, chars, billed, dur_ms, est_c, act_c in rows:
        chars = int(chars)
        billed = int(billed)
        dur_ms = int(dur_ms)
        est_c = round(float(est_c), 6)
        act_c = round(float(act_c), 6)
        total_chars += chars
        total_billed_chars += billed
        total_ms += dur_ms
        total_est_cost += est_c
        total_actual_cost += act_c
        items.append(
            {
                "provider": prov,
                "model": model,
                "operation": op_name,
                "currency": currency or default_currency,
                "cost_kind": cost_kind or "estimated",
                "call_count": int(cnt),
                "input_chars": chars,
                "billed_characters": billed,
                "audio_duration_ms": dur_ms,
                "estimated_cost": est_c,
                "actual_cost": act_c,
            }
        )
    return {
        "novel_id": novel_id,
        "currency": default_currency,
        "total_input_chars": total_chars,
        "total_billed_characters": total_billed_chars,
        "total_audio_duration_ms": total_ms,
        "total_estimated_cost": round(total_est_cost, 6),
        "total_actual_cost": round(total_actual_cost, 6),
        "by_operation": items,
    }
