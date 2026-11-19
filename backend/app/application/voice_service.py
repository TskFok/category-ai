from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import AppError
from app.domain.enums import AudioSegmentStatus, VoiceApprovalStatus, VoiceProviderStatus
from app.domain.text_processing import sha256_bytes
from app.domain.voice_profile import estimate_tts_cost
from app.infrastructure.db.models import (
    Alias,
    AnalysisReport,
    AudioSegment,
    Character,
    LLMCall,
    Novel,
    ProviderCall,
    ScriptSegment,
    VoiceProfile,
    VoiceProfileVersion,
)
from app.infrastructure.llm.deepseek import DeepSeekClient, LLMClientError
from app.infrastructure.storage.local import LocalStorage
from app.infrastructure.tts.base import TTSProviderError
from app.infrastructure.tts.cosyvoice import get_tts_provider
from worker.pipelines.prompt_loader import load_prompt


def list_voice_profiles(db: Session, novel_id: str) -> list[VoiceProfile]:
    if not db.get(Novel, novel_id):
        raise AppError(code="NOVEL_NOT_FOUND", message="Novel not found", status_code=404)
    return list(
        db.scalars(
            select(VoiceProfile)
            .where(VoiceProfile.novel_id == novel_id)
            .order_by(VoiceProfile.created_at.asc())
        ).all()
    )


def get_voice_profile(db: Session, profile_id: str) -> VoiceProfile:
    vp = db.get(VoiceProfile, profile_id)
    if not vp:
        raise AppError(code="VOICE_PROFILE_NOT_FOUND", message="Voice profile not found", status_code=404)
    return vp


def _record_version_snapshot(db: Session, vp: VoiceProfile) -> VoiceProfileVersion:
    existing = db.scalar(
        select(VoiceProfileVersion).where(
            VoiceProfileVersion.voice_profile_id == vp.id,
            VoiceProfileVersion.version == vp.profile_version,
        )
    )
    if existing:
        existing.voice_id = vp.voice_id
        existing.target_model = vp.target_model
        existing.voice_prompt = vp.voice_prompt
        existing.default_rate = vp.default_rate
        existing.pitch = vp.pitch
        existing.provider_status = vp.provider_status
        existing.preview_checksum = vp.preview_checksum
        existing.approval_status = vp.approval_status
        return existing

    snap = VoiceProfileVersion(
        voice_profile_id=vp.id,
        novel_id=vp.novel_id,
        version=vp.profile_version,
        voice_id=vp.voice_id,
        target_model=vp.target_model,
        voice_prompt=vp.voice_prompt,
        default_rate=vp.default_rate,
        pitch=vp.pitch,
        provider_status=vp.provider_status,
        preview_checksum=vp.preview_checksum,
        approval_status=vp.approval_status,
    )
    db.add(snap)
    return snap


def _find_speaker_segments(db: Session, vp: VoiceProfile, episode_id: str | None = None) -> list[ScriptSegment]:
    if vp.character_id:
        stmt = select(ScriptSegment).where(
            ScriptSegment.novel_id == vp.novel_id,
            ScriptSegment.speaker_character_id == vp.character_id,
        )
    else:
        stmt = select(ScriptSegment).where(
            ScriptSegment.novel_id == vp.novel_id,
            ScriptSegment.narrator_key == vp.narrator_key,
        )
    if episode_id:
        stmt = stmt.where(ScriptSegment.episode_id == episode_id)
    return list(db.scalars(stmt).all())


