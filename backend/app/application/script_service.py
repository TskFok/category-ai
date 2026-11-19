from __future__ import annotations

import json
import logging

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.application.episode_service import _load_chapter_inputs, get_episode
from app.core.config import Settings
from app.core.errors import AppError
from app.domain.enums import (
    AudioSegmentStatus,
    EpisodeStatus,
    ReviewStatus,
    ReviewTaskState,
    SegmentReviewStatus,
    SegmentType,
)
from app.domain.episode_rules import extract_episode_text_slices
from app.domain.script_validation import split_chapter_slice_to_segments
from app.infrastructure.db.models import (
    Alias,
    AudioSegment,
    Character,
    LLMCall,
    ReviewTask,
    ScriptSegment,
    SegmentCandidate,
    VoicePerformance,
)
from app.infrastructure.llm.deepseek import DeepSeekClient, LLMClientError
from app.infrastructure.storage.local import LocalStorage
from worker.pipelines.prompt_loader import load_prompt

logger = logging.getLogger(__name__)


def _build_character_lookup(db: Session, novel_id: str) -> tuple[dict[str, str], list[str]]:
    """Map canonical names and confirmed/candidate aliases -> character_id for this novel."""
    chars = list(
        db.scalars(
            select(Character)
            .where(
                Character.novel_id == novel_id,
                Character.status != ReviewStatus.SUPERSEDED.value,
            )
            .order_by(Character.mention_count.desc())
        ).all()
    )
    name_to_id: dict[str, str] = {}
    known_names: list[str] = []
    for c in chars:
        name_to_id[c.canonical_name] = c.id
        known_names.append(c.canonical_name)

    aliases = list(
        db.scalars(
            select(Alias).where(
                Alias.novel_id == novel_id,
                Alias.review_status != ReviewStatus.REJECTED.value,
            )
        ).all()
    )
    for a in aliases:
        if a.alias_text not in name_to_id:
            name_to_id[a.alias_text] = a.character_id
    return name_to_id, known_names


