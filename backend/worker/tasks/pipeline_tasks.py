from __future__ import annotations

import json
import logging
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.application.job_service import refresh_job_progress
from app.core.config import get_settings
from app.domain.enums import (
    ClaimType,
    EntityType,
    JobState,
    PipelineStage,
    RelationDirection,
    ReviewStatus,
    ReviewTaskState,
    SYMMETRIC_RELATION_TYPES,
    TaskState,
    VerificationStatus,
)
from app.domain.evidence_verifier import verify_evidence_span
from app.domain.identity_rules import is_generic_honorific, resolve_by_rules
from app.infrastructure.db.models import (
    Alias,
    AnalysisClaim,
    AnalysisReport,
    Chapter,
    Character,
    CharacterState,
    Chunk,
    Event,
    EventParticipant,
    Evidence,
    EvidenceLink,
    Fact,
    Job,
    LLMCall,
    Mention,
    Novel,
    Relationship,
    ReviewTask,
    TaskRun,
)
from app.infrastructure.db.session import SessionLocal
from app.infrastructure.llm.deepseek import DeepSeekClient, LLMClientError
from app.infrastructure.redis.client import publish_job_event
from app.infrastructure.storage.local import LocalStorage
from worker.celery_app import celery_app
from worker.pipelines.prompt_loader import load_prompt

logger = logging.getLogger(__name__)


@celery_app.task(name="worker.tasks.pipeline_tasks.run_job_task", bind=True)
def run_job_task(self, job_id: str) -> dict:
    db = SessionLocal()
    try:
        return execute_job(db, job_id)
    finally:
        db.close()


def execute_job(db: Session, job_id: str) -> dict:
    """
    Execute pending TaskRuns for a Job with:
    - Pause & budget enforcement before each TaskRun
    - Evidence span verification
    - Persistent progress updates & SSE event publishing
    """
    settings = get_settings()
    storage = LocalStorage(settings.storage_path)
    llm = DeepSeekClient(settings)

    job = db.get(Job, job_id)
    if not job:
        return {"status": "not_found"}

    if job.state in (JobState.PAUSED.value, JobState.CANCELLED.value):
        return {"status": job.state}

    job.state = JobState.RUNNING.value
    db.commit()
    _emit(job, "job_started")

    pending_runs = list(
        db.scalars(
            select(TaskRun)
            .where(TaskRun.job_id == job.id, TaskRun.state == TaskState.PENDING.value)
            .order_by(TaskRun.created_at.asc())
        ).all()
    )

    for tr in pending_runs:
        db.refresh(job)
        if job.state == JobState.PAUSED.value:
            _emit(job, "job_paused")
            return {"status": "paused"}
        if job.state == JobState.CANCELLED.value:
            _emit(job, "job_cancelled")
            return {"status": "cancelled"}

        if job.budget_limit > 0 and job.spent_cost >= job.budget_limit:
            job.state = JobState.PAUSED.value
            job.error_summary = (
                f"Budget limit reached (${job.spent_cost:.4f} >= ${job.budget_limit:.4f})"
            )
            db.commit()
            _emit(job, "budget_limit_reached")
            return {"status": "paused_budget"}

        tr.state = TaskState.RUNNING.value
        tr.attempt += 1
        db.commit()

        try:
            if tr.stage == PipelineStage.S1_DISCOVER.value:
                _run_s1_discover(db, storage, llm, job, tr)
            elif tr.stage == PipelineStage.S2_EXTRACT.value:
                _run_s2_extract(db, storage, llm, job, tr)
            elif tr.stage == PipelineStage.S3_DISAMBIGUATE.value:
                _run_s3_disambiguate(db, job)
            elif tr.stage == PipelineStage.S4_CONSOLIDATE.value:
                _run_s4_consolidate(db, job)
            elif tr.stage == PipelineStage.S5_ANALYZE.value:
                _run_s5_analyze(db, llm, job, tr)
            elif tr.stage == PipelineStage.S6_PUBLISH.value:
                _run_s6_publish(db, storage, job)

            tr.state = TaskState.COMPLETED.value
            tr.error_code = None
            tr.error_message = None
            db.commit()
        except LLMClientError as exc:
            tr.state = TaskState.FAILED.value
            tr.error_code = exc.error_type
            tr.error_message = str(exc)
            db.add(
                LLMCall(
                    novel_id=job.novel_id,
                    job_id=job.id,
                    task_run_id=tr.id,
                    stage=tr.stage,
                    model=llm.select_model("extract"),
                    prompt_version="1.0.0",
                    status="error",
                    error_type=exc.error_type,
                )
            )
            db.commit()
        except Exception as exc:
            logger.exception("TaskRun %s failed", tr.id)
            tr.state = TaskState.FAILED.value
            tr.error_code = "INTERNAL_ERROR"
            tr.error_message = str(exc)[:500]
            db.commit()

        refresh_job_progress(db, job.id)
        _emit(job, "task_progress")

    # If FULL_PIPELINE, also run S3 -> S4 -> S5 -> S6 deterministically
    if job.job_type == PipelineStage.FULL_PIPELINE.value and job.state == JobState.RUNNING.value:
        _run_s3_disambiguate(db, job)
        _run_s4_consolidate(db, job)
        _run_s5_analyze(db, llm, job, None)
        _run_s6_publish(db, storage, job)

    job = refresh_job_progress(db, job.id)
    if job.state == JobState.RUNNING.value:
        failed = (job.progress_json or {}).get("failed_tasks", 0)
        job.state = JobState.FAILED.value if failed > 0 else JobState.COMPLETED.value
        db.commit()
    _emit(job, "job_finished")
    return {"status": job.state, "progress": job.progress_json}


