from __future__ import annotations

import json
import logging
import uuid

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
    VoiceApprovalStatus,
)
from app.domain.episode_rules import extract_episode_text_slices
from app.domain.script_validation import (
    split_chapter_slice_to_segments,
    split_text_to_synthesis_units,
    verify_segments_against_chapters,
)
from app.infrastructure.db.models import (
    Alias,
    AudioSegment,
    Chapter,
    Character,
    LLMCall,
    ReviewTask,
    ScriptSegment,
    SegmentCandidate,
    VoicePerformance,
    VoiceProfile,
)
from app.infrastructure.llm.deepseek import DeepSeekClient, LLMClientError
from app.infrastructure.storage.local import LocalStorage
from worker.pipelines.prompt_loader import load_prompt

logger = logging.getLogger(__name__)

SPEAKER_REQUIRED_TYPES = frozenset({SegmentType.DIALOGUE.value, SegmentType.INNER_MONOLOGUE.value})


def make_stable_span_key(chapter_id: str, start_offset: int, end_offset: int, text_hash: str) -> str:
    return f"{chapter_id}:{start_offset}:{end_offset}:{text_hash[:16]}"


def compute_is_render_blocked(
    *,
    segment_type: str,
    speaker_character_id: str | None,
    narrator_key: str | None,
    review_status: str,
    manual_override: bool,
    allow_low_confidence_auto_pass: bool = False,
) -> bool:
    """
    P0-04: Determine whether a ScriptSegment is blocked from rendering.
    - SKIP segments are never blocked.
    - DIALOGUE / INNER_MONOLOGUE require a confirmed/auto-accepted speaker_character_id,
      OR an explicit manual_override assigning narrator_key.
    - Unresolved dialogue is NEVER silently routed to narrator.
    """
    if review_status == SegmentReviewStatus.SKIP.value:
        return False

    if segment_type in SPEAKER_REQUIRED_TYPES:
        if review_status == SegmentReviewStatus.NEEDS_REVIEW.value:
            if allow_low_confidence_auto_pass and speaker_character_id:
                return False
            return True
        if speaker_character_id:
            return False
        if manual_override and narrator_key:
            return False
        return True

    # NARRATION / SCENE_HEADING
    return not bool(narrator_key or speaker_character_id)


def _build_character_lookup(
    db: Session,
    novel_id: str,
    *,
    chapter_no: int | None = None,
) -> tuple[dict[str, str], list[str]]:
    """
    Map canonical names and chapter-valid confirmed/candidate aliases -> character_id.
    Respects Alias.valid_from_chapter / valid_to_chapter (P1-05) to avoid spoiler leakage.
    """
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
        if chapter_no is not None:
            if a.valid_from_chapter is not None and chapter_no < a.valid_from_chapter:
                continue
            if a.valid_to_chapter is not None and chapter_no > a.valid_to_chapter:
                continue
        if a.alias_text not in name_to_id:
            name_to_id[a.alias_text] = a.character_id
    return name_to_id, known_names


def _sync_segment_review_task(
    db: Session,
    *,
    novel_id: str,
    episode_id: str,
    episode_no: int,
    seg: ScriptSegment,
    reason: str,
) -> None:
    open_tasks = list(
        db.scalars(
            select(ReviewTask).where(
                ReviewTask.target_kind == "script_segment",
                ReviewTask.target_id == seg.id,
                ReviewTask.state == ReviewTaskState.OPEN.value,
            )
        ).all()
    )
    if seg.review_status == SegmentReviewStatus.NEEDS_REVIEW.value:
        summary = f"第{episode_no}集第{seg.order_no}段说话人归属存疑 (置信度 {seg.confidence:.2f})"
        payload = {
            "episode_id": episode_id,
            "order_no": seg.order_no,
            "text_preview": seg.text_content[:80],
            "reason": reason,
        }
        if open_tasks:
            open_tasks[0].summary = summary
            open_tasks[0].payload_json = payload
            for extra in open_tasks[1:]:
                extra.state = ReviewTaskState.RESOLVED.value
                extra.decision_note = "Deduplicated during script re-analysis"
        else:
            db.add(
                ReviewTask(
                    novel_id=novel_id,
                    review_type="speaker_attribution",
                    target_kind="script_segment",
                    target_id=seg.id,
                    severity="medium",
                    state=ReviewTaskState.OPEN.value,
                    summary=summary,
                    payload_json=payload,
                )
            )
    else:
        for rt in open_tasks:
            rt.state = ReviewTaskState.RESOLVED.value
            rt.decision_note = "Auto-resolved during script re-analysis"