def analyze_episode_script(
    db: Session,
    storage: LocalStorage,
    settings: Settings,
    *,
    episode_id: str,
) -> list[ScriptSegment]:
    """
    SCRIPT-01:
    1. Deterministically split authoritative episode text into narration/dialogue/inner_monologue/scene_heading
       segments with exact chapter_id + [start_offset, end_offset) + text_hash.
    2. Preserve any existing segments that were manually overridden by a human reviewer.
    3. Batch dialogue & inner_monologue segments to DeepSeek for speaker attribution + emotion.
    4. Route low-confidence / unresolved attributions to ReviewTask (speaker_attribution).
    """
    ep = get_episode(db, episode_id)
    chapter_inputs = _load_chapter_inputs(db, storage, ep.novel_id)
    slices = extract_episode_text_slices(
        chapter_inputs,
        ep.start_chapter_id,
        ep.start_offset,
        ep.end_chapter_id,
        ep.end_offset,
    )

    raw_segments = []
    for sl in slices:
        raw_segments.extend(
            split_chapter_slice_to_segments(sl.chapter_id, sl.start_offset, sl.text)
        )

    # Snapshot existing manual overrides keyed by (chapter_id, start_offset, end_offset)
    existing_segs = list(
        db.scalars(
            select(ScriptSegment).where(ScriptSegment.episode_id == ep.id)
        ).all()
    )
    manual_map: dict[tuple[str, int, int], dict] = {}
    for es in existing_segs:
        if es.manual_override:
            manual_map[(es.chapter_id, es.start_offset, es.end_offset)] = {
                "segment_type": es.segment_type,
                "speaker_character_id": es.speaker_character_id,
                "narrator_key": es.narrator_key,
                "inference_reason": es.inference_reason,
                "confidence": es.confidence,
                "emotion": es.emotion,
                "emotion_intensity": es.emotion_intensity,
                "speech_rate": es.speech_rate,
                "review_status": es.review_status,
                "revision": es.revision,
            }

    # Clear previous segments for this episode before inserting fresh ordered script
    if existing_segs:
        db.execute(delete(ScriptSegment).where(ScriptSegment.episode_id == ep.id))
        db.flush()

    name_to_id, known_names = _build_character_lookup(db, ep.novel_id)

    # Identify dialogue / inner_monologue segments needing speaker attribution
    dialogue_batch_items: list[dict] = []
    for idx, raw in enumerate(raw_segments, start=1):
        span_key = (raw.chapter_id, raw.start_offset, raw.end_offset)
        if span_key in manual_map:
            continue
        if raw.segment_type in (SegmentType.DIALOGUE.value, SegmentType.INNER_MONOLOGUE.value):
            before = raw_segments[idx - 2].text_content if idx >= 2 else ""
            after = raw_segments[idx].text_content if idx < len(raw_segments) else ""
            dialogue_batch_items.append(
                {
                    "order_no": idx,
                    "segment_type": raw.segment_type,
                    "text": raw.text_content,
                    "context_before": before[-120:],
                    "context_after": after[:120],
                }
            )

    attribution_by_order: dict[int, dict] = {}
    model_used = settings.deepseek_extract_model
    if dialogue_batch_items:
        llm = DeepSeekClient(settings)
        spec = load_prompt("dialogue_attribution")
        batch_size = 25
        for start_i in range(0, len(dialogue_batch_items), batch_size):
            batch = dialogue_batch_items[start_i : start_i + batch_size]
            try:
                resp = llm.chat_json(
                    system_prompt=spec.system_prompt,
                    user_prompt=spec.user_template.format(
                        known_characters=", ".join(known_names[:80]) or "暂无",
                        segments_json=json.dumps(batch, ensure_ascii=False),
                    ),
                    tier=spec.model_tier,
                    temperature=spec.temperature,
                    max_tokens=spec.max_tokens,
                    task_hint="dialogue_attribution",
                )
                model_used = resp.model
                db.add(
                    LLMCall(
                        novel_id=ep.novel_id,
                        stage="DIALOGUE_ATTRIBUTION",
                        model=resp.model,
                        prompt_version=spec.version,
                        input_tokens=resp.input_tokens,
                        output_tokens=resp.output_tokens,
                        estimated_cost=resp.estimated_cost,
                        latency_ms=resp.latency_ms,
                        status="ok",
                    )
                )
                for attr in resp.data.get("attributions", []):
                    ord_no = attr.get("order_no")
                    if isinstance(ord_no, int):
                        attribution_by_order[ord_no] = attr
            except LLMClientError as exc:
                logger.warning("Dialogue attribution batch failed: %s", exc)

    created_segments: list[ScriptSegment] = []
    threshold = settings.speaker_review_threshold

    for idx, raw in enumerate(raw_segments, start=1):
        span_key = (raw.chapter_id, raw.start_offset, raw.end_offset)
        if span_key in manual_map:
            ov = manual_map[span_key]
            seg = ScriptSegment(
                novel_id=ep.novel_id,
                episode_id=ep.id,
                chapter_id=raw.chapter_id,
                order_no=idx,
                start_offset=raw.start_offset,
                end_offset=raw.end_offset,
                text_content=raw.text_content,
                text_hash=raw.text_hash,
                segment_type=ov["segment_type"],
                speaker_character_id=ov["speaker_character_id"],
                narrator_key=ov["narrator_key"],
                inference_reason=ov["inference_reason"],
                confidence=ov["confidence"],
                emotion=ov["emotion"],
                emotion_intensity=ov["emotion_intensity"],
                speech_rate=ov["speech_rate"],
                review_status=ov["review_status"],
                manual_override=True,
                revision=ov["revision"],
            )
            db.add(seg)
            created_segments.append(seg)
            continue

        if raw.segment_type in (SegmentType.NARRATION.value, SegmentType.SCENE_HEADING.value):
            seg = ScriptSegment(
                novel_id=ep.novel_id,
                episode_id=ep.id,
                chapter_id=raw.chapter_id,
                order_no=idx,
                start_offset=raw.start_offset,
                end_offset=raw.end_offset,
                text_content=raw.text_content,
                text_hash=raw.text_hash,
                segment_type=raw.segment_type,
                speaker_character_id=None,
                narrator_key="default",
                inference_reason="叙述/场景标题默认归属旁白角色",
                confidence=1.0,
                emotion="neutral",
                emotion_intensity=0.5,
                speech_rate=1.0,
                review_status=SegmentReviewStatus.AUTO_ACCEPTED.value,
                manual_override=False,
            )
            db.add(seg)
            created_segments.append(seg)
            continue

        # Dialogue or inner monologue
        attr = attribution_by_order.get(idx, {})
        speaker_name = (attr.get("speaker_name") or "").strip() if attr.get("speaker_name") else ""
        speaker_char_id = name_to_id.get(speaker_name) if speaker_name else None
        confidence = float(attr.get("confidence", 0.0 if not speaker_char_id else 0.75))
        emotion = (attr.get("emotion") or "neutral").strip()
        emotion_intensity = float(attr.get("emotion_intensity", 0.5))
        reason = attr.get("reason") or ("未匹配到已知人物" if not speaker_char_id else f"匹配角色「{speaker_name}」")

        # Low confidence or unknown speaker cannot be auto-bound silently
        if speaker_char_id is None or confidence < threshold:
            review_status = SegmentReviewStatus.NEEDS_REVIEW.value
            if confidence < 0.5:
                speaker_char_id = None
        else:
            review_status = SegmentReviewStatus.AUTO_ACCEPTED.value

        seg = ScriptSegment(
            novel_id=ep.novel_id,
            episode_id=ep.id,
            chapter_id=raw.chapter_id,
            order_no=idx,
            start_offset=raw.start_offset,
            end_offset=raw.end_offset,
            text_content=raw.text_content,
            text_hash=raw.text_hash,
            segment_type=raw.segment_type,
            speaker_character_id=speaker_char_id,
            narrator_key=None,
            inference_reason=reason,
            confidence=round(max(0.0, min(1.0, confidence)), 4),
            emotion=emotion,
            emotion_intensity=round(max(0.0, min(1.0, emotion_intensity)), 2),
            speech_rate=1.0,
            review_status=review_status,
            manual_override=False,
        )
        db.add(seg)
        db.flush()
        created_segments.append(seg)

        for cand in attr.get("candidates", []):
            c_name = (cand.get("name") or "").strip()
            if not c_name:
                continue
            db.add(
                SegmentCandidate(
                    segment_id=seg.id,
                    character_id=name_to_id.get(c_name),
                    candidate_name=c_name,
                    score=float(cand.get("score", 0.5)),
                    reason=cand.get("reason"),
                    model_version=model_used,
                )
            )

        if review_status == SegmentReviewStatus.NEEDS_REVIEW.value:
            db.add(
                ReviewTask(
                    novel_id=ep.novel_id,
                    review_type="speaker_attribution",
                    target_kind="script_segment",
                    target_id=seg.id,
                    severity="medium",
                    state=ReviewTaskState.OPEN.value,
                    summary=f"第{ep.episode_no}集第{idx}段说话人归属存疑 (置信度 {seg.confidence:.2f})",
                    payload_json={
                        "episode_id": ep.id,
                        "order_no": idx,
                        "text_preview": seg.text_content[:80],
                        "reason": reason,
                    },
                )
            )

    ep.status = EpisodeStatus.SCRIPT_READY.value
    db.commit()
    return created_segments