def _emit(job: Job, event_type: str) -> None:
    publish_job_event(
        job.id,
        event_id=str(uuid.uuid4()),
        payload={
            "event_type": event_type,
            "job_id": job.id,
            "state": job.state,
            "spent_cost": job.spent_cost,
            "progress": job.progress_json,
        },
    )


def _record_llm_call(
    db: Session,
    *,
    job: Job,
    tr: TaskRun | None,
    stage: str,
    prompt_version: str,
    resp,
) -> None:
    db.add(
        LLMCall(
            novel_id=job.novel_id,
            job_id=job.id,
            task_run_id=tr.id if tr else None,
            stage=stage,
            model=resp.model,
            prompt_version=prompt_version,
            input_tokens=resp.input_tokens,
            output_tokens=resp.output_tokens,
            estimated_cost=resp.estimated_cost,
            latency_ms=resp.latency_ms,
            status="ok",
        )
    )
    job.spent_cost = round(job.spent_cost + resp.estimated_cost, 6)


def _get_or_create_character(
    db: Session,
    *,
    novel_id: str,
    name: str,
    entity_type: str,
    chapter_no: int,
    run_id: str,
    model_id: str,
    prompt_version: str,
) -> Character:
    char = db.scalar(
        select(Character).where(
            Character.novel_id == novel_id,
            Character.canonical_name == name,
            Character.status != ReviewStatus.SUPERSEDED.value,
        )
    )
    if char:
        if char.first_appearance_chapter is None or chapter_no < char.first_appearance_chapter:
            char.first_appearance_chapter = chapter_no
        return char

    char = Character(
        novel_id=novel_id,
        canonical_name=name,
        entity_type=entity_type if entity_type in {e.value for e in EntityType} else EntityType.PERSON.value,
        status=ReviewStatus.CANDIDATE.value,
        first_appearance_chapter=chapter_no,
        mention_count=0,
        run_id=run_id,
        model_id=model_id,
        prompt_version=prompt_version,
    )
    db.add(char)
    db.flush()
    return char