def analyze_episode_script(
    db: Session,
    storage: LocalStorage,
    settings: Settings,
    *,
    episode_id: str,
) -> list[ScriptSegment]:
    """
    SCRIPT-01 & P0-03 (Stable Segment ID + Diff/Upsert):
    1. Deterministically split authoritative episode text into narration/dialogue/inner_monologue/scene_heading
       segments with exact chapter_id + [start_offset, end_offset) + text_hash.
    2. NEVER cascade-delete existing ScriptSegments whose span/hash is unchanged.
       Preserve Segment IDs, linked AudioSegments, and manual overrides in-place.
    3. Batch non-manual dialogue & inner_monologue segments to DeepSeek for speaker attribution + emotion.
    4. Route low-confidence / unresolved attributions to ReviewTask (speaker_attribution) and mark
       is_render_blocked=True so they can never be silently rendered as narrator.
    5. Clean up / resolve ReviewTasks for truly removed spans; roll back transactionally on error.
    """
    ep = get_episode(db, episode_id)
    chapter_inputs = _load_chapter_inputs(db, storage, ep.novel_id)
    chapter_no_by_id = {ci.chapter_id: ci.chapter_no for ci in chapter_inputs}
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

    existing_segs = list(
        db.scalars(
            select(ScriptSegment)
            .where(ScriptSegment.episode_id == ep.id)
            .order_by(ScriptSegment.order_no.asc())
        ).all()
    )
    existing_by_span: dict[tuple[str, int, int, str], ScriptSegment] = {
        (es.chapter_id, es.start_offset, es.end_offset, es.text_hash): es
        for es in existing_segs
    }

    # Build chapter-aware lookup (defaulting to episode start chapter for alias validity)
    start_ch_no = chapter_no_by_id.get(ep.start_chapter_id)
    name_to_id, known_names = _build_character_lookup(db, ep.novel_id, chapter_no=start_ch_no)

    # Identify dialogue / inner_monologue segments needing speaker attribution
    dialogue_batch_items: list[dict] = []
    for idx, raw in enumerate(raw_segments, start=1):
        span_key = (raw.chapter_id, raw.start_offset, raw.end_offset, raw.text_hash)
        existing = existing_by_span.get(span_key)
        if existing and existing.manual_override:
            continue
        if raw.segment_type in SPEAKER_REQUIRED_TYPES:
            before = raw_segments[idx - 2].text_content if idx >= 2 else ""
            after = raw_segments[idx].text_content if idx < len(raw_segments) else ""
            prev_dialogue = ""
            for back in range(idx - 2, max(-1, idx - 6), -1):
                if raw_segments[back].segment_type in SPEAKER_REQUIRED_TYPES:
                    prev_dialogue = raw_segments[back].text_content[:120]
                    break
            dialogue_batch_items.append(
                {
                    "order_no": idx,
                    "segment_type": raw.segment_type,
                    "text": raw.text_content,
                    "context_before": before[-200:],
                    "context_after": after[:200],
                    "prev_dialogue": prev_dialogue,
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
                        known_characters=", ".join(known_names[:120]) or "暂无",
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

    run_id = str(uuid.uuid4())
    threshold = settings.speaker_review_threshold
    matched_existing_ids: set[str] = set()
    result_segments: list[ScriptSegment] = []

    try:
        # Temporarily shift existing order_no to negative values to avoid unique constraint collisions
        for i, es in enumerate(existing_segs, start=1):
            es.order_no = -i
        db.flush()

        for idx, raw in enumerate(raw_segments, start=1):
            span_tuple = (raw.chapter_id, raw.start_offset, raw.end_offset, raw.text_hash)
            stable_key = make_stable_span_key(
                raw.chapter_id, raw.start_offset, raw.end_offset, raw.text_hash
            )
            existing = existing_by_span.get(span_tuple)

            # Case 1: Unchanged span with manual_override -> preserve ID, manual fields, and audio!
            if existing is not None and existing.manual_override:
                matched_existing_ids.add(existing.id)
                existing.order_no = idx
                existing.stable_span_key = stable_key
                existing.analysis_run_id = run_id
                existing.is_render_blocked = compute_is_render_blocked(
                    segment_type=existing.segment_type,
                    speaker_character_id=existing.speaker_character_id,
                    narrator_key=existing.narrator_key,
                    review_status=existing.review_status,
                    manual_override=True,
                    allow_low_confidence_auto_pass=settings.allow_low_confidence_auto_pass,
                )
                result_segments.append(existing)
                continue

            # Determine inferred attributes for narration/heading vs dialogue/inner_monologue
            if raw.segment_type in (SegmentType.NARRATION.value, SegmentType.SCENE_HEADING.value):
                new_seg_type = raw.segment_type
                new_speaker_id = None
                new_narrator_key = "default"
                new_reason = "叙述/场景标题默认归属旁白角色"
                new_confidence = 1.0
                new_emotion = "neutral"
                new_intensity = 0.5
                new_rate = 1.0
                new_review_status = SegmentReviewStatus.AUTO_ACCEPTED.value
                candidates_data: list[dict] = []
            else:
                # Chapter-specific alias resolution
                seg_ch_no = chapter_no_by_id.get(raw.chapter_id)
                if seg_ch_no is not None and seg_ch_no != start_ch_no:
                    seg_name_to_id, _ = _build_character_lookup(db, ep.novel_id, chapter_no=seg_ch_no)
                else:
                    seg_name_to_id = name_to_id

                attr = attribution_by_order.get(idx, {})
                speaker_name = (attr.get("speaker_name") or "").strip() if attr.get("speaker_name") else ""
                speaker_char_id = seg_name_to_id.get(speaker_name) if speaker_name else None
                confidence = float(attr.get("confidence", 0.0 if not speaker_char_id else 0.75))
                emotion = (attr.get("emotion") or "neutral").strip()
                emotion_intensity = float(attr.get("emotion_intensity", 0.5))
                reason = attr.get("reason") or (
                    "未匹配到已知人物" if not speaker_char_id else f"匹配角色「{speaker_name}」"
                )

                if speaker_char_id is None or confidence < threshold:
                    new_review_status = SegmentReviewStatus.NEEDS_REVIEW.value
                    if confidence < 0.5:
                        speaker_char_id = None
                else:
                    new_review_status = SegmentReviewStatus.AUTO_ACCEPTED.value

                new_seg_type = raw.segment_type
                new_speaker_id = speaker_char_id
                new_narrator_key = None
                new_reason = reason
                new_confidence = round(max(0.0, min(1.0, confidence)), 4)
                new_emotion = emotion
                new_intensity = round(max(0.0, min(1.0, emotion_intensity)), 2)
                new_rate = 1.0
                candidates_data = list(attr.get("candidates", []))

            blocked = compute_is_render_blocked(
                segment_type=new_seg_type,
                speaker_character_id=new_speaker_id,
                narrator_key=new_narrator_key,
                review_status=new_review_status,
                manual_override=False,
                allow_low_confidence_auto_pass=settings.allow_low_confidence_auto_pass,
            )

            # Case 2: Unchanged span without manual_override -> in-place update, preserve ID & valid audio!
            if existing is not None:
                matched_existing_ids.add(existing.id)
                audio_impacting_change = (
                    existing.segment_type != new_seg_type
                    or existing.speaker_character_id != new_speaker_id
                    or existing.narrator_key != new_narrator_key
                    or existing.emotion != new_emotion
                    or abs(existing.emotion_intensity - new_intensity) > 1e-6
                    or abs(existing.speech_rate - new_rate) > 1e-6
                )
                existing.order_no = idx
                existing.stable_span_key = stable_key
                existing.analysis_run_id = run_id
                existing.segment_type = new_seg_type
                existing.speaker_character_id = new_speaker_id
                existing.narrator_key = new_narrator_key
                existing.inference_reason = new_reason
                existing.confidence = new_confidence
                existing.emotion = new_emotion
                existing.emotion_intensity = new_intensity
                existing.speech_rate = new_rate
                existing.review_status = new_review_status
                existing.is_render_blocked = blocked
                if audio_impacting_change:
                    existing.revision += 1
                    for aud in db.scalars(
                        select(AudioSegment).where(
                            AudioSegment.script_segment_id == existing.id,
                            AudioSegment.is_current.is_(True),
                        )
                    ).all():
                        aud.status = AudioSegmentStatus.STALE.value

                # Refresh candidates for this segment
                db.execute(delete(SegmentCandidate).where(SegmentCandidate.segment_id == existing.id))
                seg = existing
            else:
                # Case 3: Truly new span -> insert new ScriptSegment
                seg = ScriptSegment(
                    novel_id=ep.novel_id,
                    episode_id=ep.id,
                    chapter_id=raw.chapter_id,
                    order_no=idx,
                    start_offset=raw.start_offset,
                    end_offset=raw.end_offset,
                    text_content=raw.text_content,
                    text_hash=raw.text_hash,
                    stable_span_key=stable_key,
                    content_revision=1,
                    analysis_run_id=run_id,
                    segment_type=new_seg_type,
                    speaker_character_id=new_speaker_id,
                    narrator_key=new_narrator_key,
                    inference_reason=new_reason,
                    confidence=new_confidence,
                    emotion=new_emotion,
                    emotion_intensity=new_intensity,
                    speech_rate=new_rate,
                    review_status=new_review_status,
                    manual_override=False,
                    is_render_blocked=blocked,
                )
                db.add(seg)
                db.flush()

            for cand in candidates_data:
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

            _sync_segment_review_task(
                db,
                novel_id=ep.novel_id,
                episode_id=ep.id,
                episode_no=ep.episode_no,
                seg=seg,
                reason=new_reason,
            )
            result_segments.append(seg)

        # Remove ONLY truly obsolete segments (spans that no longer exist) and close their ReviewTasks
        removed_segs = [es for es in existing_segs if es.id not in matched_existing_ids]
        if removed_segs:
            removed_ids = [rs.id for rs in removed_segs]
            for rt in db.scalars(
                select(ReviewTask).where(
                    ReviewTask.target_kind == "script_segment",
                    ReviewTask.target_id.in_(removed_ids),
                    ReviewTask.state == ReviewTaskState.OPEN.value,
                )
            ).all():
                rt.state = ReviewTaskState.RESOLVED.value
                rt.decision_note = "Segment span removed after episode boundary or script update"
            db.execute(delete(ScriptSegment).where(ScriptSegment.id.in_(removed_ids)))

        ep.status = EpisodeStatus.SCRIPT_READY.value
        db.commit()
        return result_segments
    except Exception:
        db.rollback()
        raise


def validate_episode_script(
    db: Session,
    storage: LocalStorage,
    settings: Settings,
    episode_id: str,
) -> dict:
    """
    P0-04 / Section 9: Validate an episode's script before rendering.
    Checks:
    1. Unresolved / needs_review / missing speaker dialogues & inner monologues (blocks render).
    2. Missing locked VoiceProfiles for all referenced speakers & narrators.
    3. Over-limit segments that will be split into SynthesisUnits.
    4. Authoritative chapter text offset/hash integrity.
    """
    ep = get_episode(db, episode_id)
    segments = list(
        db.scalars(
            select(ScriptSegment)
            .where(ScriptSegment.episode_id == ep.id)
            .order_by(ScriptSegment.order_no.asc())
        ).all()
    )
    active_segs = [s for s in segments if s.review_status != SegmentReviewStatus.SKIP.value]

    # Load active locked voices
    locked_profiles = list(
        db.scalars(
            select(VoiceProfile).where(
                VoiceProfile.novel_id == ep.novel_id,
                VoiceProfile.is_active.is_(True),
                VoiceProfile.approval_status == VoiceApprovalStatus.LOCKED.value,
                VoiceProfile.voice_id.is_not(None),
            )
        ).all()
    )
    by_char = {vp.character_id: vp for vp in locked_profiles if vp.character_id}
    by_narrator = {vp.narrator_key: vp for vp in locked_profiles if vp.narrator_key}

    # Verify character IDs are still valid (not deleted/superseded)
    valid_char_ids = set(
        db.scalars(
            select(Character.id).where(
                Character.novel_id == ep.novel_id,
                Character.status != ReviewStatus.SUPERSEDED.value,
            )
        ).all()
    )

    eff_max_chars = max(50, settings.tts_max_chars_per_request - settings.tts_unit_safety_margin)
    unresolved: list[dict] = []
    missing_voices: list[str] = []
    seen_missing: set[str] = set()
    over_limit: list[dict] = []

    for seg in active_segs:
        # Check speaker validity
        if seg.segment_type in SPEAKER_REQUIRED_TYPES:
            speaker_invalid = bool(seg.speaker_character_id and seg.speaker_character_id not in valid_char_ids)
            is_blocked = speaker_invalid or compute_is_render_blocked(
                segment_type=seg.segment_type,
                speaker_character_id=seg.speaker_character_id,
                narrator_key=seg.narrator_key,
                review_status=seg.review_status,
                manual_override=seg.manual_override,
                allow_low_confidence_auto_pass=settings.allow_low_confidence_auto_pass,
            )
            if is_blocked:
                reason = (
                    "角色已失效/已合并"
                    if speaker_invalid
                    else (
                        "说话人待人工审核"
                        if seg.review_status == SegmentReviewStatus.NEEDS_REVIEW.value
                        else "未归属说话人（对话不可静默配为旁白）"
                    )
                )
                unresolved.append(
                    {
                        "segment_id": seg.id,
                        "order_no": seg.order_no,
                        "segment_type": seg.segment_type,
                        "review_status": seg.review_status,
                        "reason": reason,
                        "text_preview": seg.text_content[:80],
                    }
                )
            elif seg.speaker_character_id:
                if seg.speaker_character_id not in by_char and seg.speaker_character_id not in seen_missing:
                    seen_missing.add(seg.speaker_character_id)
                    char = db.get(Character, seg.speaker_character_id)
                    missing_voices.append(f"character:{char.canonical_name if char else seg.speaker_character_id}")
            elif seg.manual_override and seg.narrator_key:
                nk = seg.narrator_key
                if nk not in by_narrator and f"narrator:{nk}" not in seen_missing:
                    seen_missing.add(f"narrator:{nk}")
                    missing_voices.append(f"narrator:{nk}")
        else:
            nk = seg.narrator_key or "default"
            if nk not in by_narrator and f"narrator:{nk}" not in seen_missing:
                seen_missing.add(f"narrator:{nk}")
                missing_voices.append(f"narrator:{nk}")

        if len(seg.text_content) > eff_max_chars:
            units = split_text_to_synthesis_units(seg.text_content, eff_max_chars)
            over_limit.append(
                {
                    "segment_id": seg.id,
                    "order_no": seg.order_no,
                    "char_count": len(seg.text_content),
                    "planned_units": len(units),
                }
            )

    # Check text coverage against authoritative chapters
    chapter_ids = {s.chapter_id for s in segments}
    chapter_texts: dict[str, str] = {}
    if chapter_ids:
        for ch in db.scalars(select(Chapter).where(Chapter.id.in_(chapter_ids))).all():
            chapter_texts[ch.id] = storage.read_text(ch.content_ref)
    text_valid, coverage_errors = verify_segments_against_chapters(
        chapter_texts,
        [(s.chapter_id, s.start_offset, s.end_offset, s.text_content, s.text_hash) for s in segments],
    )

    ready_to_render = (
        len(active_segs) > 0
        and len(unresolved) == 0
        and len(missing_voices) == 0
        and text_valid
    )
    return {
        "episode_id": ep.id,
        "total_segments": len(segments),
        "active_segments": len(active_segs),
        "unresolved_segments": unresolved,
        "missing_voices": missing_voices,
        "over_limit_segments": over_limit,
        "text_coverage_valid": text_valid,
        "coverage_errors": coverage_errors,
        "ready_to_render": ready_to_render,
    }


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
                "stable_span_key": s.stable_span_key,
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
                "is_render_blocked": s.is_render_blocked,
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
                        "is_mock": aud.is_mock,
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
    seg.is_render_blocked = compute_is_render_blocked(
        segment_type=seg.segment_type,
        speaker_character_id=seg.speaker_character_id,
        narrator_key=seg.narrator_key,
        review_status=seg.review_status,
        manual_override=True,
    )

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