def list_episode_segments(
    db: Session,
    episode_id: str,
    *,
    review_status: str | None = None,
    segment_type: str | None = None,
) -> list[dict]:
    ep = get_episode(db, episode_id)
    stmt = (
        select(ScriptSegment)
        .where(ScriptSegment.episode_id == ep.id)
        .order_by(ScriptSegment.order_no.asc())
    )
    if review_status:
        stmt = stmt.where(ScriptSegment.review_status == review_status)
    if segment_type:
        stmt = stmt.where(ScriptSegment.segment_type == segment_type)

    segs = list(db.scalars(stmt).all())
    seg_ids = [s.id for s in segs]

    char_ids = {s.speaker_character_id for s in segs if s.speaker_character_id}
    char_names: dict[str, str] = {}
    if char_ids:
        for c in db.scalars(select(Character).where(Character.id.in_(char_ids))).all():
            char_names[c.id] = c.canonical_name

    cand_map: dict[str, list[SegmentCandidate]] = {}
    audio_map: dict[str, AudioSegment] = {}
    if seg_ids:
        for cand in db.scalars(
            select(SegmentCandidate).where(SegmentCandidate.segment_id.in_(seg_ids))
        ).all():
            cand_map.setdefault(cand.segment_id, []).append(cand)
        for aud in db.scalars(
            select(AudioSegment).where(
                AudioSegment.script_segment_id.in_(seg_ids),
                AudioSegment.is_current.is_(True),
            )
        ).all():
            audio_map[aud.script_segment_id] = aud

    result = []
    for s in segs:
        aud = audio_map.get(s.id)
        result.append(
            {
                "id": s.id,
                "novel_id": s.novel_id,
                "episode_id": s.episode_id,
                "chapter_id": s.chapter_id,
                "order_no": s.order_no,
                "start_offset": s.start_offset,
                "end_offset": s.end_offset,
                "text_content": s.text_content,
                "text_hash": s.text_hash,
                "segment_type": s.segment_type,
                "speaker_character_id": s.speaker_character_id,
                "speaker_name": (
                    char_names.get(s.speaker_character_id)
                    if s.speaker_character_id
                    else ("旁白" if s.narrator_key else None)
                ),
                "narrator_key": s.narrator_key,
                "inference_reason": s.inference_reason,
                "confidence": s.confidence,
                "emotion": s.emotion,
                "emotion_intensity": s.emotion_intensity,
                "speech_rate": s.speech_rate,
                "review_status": s.review_status,
                "manual_override": s.manual_override,
                "revision": s.revision,
                "candidates": [
                    {
                        "id": c.id,
                        "character_id": c.character_id,
                        "candidate_name": c.candidate_name,
                        "score": c.score,
                        "reason": c.reason,
                    }
                    for c in cand_map.get(s.id, [])
                ],
                "current_audio": (
                    {
                        "id": aud.id,
                        "status": aud.status,
                        "voice_id": aud.voice_id,
                        "duration_ms": aud.duration_ms,
                        "audio_format": aud.audio_format,
                        "checksum": aud.checksum,
                        "stream_url": f"/api/v1/audio-segments/{aud.id}/stream" if aud.audio_ref else None,
                    }
                    if aud
                    else None
                ),
            }
        )
    return result