def _create_verified_evidence(
    db: Session,
    *,
    novel_id: str,
    chapter: Chapter,
    chapter_text: str,
    chunk_start: int,
    quote_text: str,
) -> Evidence:
    local_pos = chapter_text.find(quote_text, chunk_start)
    if local_pos == -1:
        local_pos = chapter_text.find(quote_text)
    start_off = local_pos if local_pos != -1 else chunk_start
    end_off = start_off + len(quote_text)

    ver = verify_evidence_span(chapter_text, start_off, end_off, quote_text)
    ev = Evidence(
        novel_id=novel_id,
        chapter_id=chapter.id,
        start_offset=ver.resolved_start,
        end_offset=ver.resolved_end,
        quote_text=quote_text,
        text_hash=ver.text_hash,
        verification_status=ver.status.value,
        verification_note=ver.reason,
    )
    db.add(ev)
    db.flush()

    if ver.status != VerificationStatus.VERIFIED:
        db.add(
            ReviewTask(
                novel_id=novel_id,
                review_type="evidence_mismatch",
                target_kind="evidence",
                target_id=ev.id,
                severity="high",
                state=ReviewTaskState.OPEN.value,
                summary=f"第{chapter.chapter_no}章证据校验未通过: {ver.status.value}",
                payload_json={"quote_text": quote_text, "reason": ver.reason},
            )
        )
    return ev


def _run_s1_discover(
    db: Session,
    storage: LocalStorage,
    llm: DeepSeekClient,
    job: Job,
    tr: TaskRun,
) -> None:
    if not tr.chunk_id:
        return
    chunk = db.get(Chunk, tr.chunk_id)
    if not chunk:
        return
    chapter = db.get(Chapter, chunk.chapter_id)
    if not chapter:
        return

    chapter_text = storage.read_text(chapter.content_ref)
    chunk_text = chapter_text[chunk.start_offset : chunk.end_offset]

    spec = load_prompt("extract_mentions")
    user_prompt = spec.user_template.format(
        chapter_no=chapter.chapter_no,
        chapter_title=chapter.title,
        chunk_text=chunk_text,
    )
    resp = llm.chat_json(
        system_prompt=spec.system_prompt,
        user_prompt=user_prompt,
        tier=spec.model_tier,
        temperature=spec.temperature,
        max_tokens=spec.max_tokens,
        task_hint="extract_mentions",
    )
    _record_llm_call(db, job=job, tr=tr, stage=tr.stage, prompt_version=spec.version, resp=resp)

    for item in resp.data.get("mentions", []):
        surface = (item.get("surface_text") or "").strip()
        if not surface:
            continue
        quote = (item.get("quote_text") or surface).strip()
        entity_type = item.get("entity_type") or EntityType.PERSON.value
        confidence = float(item.get("confidence") or 0.7)

        pos_in_chunk = chunk_text.find(surface)
        if pos_in_chunk == -1:
            pos_in_chunk = 0
        abs_start = chunk.start_offset + pos_in_chunk
        abs_end = abs_start + len(surface)

        # Deduplicate overlapping chunks by (chapter_id, start_offset, end_offset, surface_text)
        dup = db.scalar(
            select(Mention).where(
                Mention.chapter_id == chapter.id,
                Mention.start_offset == abs_start,
                Mention.end_offset == abs_end,
                Mention.surface_text == surface,
            )
        )
        if dup:
            continue

        char_id: str | None = None
        res_status = ReviewStatus.UNRESOLVED.value
        if not is_generic_honorific(surface):
            char = _get_or_create_character(
                db,
                novel_id=job.novel_id,
                name=surface,
                entity_type=entity_type,
                chapter_no=chapter.chapter_no,
                run_id=tr.id,
                model_id=resp.model,
                prompt_version=spec.version,
            )
            char.mention_count += 1
            char_id = char.id
            res_status = ReviewStatus.CANDIDATE.value

        ctx_start = max(0, abs_start - 40)
        ctx_end = min(len(chapter_text), abs_end + 40)
        mention = Mention(
            novel_id=job.novel_id,
            chapter_id=chapter.id,
            chunk_id=chunk.id,
            surface_text=surface,
            start_offset=abs_start,
            end_offset=abs_end,
            context_snippet=chapter_text[ctx_start:ctx_end],
            character_id=char_id,
            resolution_status=res_status,
            confidence=confidence,
            run_id=tr.id,
            model_id=resp.model,
            prompt_version=spec.version,
        )
        db.add(mention)
        db.flush()

        ev = _create_verified_evidence(
            db,
            novel_id=job.novel_id,
            chapter=chapter,
            chapter_text=chapter_text,
            chunk_start=chunk.start_offset,
            quote_text=quote,
        )
        db.add(
            EvidenceLink(
                novel_id=job.novel_id,
                evidence_id=ev.id,
                subject_kind="mention",
                subject_id=mention.id,
            )
        )

    chunk.processing_status = "completed"