def create_voice_profile(
    db: Session,
    settings: Settings,
    *,
    novel_id: str,
    character_id: str | None = None,
    narrator_key: str | None = None,
    display_name: str | None = None,
    voice_prompt: str = "",
    voice_id: str | None = None,
    language: str = "zh-CN",
    default_rate: float = 1.0,
    pitch: float = 1.0,
    seed: int | None = None,
    consent_confirmed: bool = False,
    consent_note: str | None = None,
) -> VoiceProfile:
    novel = db.get(Novel, novel_id)
    if not novel:
        raise AppError(code="NOVEL_NOT_FOUND", message="Novel not found", status_code=404)

    if bool(character_id) == bool(narrator_key):
        raise AppError(
            code="INVALID_VOICE_TARGET",
            message="Provide exactly one of character_id or narrator_key",
            status_code=400,
        )

    eff_name = (display_name or "").strip()
    if character_id:
        char = db.get(Character, character_id)
        if not char or char.novel_id != novel_id:
            raise AppError(code="CHARACTER_NOT_FOUND", message="Character not found in novel", status_code=404)
        eff_name = eff_name or char.canonical_name
        max_ver = (
            db.scalar(
                select(func.coalesce(func.max(VoiceProfile.profile_version), 0)).where(
                    VoiceProfile.novel_id == novel_id,
                    VoiceProfile.character_id == character_id,
                )
            )
            or 0
        )
    else:
        eff_name = eff_name or f"旁白 ({narrator_key})"
        max_ver = (
            db.scalar(
                select(func.coalesce(func.max(VoiceProfile.profile_version), 0)).where(
                    VoiceProfile.novel_id == novel_id,
                    VoiceProfile.narrator_key == narrator_key,
                )
            )
            or 0
        )

    vp = VoiceProfile(
        novel_id=novel_id,
        character_id=character_id,
        narrator_key=narrator_key,
        display_name=eff_name,
        provider=settings.tts_provider,
        target_model=settings.cosyvoice_model,
        voice_prompt=voice_prompt.strip()[:500],
        voice_id=voice_id.strip() if voice_id else None,
        language=language,
        default_rate=default_rate,
        pitch=pitch,
        seed=seed,
        profile_version=int(max_ver) + 1,
        approval_status=VoiceApprovalStatus.DRAFT.value,
        is_active=False,
        candidates_json=[],
        consent_confirmed=consent_confirmed,
        consent_note=consent_note,
        provider_status=VoiceProviderStatus.OK.value if voice_id else VoiceProviderStatus.UNKNOWN.value,
        prompt_version=1,
    )
    db.add(vp)
    db.flush()
    _record_version_snapshot(db, vp)
    db.commit()
    db.refresh(vp)
    return vp