def update_script_segment(
    db: Session,
    segment_id: str,
    *,
    segment_type: str | None = None,
    speaker_character_id: str | None = None,
    set_narrator: bool = False,
    narrator_key: str | None = None,
    emotion: str | None = None,
    emotion_intensity: float | None = None,
    speech_rate: float | None = None,
    instruction: str | None = None,
    review_status: str | None = None,
) -> ScriptSegment:
    seg = db.get(ScriptSegment, segment_id)
    if not seg:
        raise AppError(code="SEGMENT_NOT_FOUND", message="Script segment not found", status_code=404)

    audio_impacting = False
    if segment_type is not None and segment_type in {t.value for t in SegmentType}:
        seg.segment_type = segment_type
        audio_impacting = True

    if set_narrator:
        seg.speaker_character_id = None
        seg.narrator_key = narrator_key or "default"
        audio_impacting = True
    elif speaker_character_id is not None:
        if speaker_character_id == "":
            seg.speaker_character_id = None
        else:
            char = db.get(Character, speaker_character_id)
            if not char or char.novel_id != seg.novel_id:
                raise AppError(
                    code="INVALID_SPEAKER",
                    message="speaker_character_id must belong to the same novel",
                    status_code=400,
                )
            seg.speaker_character_id = char.id
            seg.narrator_key = None
        audio_impacting = True

    if emotion is not None:
        seg.emotion = emotion.strip() or "neutral"
        audio_impacting = True
    if emotion_intensity is not None:
        seg.emotion_intensity = max(0.0, min(1.0, emotion_intensity))
        audio_impacting = True
    if speech_rate is not None:
        seg.speech_rate = max(0.5, min(2.0, speech_rate))
        audio_impacting = True

    if review_status is not None and review_status in {r.value for r in SegmentReviewStatus}:
        seg.review_status = review_status
    else:
        seg.review_status = SegmentReviewStatus.CONFIRMED.value

    seg.manual_override = True
    seg.revision += 1

    # Record dynamic VoicePerformance snapshot
    if audio_impacting or instruction is not None:
        db.add(
            VoicePerformance(
                segment_id=seg.id,
                emotion=seg.emotion,
                intensity=seg.emotion_intensity,
                instruction=instruction,
                rate=seg.speech_rate,
                pitch=1.0,
                version=seg.revision,
            )
        )
        # AUDIO-01: Mark ONLY this segment's current audio as stale (other segments untouched!)
        cur_audios = db.scalars(
            select(AudioSegment).where(
                AudioSegment.script_segment_id == seg.id,
                AudioSegment.is_current.is_(True),
            )
        ).all()
        for aud in cur_audios:
            aud.status = AudioSegmentStatus.STALE.value

    # Close any open speaker_attribution ReviewTask for this segment
    open_tasks = db.scalars(
        select(ReviewTask).where(
            ReviewTask.target_kind == "script_segment",
            ReviewTask.target_id == seg.id,
            ReviewTask.state == ReviewTaskState.OPEN.value,
        )
    ).all()
    for rt in open_tasks:
        rt.state = ReviewTaskState.RESOLVED.value
        rt.decision_note = "Resolved via script segment manual update"

    db.commit()
    db.refresh(seg)
    return seg


def submit_speaker_review(
    db: Session,
    episode_id: str,
    decisions: list[dict],
) -> int:
    """Batch-apply reviewer speaker decisions for an episode."""
    ep = get_episode(db, episode_id)
    updated = 0
    for d in decisions:
        seg = db.get(ScriptSegment, d["segment_id"])
        if not seg or seg.episode_id != ep.id:
            continue
        action = d.get("action", "confirm")
        if action == "skip":
            update_script_segment(db, seg.id, review_status=SegmentReviewStatus.SKIP.value)
        elif action == "narrator":
            update_script_segment(
                db,
                seg.id,
                set_narrator=True,
                narrator_key=d.get("narrator_key") or "default",
                review_status=SegmentReviewStatus.CONFIRMED.value,
            )
        else:
            update_script_segment(
                db,
                seg.id,
                speaker_character_id=d.get("speaker_character_id"),
                emotion=d.get("emotion"),
                review_status=SegmentReviewStatus.CONFIRMED.value,
            )
        updated += 1
    return updated