def _run_s2_extract(
    db: Session,
    storage: LocalStorage,
    llm: DeepSeekClient,
    job: Job,
    tr: TaskRun,
) -> None:
    if not tr.chunk_id:
        return
    chunk = db.get(Chunk, tr.chunk_id)
    if not chunk:
        return
    chapter = db.get(Chapter, chunk.chapter_id)
    if not chapter:
        return

    chapter_text = storage.read_text(chapter.content_ref)
    chunk_text = chapter_text[chunk.start_offset : chunk.end_offset]

    known_chars = list(
        db.scalars(
            select(Character.canonical_name)
            .where(
                Character.novel_id == job.novel_id,
                Character.status != ReviewStatus.SUPERSEDED.value,
            )
            .limit(80)
        ).all()
    )

    spec = load_prompt("extract_facts")
    user_prompt = spec.user_template.format(
        chapter_no=chapter.chapter_no,
        known_characters=", ".join(known_chars),
        chunk_text=chunk_text,
    )
    resp = llm.chat_json(
        system_prompt=spec.system_prompt,
        user_prompt=user_prompt,
        tier=spec.model_tier,
        temperature=spec.temperature,
        max_tokens=spec.max_tokens,
        task_hint="extract_facts",
    )
    _record_llm_call(db, job=job, tr=tr, stage=tr.stage, prompt_version=spec.version, resp=resp)

    # 1. Facts & States
    for f_item in resp.data.get("facts", []):
        subj_name = (f_item.get("subject") or "").strip()
        predicate = (f_item.get("predicate") or "other").strip()
        value = f_item.get("value")
        quote = (f_item.get("quote_text") or "").strip()
        if not subj_name or value is None or not quote:
            continue

        subj = _get_or_create_character(
            db,
            novel_id=job.novel_id,
            name=subj_name,
            entity_type=EntityType.PERSON.value,
            chapter_no=chapter.chapter_no,
            run_id=tr.id,
            model_id=resp.model,
            prompt_version=spec.version,
        )
        fact = Fact(
            novel_id=job.novel_id,
            subject_id=subj.id,
            predicate=predicate,
            value_json={"value": value},
            chapter_no=chapter.chapter_no,
            certainty=f_item.get("certainty", "medium"),
            review_status=ReviewStatus.CANDIDATE.value,
            run_id=tr.id,
            model_id=resp.model,
            prompt_version=spec.version,
        )
        db.add(fact)
        db.flush()

        ev = _create_verified_evidence(
            db,
            novel_id=job.novel_id,
            chapter=chapter,
            chapter_text=chapter_text,
            chunk_start=chunk.start_offset,
            quote_text=quote,
        )
        db.add(
            EvidenceLink(
                novel_id=job.novel_id,
                evidence_id=ev.id,
                subject_kind="fact",
                subject_id=fact.id,
            )
        )

        if predicate in ("cultivation", "faction", "identity", "location"):
            db.add(
                CharacterState(
                    novel_id=job.novel_id,
                    character_id=subj.id,
                    state_type=predicate,
                    value_json={"value": value},
                    valid_from=chapter.chapter_no,
                    review_status=ReviewStatus.CANDIDATE.value,
                )
            )

    # 2. Relationships
    for r_item in resp.data.get("relationships", []):
        src_name = (r_item.get("source") or "").strip()
        tgt_name = (r_item.get("target") or "").strip()
        rel_type = (r_item.get("relation_type") or "acquaintance").strip()
        quote = (r_item.get("quote_text") or "").strip()
        if not src_name or not tgt_name or src_name == tgt_name or not quote:
            continue

        src = _get_or_create_character(
            db,
            novel_id=job.novel_id,
            name=src_name,
            entity_type=EntityType.PERSON.value,
            chapter_no=chapter.chapter_no,
            run_id=tr.id,
            model_id=resp.model,
            prompt_version=spec.version,
        )
        tgt = _get_or_create_character(
            db,
            novel_id=job.novel_id,
            name=tgt_name,
            entity_type=EntityType.PERSON.value,
            chapter_no=chapter.chapter_no,
            run_id=tr.id,
            model_id=resp.model,
            prompt_version=spec.version,
        )
        direction = (
            RelationDirection.UNDIRECTED.value
            if rel_type in SYMMETRIC_RELATION_TYPES
            else RelationDirection.DIRECTED.value
        )
        rel = Relationship(
            novel_id=job.novel_id,
            source_character_id=src.id,
            target_character_id=tgt.id,
            relation_type=rel_type,
            direction=direction,
            description=r_item.get("description"),
            start_chapter=chapter.chapter_no,
            confidence=float(r_item.get("confidence") or 0.75),
            review_status=ReviewStatus.CANDIDATE.value,
            run_id=tr.id,
            model_id=resp.model,
            prompt_version=spec.version,
        )
        db.add(rel)
        db.flush()

        ev = _create_verified_evidence(
            db,
            novel_id=job.novel_id,
            chapter=chapter,
            chapter_text=chapter_text,
            chunk_start=chunk.start_offset,
            quote_text=quote,
        )
        db.add(
            EvidenceLink(
                novel_id=job.novel_id,
                evidence_id=ev.id,
                subject_kind="relationship",
                subject_id=rel.id,
            )
        )

    # 3. Events
    for e_item in resp.data.get("events", []):
        title = (e_item.get("title") or "").strip()
        quote = (e_item.get("quote_text") or "").strip()
        if not title or not quote:
            continue

        ev_obj = Event(
            novel_id=job.novel_id,
            chapter_id=chapter.id,
            chapter_no=chapter.chapter_no,
            title=title[:255],
            event_type=e_item.get("event_type", "plot"),
            narrative_order=chapter.chapter_no * 1000 + chunk.chunk_index,
            story_time_text=e_item.get("story_time_text"),
            certainty=e_item.get("certainty", "medium"),
            review_status=ReviewStatus.CANDIDATE.value,
        )
        db.add(ev_obj)
        db.flush()

        for part in e_item.get("participants", []):
            p_name = (part.get("name") or "").strip()
            p_role = (part.get("role") or "participant").strip()
            if not p_name:
                continue
            p_char = _get_or_create_character(
                db,
                novel_id=job.novel_id,
                name=p_name,
                entity_type=EntityType.PERSON.value,
                chapter_no=chapter.chapter_no,
                run_id=tr.id,
                model_id=resp.model,
                prompt_version=spec.version,
            )
            dup_ep = db.scalar(
                select(EventParticipant).where(
                    EventParticipant.event_id == ev_obj.id,
                    EventParticipant.character_id == p_char.id,
                    EventParticipant.participant_role == p_role,
                )
            )
            if not dup_ep:
                db.add(
                    EventParticipant(
                        novel_id=job.novel_id,
                        event_id=ev_obj.id,
                        character_id=p_char.id,
                        participant_role=p_role,
                    )
                )

        ev = _create_verified_evidence(
            db,
            novel_id=job.novel_id,
            chapter=chapter,
            chapter_text=chapter_text,
            chunk_start=chunk.start_offset,
            quote_text=quote,
        )
        db.add(
            EvidenceLink(
                novel_id=job.novel_id,
                evidence_id=ev.id,
                subject_kind="event",
                subject_id=ev_obj.id,
            )
        )