def design_voice_candidates(
    db: Session,
    storage: LocalStorage,
    settings: Settings,
    *,
    profile_id: str,
    preview_text: str | None = None,
) -> VoiceProfile:
    """
    VOICE-01 & P0-02:
    1. Use DeepSeek to translate character profile/analysis into 2-3 voice design prompts.
    2. Call TTSProvider.design_voice (official voice-enrollment protocol) for each candidate.
    3. Save preview WAV files, record checksum & provider_status, and store candidates.
    """
    vp = get_voice_profile(db, profile_id)
    aliases_text = "无"
    summary_text = vp.voice_prompt or "待设计"
    analysis_text = "暂无深度分析报告（将按角色名与默认风格推荐）"

    if vp.character_id:
        char = db.get(Character, vp.character_id)
        if char:
            aliases = list(
                db.scalars(select(Alias.alias_text).where(Alias.character_id == char.id)).all()
            )
            aliases_text = ", ".join(aliases) or "无"
            summary_text = char.summary or summary_text
            latest_report = db.scalar(
                select(AnalysisReport)
                .where(AnalysisReport.character_id == char.id)
                .order_by(AnalysisReport.generation_version.desc())
                .limit(1)
            )
            if latest_report and latest_report.content_json:
                analysis_text = str(latest_report.content_json)[:1200]

    llm = DeepSeekClient(settings)
    spec = load_prompt("voice_design")
    candidates_raw: list[dict] = []
    try:
        resp = llm.chat_json(
            system_prompt=spec.system_prompt,
            user_prompt=spec.user_template.format(
                character_name=vp.display_name,
                aliases=aliases_text,
                summary=summary_text,
                analysis_text=analysis_text,
            ),
            tier=spec.model_tier,
            temperature=spec.temperature,
            max_tokens=spec.max_tokens,
            task_hint="voice_design",
        )
        db.add(
            LLMCall(
                novel_id=vp.novel_id,
                stage="VOICE_DESIGN",
                model=resp.model,
                prompt_version=spec.version,
                input_tokens=resp.input_tokens,
                output_tokens=resp.output_tokens,
                estimated_cost=resp.estimated_cost,
                latency_ms=resp.latency_ms,
                status="ok",
            )
        )
        candidates_raw = resp.data.get("candidates") or []
    except LLMClientError:
        candidates_raw = [
            {
                "label": "默认推荐版",
                "voice_prompt": vp.voice_prompt or f"适合{vp.display_name}的自然中文有声书配音。",
                "recommended_rate": vp.default_rate,
                "recommended_pitch": vp.pitch,
                "rationale": "默认回退方案",
            }
        ]

    try:
        tts = get_tts_provider(settings)
    except TTSProviderError as exc:
        raise AppError(code=exc.error_type, message=str(exc), status_code=503) from exc

    sample_line = (
        preview_text
        or f"你好，我是{vp.display_name}，这是我的专属有声书音色试听，请确认是否符合角色气质。"
    ).strip()
    if len(sample_line) < 15:
        sample_line = f"{sample_line}，这是有声书专属音色试听样本。"
    sample_line = sample_line[:200]

    designed_candidates: list[dict] = []

    for idx, cand in enumerate(candidates_raw[:3]):
        prompt = (cand.get("voice_prompt") or "").strip()[:500]
        if not prompt:
            continue
        try:
            vd = tts.design_voice(
                voice_prompt=prompt,
                preview_text=sample_line,
                language=vp.language,
            )
        except TTSProviderError as exc:
            raise AppError(code=exc.error_type, message=str(exc), status_code=502) from exc

        preview_checksum = sha256_bytes(vd.sample_audio_bytes)
        rel_audio, _size = storage.save_audio_bytes(
            f"{vp.novel_id}/previews/{vp.id}_cand{idx}.{vd.sample_format}",
            vd.sample_audio_bytes,
        )
        est_cost = estimate_tts_cost(len(sample_line), settings.tts_price_per_10k_chars)
        db.add(
            ProviderCall(
                novel_id=vp.novel_id,
                provider=vd.provider,
                model=vd.model,
                operation="voice_design",
                request_id=vd.request_id,
                input_chars=len(sample_line),
                billed_characters=None,
                audio_duration_ms=vd.duration_ms,
                estimated_cost=est_cost,
                actual_cost=est_cost,
                currency=settings.tts_currency,
                cost_kind="estimated",
                provider_request_status=vd.provider_status,
                is_mock=vd.is_mock,
                latency_ms=vd.latency_ms,
                status="ok",
            )
        )
        designed_candidates.append(
            {
                "index": idx,
                "label": cand.get("label") or f"候选音色 {idx + 1}",
                "voice_prompt": prompt,
                "voice_id": vd.voice_id,
                "prefix": vd.prefix,
                "provider_status": vd.provider_status,
                "preview_checksum": preview_checksum,
                "sample_audio_ref": rel_audio,
                "duration_ms": vd.duration_ms,
                "recommended_rate": float(cand.get("recommended_rate", 1.0)),
                "recommended_pitch": float(cand.get("recommended_pitch", 1.0)),
                "rationale": cand.get("rationale") or "",
            }
        )

    vp.candidates_json = designed_candidates
    if designed_candidates and not vp.voice_id:
        first = designed_candidates[0]
        vp.voice_id = first["voice_id"]
        vp.voice_prompt = first["voice_prompt"]
        vp.sample_audio_ref = first["sample_audio_ref"]
        vp.preview_checksum = first["preview_checksum"]
        vp.provider_status = first["provider_status"]
        vp.approval_status = VoiceApprovalStatus.PREVIEWED.value

    db.commit()
    db.refresh(vp)
    return vp


