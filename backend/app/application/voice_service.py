from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import AppError
from app.domain.enums import AudioSegmentStatus, VoiceApprovalStatus
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
)
from app.infrastructure.llm.deepseek import DeepSeekClient, LLMClientError
from app.infrastructure.storage.local import LocalStorage
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
        voice_prompt=voice_prompt.strip(),
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
    )
    db.add(vp)
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
    VOICE-01:
    1. Use DeepSeek to translate character profile/analysis into 2-3 voice design prompts.
    2. Call TTSProvider.design_voice for each candidate to obtain reusable voice_id + preview WAV.
    3. Save preview audio files and store candidates on the VoiceProfile.
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

    tts = get_tts_provider(settings)
    sample_line = (preview_text or f"你好，我是{vp.display_name}，这是我的专属有声书音色试听。").strip()
    designed_candidates: list[dict] = []

    for idx, cand in enumerate(candidates_raw[:3]):
        prompt = (cand.get("voice_prompt") or "").strip()
        if not prompt:
            continue
        vd = tts.design_voice(
            voice_prompt=prompt,
            preview_text=sample_line,
            language=vp.language,
        )
        rel_audio, _size = storage.save_audio_bytes(
            f"{vp.novel_id}/previews/{vp.id}_cand{idx}.{vd.sample_format}",
            vd.sample_audio_bytes,
        )
        db.add(
            ProviderCall(
                novel_id=vp.novel_id,
                provider=vd.provider,
                model=vd.model,
                operation="voice_design",
                request_id=vd.request_id,
                input_chars=len(sample_line),
                audio_duration_ms=vd.duration_ms,
                estimated_cost=estimate_tts_cost(len(sample_line), settings.tts_price_per_10k_chars),
                actual_cost=estimate_tts_cost(len(sample_line), settings.tts_price_per_10k_chars),
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
        vp.approval_status = VoiceApprovalStatus.PREVIEWED.value

    db.commit()
    db.refresh(vp)
    return vp


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
    """
    VOICE-01:
    Lock a reusable voice_id onto this profile and activate it as the sole active voice
    for this character/narrator in the novel.
    Computes impacted segments & estimated re-render cost; if scope == 'all_episodes',
    marks existing audio segments for this speaker as stale.
    """
    vp = get_voice_profile(db, profile_id)

    if candidate_index is not None:
        cands = vp.candidates_json or []
        if candidate_index < 0 or candidate_index >= len(cands):
            raise AppError(code="INVALID_CANDIDATE", message="Candidate index out of range", status_code=400)
        chosen = cands[candidate_index]
        vp.voice_id = chosen["voice_id"]
        vp.voice_prompt = chosen["voice_prompt"]
        vp.sample_audio_ref = chosen.get("sample_audio_ref")
        vp.default_rate = float(chosen.get("recommended_rate", vp.default_rate))
        vp.pitch = float(chosen.get("recommended_pitch", vp.pitch))
    else:
        if voice_id is not None:
            vp.voice_id = voice_id.strip() or vp.voice_id
        if voice_prompt is not None:
            vp.voice_prompt = voice_prompt.strip()

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

    # Compute impacted script segments & optional invalidation
    if vp.character_id:
        segs = list(
            db.scalars(
                select(ScriptSegment).where(
                    ScriptSegment.novel_id == vp.novel_id,
                    ScriptSegment.speaker_character_id == vp.character_id,
                )
            ).all()
        )
    else:
        segs = list(
            db.scalars(
                select(ScriptSegment).where(
                    ScriptSegment.novel_id == vp.novel_id,
                    ScriptSegment.narrator_key == vp.narrator_key,
                )
            ).all()
        )

    impacted_chars = sum(len(s.text_content) for s in segs)
    if scope == "all_episodes" and segs:
        seg_ids = [s.id for s in segs]
        for aud in db.scalars(
            select(AudioSegment).where(
                AudioSegment.script_segment_id.in_(seg_ids),
                AudioSegment.is_current.is_(True),
            )
        ).all():
            if aud.voice_id != vp.voice_id:
                aud.status = AudioSegmentStatus.STALE.value

    db.commit()
    db.refresh(vp)
    return {
        "profile": vp,
        "impacted_segments": len(segs),
        "impacted_chars": impacted_chars,
        "estimated_rerender_cost": estimate_tts_cost(impacted_chars, settings.tts_price_per_10k_chars),
    }


def update_voice_profile(
    db: Session,
    *,
    profile_id: str,
    display_name: str | None = None,
    voice_prompt: str | None = None,
    voice_id: str | None = None,
    default_rate: float | None = None,
    pitch: float | None = None,
    consent_confirmed: bool | None = None,
    consent_note: str | None = None,
) -> VoiceProfile:
    vp = get_voice_profile(db, profile_id)
    if display_name is not None:
        vp.display_name = display_name.strip() or vp.display_name
    if voice_prompt is not None:
        vp.voice_prompt = voice_prompt.strip()
    if voice_id is not None:
        vp.voice_id = voice_id.strip() or None
    if default_rate is not None:
        vp.default_rate = max(0.5, min(2.0, default_rate))
    if pitch is not None:
        vp.pitch = max(0.5, min(2.0, pitch))
    if consent_confirmed is not None:
        vp.consent_confirmed = consent_confirmed
    if consent_note is not None:
        vp.consent_note = consent_note
    db.commit()
    db.refresh(vp)
    return vp


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