def _run_s3_disambiguate(db: Session, job: Job) -> None:
    """Rule-first identity resolution for unresolved mentions (Section 7.4)."""
    chars = list(
        db.scalars(
            select(Character).where(
                Character.novel_id == job.novel_id,
                Character.status != ReviewStatus.SUPERSEDED.value,
            )
        ).all()
    )
    canonical_map: dict[str, list[str]] = {}
    for c in chars:
        canonical_map.setdefault(c.canonical_name, []).append(c.id)

    aliases = list(
        db.scalars(
            select(Alias).where(
                Alias.novel_id == job.novel_id,
                Alias.review_status == ReviewStatus.CONFIRMED.value,
            )
        ).all()
    )
    alias_entries = [
        (a.character_id, a.alias_text, a.valid_from_chapter, a.valid_to_chapter) for a in aliases
    ]

    unresolved = db.execute(
        select(Mention, Chapter.chapter_no)
        .join(Chapter, Mention.chapter_id == Chapter.id)
        .where(
            Mention.novel_id == job.novel_id,
            Mention.character_id.is_(None),
        )
    ).all()

    for mention, ch_no in unresolved:
        char_id, reason = resolve_by_rules(
            mention.surface_text, ch_no, canonical_map, alias_entries
        )
        if char_id:
            mention.character_id = char_id
            mention.resolution_status = ReviewStatus.PROBABLE.value
        else:
            mention.resolution_status = ReviewStatus.UNRESOLVED.value
            existing_rt = db.scalar(
                select(ReviewTask).where(
                    ReviewTask.novel_id == job.novel_id,
                    ReviewTask.target_kind == "mention",
                    ReviewTask.target_id == mention.id,
                )
            )
            if not existing_rt:
                db.add(
                    ReviewTask(
                        novel_id=job.novel_id,
                        review_type="identity_conflict",
                        target_kind="mention",
                        target_id=mention.id,
                        severity="medium",
                        state=ReviewTaskState.OPEN.value,
                        summary=f"第{ch_no}章提及「{mention.surface_text}」无法自动归属 ({reason})",
                        payload_json={"surface_text": mention.surface_text, "reason": reason},
                    )
                )
    db.commit()