def update_voice_assignment(
    db: Session,
    settings: Settings,
    *,
    profile_id: str,
    display_name: str | None = None,
    candidate_index: int | None = None,
    voice_id: str | None = None,
    voice_prompt: str | None = None,
    target_model: str | None = None,
    default_rate: float | None = None,
    pitch: float | None = None,
    consent_confirmed: bool | None = None,
    consent_note: str | None = None,
    lock: bool = False,
    scope: str = "all_episodes",
    episode_id: str | None = None,
) -> dict:
    """
    P0-05: Unified atomic voice assignment update & selective audio invalidation.
    - Any synthesis-impacting change (voice_id, target_model, default_rate, pitch, voice_prompt)
      increments profile_version and persists an immutable VoiceProfileVersion snapshot.
    - Computes impacted segments, chars, and estimated CNY re-render cost.
    - Marks ONLY this speaker's current AudioSegments as STALE when scope != 'unrendered_only'.
      Other speakers' and narrator's audio segments remain completely untouched.
    """
    vp = get_voice_profile(db, profile_id)

    old_voice_id = vp.voice_id
    old_model = vp.target_model
    old_rate = vp.default_rate
    old_pitch = vp.pitch
    old_prompt = vp.voice_prompt

    if display_name is not None:
        vp.display_name = display_name.strip() or vp.display_name

    if candidate_index is not None:
        cands = vp.candidates_json or []
        if candidate_index < 0 or candidate_index >= len(cands):
            raise AppError(code="INVALID_CANDIDATE", message="Candidate index out of range", status_code=400)
        chosen = cands[candidate_index]
        vp.voice_id = chosen["voice_id"]
        vp.voice_prompt = (chosen["voice_prompt"] or "")[:500]
        vp.sample_audio_ref = chosen.get("sample_audio_ref")
        vp.preview_checksum = chosen.get("preview_checksum") or vp.preview_checksum
        vp.provider_status = chosen.get("provider_status") or vp.provider_status
        vp.default_rate = float(chosen.get("recommended_rate", vp.default_rate))
        vp.pitch = float(chosen.get("recommended_pitch", vp.pitch))
    else:
        if voice_id is not None:
            vp.voice_id = voice_id.strip() or None
        if voice_prompt is not None:
            vp.voice_prompt = voice_prompt.strip()[:500]

    if target_model is not None and target_model.strip():
        vp.target_model = target_model.strip()
    if default_rate is not None:
        vp.default_rate = max(0.5, min(2.0, default_rate))
    if pitch is not None:
        vp.pitch = max(0.5, min(2.0, pitch))
    if consent_confirmed is not None:
        vp.consent_confirmed = consent_confirmed
    if consent_note is not None:
        vp.consent_note = consent_note

    if lock:
        if not vp.voice_id:
            raise AppError(
                code="MISSING_VOICE_ID",
                message="Cannot lock voice profile without a valid voice_id; design or provide one first",
                status_code=400,
            )
        # Deactivate any other active voice profile for the same character/narrator in this novel
        if vp.character_id:
            others = db.scalars(
                select(VoiceProfile).where(
                    VoiceProfile.novel_id == vp.novel_id,
                    VoiceProfile.character_id == vp.character_id,
                    VoiceProfile.id != vp.id,
                    VoiceProfile.is_active.is_(True),
                )
            ).all()
        else:
            others = db.scalars(
                select(VoiceProfile).where(
                    VoiceProfile.novel_id == vp.novel_id,
                    VoiceProfile.narrator_key == vp.narrator_key,
                    VoiceProfile.id != vp.id,
                    VoiceProfile.is_active.is_(True),
                )
            ).all()
        for o in others:
            o.is_active = False

        vp.is_active = True
        vp.approval_status = VoiceApprovalStatus.LOCKED.value
        vp.locked_at = datetime.now(timezone.utc)

    synthesis_changed = (
        vp.voice_id != old_voice_id
        or vp.target_model != old_model
        or abs(vp.default_rate - old_rate) > 1e-6
        or abs(vp.pitch - old_pitch) > 1e-6
    )
    prompt_changed = vp.voice_prompt != old_prompt

    if synthesis_changed or (vp.is_active and prompt_changed):
        vp.profile_version += 1
        if prompt_changed:
            vp.prompt_version += 1

    _record_version_snapshot(db, vp)

    # Compute impacted segments & invalidate stale audio selectively
    segs = _find_speaker_segments(
        db,
        vp,
        episode_id=episode_id if scope == "current_episode" else None,
    )
    impacted_chars = sum(len(s.text_content) for s in segs)
    invalidated_count = 0

    should_invalidate = segs and (
        scope in ("all_episodes", "current_episode")
        and (synthesis_changed or (lock and vp.is_active))
    )
    if should_invalidate:
        seg_ids = [s.id for s in segs]
        for aud in db.scalars(
            select(AudioSegment).where(
                AudioSegment.script_segment_id.in_(seg_ids),
                AudioSegment.is_current.is_(True),
            )
        ).all():
            if (
                aud.voice_id != vp.voice_id
                or aud.voice_profile_version != vp.profile_version
                or aud.model != vp.target_model
                or synthesis_changed
            ):
                aud.status = AudioSegmentStatus.STALE.value
                invalidated_count += 1

    db.commit()
    db.refresh(vp)
    return {
        "profile": vp,
        "impacted_segments": len(segs),
        "impacted_chars": impacted_chars,
        "invalidated_audio_segments": invalidated_count,
        "estimated_rerender_cost": estimate_tts_cost(impacted_chars, settings.tts_price_per_10k_chars),
        "currency": settings.tts_currency,
    }