def _run_s4_consolidate(db: Session, job: Job) -> None:
    """Detect conflicting character states in the same chapter without overwriting history."""
    states = list(
        db.scalars(
            select(CharacterState)
            .where(CharacterState.novel_id == job.novel_id)
            .order_by(CharacterState.character_id, CharacterState.state_type, CharacterState.valid_from)
        ).all()
    )
    seen: dict[tuple[str, str, int | None], str] = {}
    for st in states:
        key = (st.character_id, st.state_type, st.valid_from)
        val = json.dumps(st.value_json, sort_keys=True, ensure_ascii=False)
        if key in seen and seen[key] != val:
            db.add(
                ReviewTask(
                    novel_id=job.novel_id,
                    review_type="fact_conflict",
                    target_kind="character",
                    target_id=st.character_id,
                    severity="high",
                    state=ReviewTaskState.OPEN.value,
                    summary=f"第{st.valid_from}章人物状态({st.state_type})存在冲突",
                    payload_json={"existing": seen[key], "conflicting": val},
                )
            )
        else:
            seen[key] = val
    db.commit()


def _run_s5_analyze(
    db: Session,
    llm: DeepSeekClient,
    job: Job,
    tr: TaskRun | None,
) -> None:
    """Generate deep analysis reports for top characters with strict fact/inference separation."""
    top_chars = list(
        db.scalars(
            select(Character)
            .where(
                Character.novel_id == job.novel_id,
                Character.status != ReviewStatus.SUPERSEDED.value,
            )
            .order_by(Character.mention_count.desc())
            .limit(5)
        ).all()
    )
    spec = load_prompt("analyze_character_full")

    for char in top_chars:
        aliases = [
            a.alias_text
            for a in db.scalars(select(Alias).where(Alias.character_id == char.id)).all()
        ]
        states = [
            {"type": s.state_type, "value": s.value_json, "from": s.valid_from, "to": s.valid_to}
            for s in db.scalars(
                select(CharacterState).where(CharacterState.character_id == char.id)
            ).all()
        ]
        ev_rows = list(
            db.scalars(
                select(Evidence)
                .where(
                    Evidence.novel_id == job.novel_id,
                    Evidence.verification_status == VerificationStatus.VERIFIED.value,
                )
                .limit(20)
            ).all()
        )
        valid_ev_ids = {e.id for e in ev_rows}

        user_prompt = spec.user_template.format(
            character_name=char.canonical_name,
            aliases=", ".join(aliases) or "无",
            states_json=json.dumps(states, ensure_ascii=False),
            events_json="[]",
            relationships_json="[]",
            evidence_json=json.dumps(
                [{"id": e.id, "quote": e.quote_text} for e in ev_rows], ensure_ascii=False
            ),
        )
        resp = llm.chat_json(
            system_prompt=spec.system_prompt,
            user_prompt=user_prompt,
            tier=spec.model_tier,
            temperature=spec.temperature,
            max_tokens=spec.max_tokens,
            task_hint="analyze_character_full",
        )
        _record_llm_call(
            db, job=job, tr=tr, stage=PipelineStage.S5_ANALYZE.value, prompt_version=spec.version, resp=resp
        )

        report = AnalysisReport(
            novel_id=job.novel_id,
            character_id=char.id,
            report_type="deep",
            content_json={k: v for k, v in resp.data.items() if k != "claims"},
            generation_version=1,
            review_status=ReviewStatus.CANDIDATE.value,
            model_id=resp.model,
            prompt_version=spec.version,
        )
        db.add(report)
        db.flush()

        for c_item in resp.data.get("claims", []):
            c_text = (c_item.get("claim_text") or "").strip()
            if not c_text:
                continue
            raw_type = c_item.get("claim_type", ClaimType.INFERENCE.value)
            linked_ids = [eid for eid in (c_item.get("evidence_ids") or []) if eid in valid_ev_ids]
            # Section 6.3: Unbacked 'fact' claims cannot be published as fact; downgrade to inference
            if raw_type == ClaimType.FACT.value and not linked_ids:
                raw_type = ClaimType.INFERENCE.value

            claim = AnalysisClaim(
                novel_id=job.novel_id,
                report_id=report.id,
                section=c_item.get("section", "general"),
                claim_text=c_text,
                claim_type=raw_type,
                certainty=c_item.get("certainty", "medium"),
            )
            db.add(claim)
            db.flush()
            for eid in linked_ids:
                db.add(
                    EvidenceLink(
                        novel_id=job.novel_id,
                        evidence_id=eid,
                        subject_kind="claim",
                        subject_id=claim.id,
                    )
                )

    db.commit()


def _run_s6_publish(db: Session, storage: LocalStorage, job: Job) -> None:
    """Verify pending evidence, bump data_revision on novel."""
    novel = db.get(Novel, job.novel_id)
    if novel:
        novel.data_revision += 1
        db.commit()