def lock_voice_profile(
    db: Session,
    settings: Settings,
    *,
    profile_id: str,
    candidate_index: int | None = None,
    voice_id: str | None = None,
    voice_prompt: str | None = None,
    scope: str = "unrendered_only",
) -> dict:
    return update_voice_assignment(
        db,
        settings,
        profile_id=profile_id,
        candidate_index=candidate_index,
        voice_id=voice_id,
        voice_prompt=voice_prompt,
        lock=True,
        scope=scope,
    )


def update_voice_profile(
    db: Session,
    settings: Settings,
    *,
    profile_id: str,
    display_name: str | None = None,
    voice_prompt: str | None = None,
    voice_id: str | None = None,
    target_model: str | None = None,
    default_rate: float | None = None,
    pitch: float | None = None,
    consent_confirmed: bool | None = None,
    consent_note: str | None = None,
    scope: str = "all_episodes",
) -> VoiceProfile:
    res = update_voice_assignment(
        db,
        settings,
        profile_id=profile_id,
        display_name=display_name,
        voice_prompt=voice_prompt,
        voice_id=voice_id,
        target_model=target_model,
        default_rate=default_rate,
        pitch=pitch,
        consent_confirmed=consent_confirmed,
        consent_note=consent_note,
        lock=False,
        scope=scope,
    )
    return res["profile"]


def sync_voice_provider_status(
    db: Session,
    settings: Settings,
    profile_id: str,
) -> dict:
    vp = get_voice_profile(db, profile_id)
    if not vp.voice_id:
        return {
            "profile_id": vp.id,
            "voice_id": None,
            "provider_status": VoiceProviderStatus.UNKNOWN.value,
            "target_model": vp.target_model,
            "is_mock": settings.tts_mock_mode,
        }
    try:
        tts = get_tts_provider(settings)
        status_val = tts.query_voice_status(vp.voice_id)
    except TTSProviderError as exc:
        raise AppError(code=exc.error_type, message=str(exc), status_code=502) from exc

    vp.provider_status = status_val
    db.commit()
    db.refresh(vp)
    return {
        "profile_id": vp.id,
        "voice_id": vp.voice_id,
        "provider_status": vp.provider_status,
        "target_model": vp.target_model,
        "is_mock": tts.is_mock,
    }


def get_preview_audio_path(
    db: Session,
    storage: LocalStorage,
    profile_id: str,
    candidate_index: int | None = None,
) -> Path:
    vp = get_voice_profile(db, profile_id)
    ref = vp.sample_audio_ref
    if candidate_index is not None:
        cands = vp.candidates_json or []
        if 0 <= candidate_index < len(cands):
            ref = cands[candidate_index].get("sample_audio_ref")
    if not ref:
        raise AppError(
            code="PREVIEW_NOT_FOUND",
            message="No preview audio generated for this voice profile yet",
            status_code=404,
        )
    return storage.get_audio_path(ref)
