from __future__ import annotations

import hashlib
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import AppError
from app.domain.enums import JobState, PipelineStage, TaskState
from app.infrastructure.db.models import Chapter, Chunk, Job, LLMCall, Novel, TaskRun


def make_idempotency_key(
    novel_id: str,
    source_version: int,
    stage: str,
    target_id: str,
    input_hash: str,
    prompt_version: str,
    model_name: str,
) -> str:
    raw = f"{novel_id}:{source_version}:{stage}:{target_id}:{input_hash}:{prompt_version}:{model_name}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def create_analysis_job(
    db: Session,
    settings: Settings,
    *,
    novel_id: str,
    job_type: str,
    requested_by: str,
    budget_limit: float | None = None,
    chapter_from: int | None = None,
    chapter_to: int | None = None,
    character_ids: list[str] | None = None,
) -> Job:
    novel = db.get(Novel, novel_id)
    if not novel:
        raise AppError(code="NOVEL_NOT_FOUND", message="Novel not found", status_code=404)

    valid_stages = {s.value for s in PipelineStage}
    if job_type not in valid_stages:
        raise AppError(
            code="INVALID_JOB_TYPE",
            message=f"Unsupported job_type '{job_type}'",
            status_code=400,
        )

    eff_budget = budget_limit if budget_limit is not None else settings.job_budget_limit

    job = Job(
        novel_id=novel.id,
        job_type=job_type,
        state=JobState.PENDING.value,
        requested_by=requested_by,
        budget_limit=eff_budget,
        spent_cost=0.0,
        chapter_from=chapter_from,
        chapter_to=chapter_to,
        progress_json={
            "total_tasks": 0,
            "completed_tasks": 0,
            "failed_tasks": 0,
            "pending_tasks": 0,
            "current_stage": job_type,
        },
    )
    db.add(job)
    db.flush()

    # Determine target chunks
    chunk_stmt = (
        select(Chunk)
        .join(Chapter, Chunk.chapter_id == Chapter.id)
        .where(Chunk.novel_id == novel.id)
        .order_by(Chapter.chapter_no.asc(), Chunk.chunk_index.asc())
    )
    if chapter_from is not None:
        chunk_stmt = chunk_stmt.where(Chapter.chapter_no >= chapter_from)
    if chapter_to is not None:
        chunk_stmt = chunk_stmt.where(Chapter.chapter_no <= chapter_to)
    chunks = list(db.scalars(chunk_stmt).all())

    stages_to_seed: list[str]
    if job_type == PipelineStage.FULL_PIPELINE.value:
        # FIX-03: Persist TaskRuns for every stage in the DAG (S1 -> S6)
        stages_to_seed = [
            PipelineStage.S1_DISCOVER.value,
            PipelineStage.S2_EXTRACT.value,
            PipelineStage.S3_DISAMBIGUATE.value,
            PipelineStage.S4_CONSOLIDATE.value,
            PipelineStage.S5_ANALYZE.value,
            PipelineStage.S6_PUBLISH.value,
        ]
    elif job_type in (PipelineStage.S1_DISCOVER.value, PipelineStage.S2_EXTRACT.value):
        stages_to_seed = [job_type]
    else:
        stages_to_seed = [job_type]

    created_count = 0
    for st in stages_to_seed:
        if st in (PipelineStage.S1_DISCOVER.value, PipelineStage.S2_EXTRACT.value):
            for ch in chunks:
                idem = make_idempotency_key(
                    novel.id,
                    novel.source_version,
                    st,
                    ch.id,
                    ch.text_hash,
                    "1.0.0",
                    settings.deepseek_extract_model,
                )
                existing = db.scalar(select(TaskRun).where(TaskRun.idempotency_key == idem))
                if existing and existing.state == TaskState.COMPLETED.value:
                    # Already completed previously: do not double-charge
                    continue
                if existing:
                    existing.job_id = job.id
                    existing.state = TaskState.PENDING.value
                    created_count += 1
                    continue
                db.add(
                    TaskRun(
                        novel_id=novel.id,
                        job_id=job.id,
                        stage=st,
                        chunk_id=ch.id,
                        input_hash=ch.text_hash,
                        idempotency_key=idem,
                        state=TaskState.PENDING.value,
                    )
                )
                created_count += 1
        elif st == PipelineStage.S5_ANALYZE.value and character_ids:
            # FIX-01: Support per-character incremental S5 reruns
            for cid in character_ids:
                idem = make_idempotency_key(
                    novel.id,
                    novel.source_version,
                    st,
                    f"{job.id}:{cid}",
                    novel.source_hash,
                    "1.0.0",
                    settings.deepseek_analysis_model,
                )
                db.add(
                    TaskRun(
                        novel_id=novel.id,
                        job_id=job.id,
                        stage=st,
                        chunk_id=None,
                        character_id=cid,
                        input_hash=novel.source_hash,
                        idempotency_key=idem,
                        state=TaskState.PENDING.value,
                    )
                )
                created_count += 1
        else:
            idem = make_idempotency_key(
                novel.id,
                novel.source_version,
                st,
                job.id,
                novel.source_hash,
                "1.0.0",
                settings.deepseek_analysis_model,
            )
            db.add(
                TaskRun(
                    novel_id=novel.id,
                    job_id=job.id,
                    stage=st,
                    chunk_id=None,
                    input_hash=novel.source_hash,
                    idempotency_key=idem,
                    state=TaskState.PENDING.value,
                )
            )
            created_count += 1

    job.progress_json = {
        "total_tasks": created_count,
        "completed_tasks": 0,
        "failed_tasks": 0,
        "skipped_tasks": 0,
        "pending_tasks": created_count,
        "completion_pct": 0.0,
        "current_stage": stages_to_seed[0] if stages_to_seed else job_type,
    }
    db.commit()
    db.refresh(job)
    return job


def get_job(db: Session, job_id: str) -> Job:
    job = db.get(Job, job_id)
    if not job:
        raise AppError(code="JOB_NOT_FOUND", message="Job not found", status_code=404)
    return job


def list_jobs(db: Session, novel_id: str, page: int = 1, page_size: int = 20) -> tuple[list[Job], int]:
    total = db.scalar(select(func.count()).select_from(Job).where(Job.novel_id == novel_id)) or 0
    rows = (
        db.scalars(
            select(Job)
            .where(Job.novel_id == novel_id)
            .order_by(Job.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        .all()
    )
    return list(rows), total


def pause_job(db: Session, job_id: str) -> Job:
    job = get_job(db, job_id)
    if job.state in (JobState.COMPLETED.value, JobState.CANCELLED.value):
        raise AppError(
            code="INVALID_JOB_TRANSITION",
            message=f"Cannot pause job in state '{job.state}'",
            status_code=409,
        )
    job.state = JobState.PAUSED.value
    db.commit()
    db.refresh(job)
    return job


def resume_job(db: Session, job_id: str) -> Job:
    job = get_job(db, job_id)
    if job.state not in (
        JobState.PAUSED.value,
        JobState.FAILED.value,
        JobState.PARTIAL_FAILED.value,
        JobState.PENDING.value,
    ):
        raise AppError(
            code="INVALID_JOB_TRANSITION",
            message=f"Cannot resume job in state '{job.state}'",
            status_code=409,
        )
    # Reset failed and skipped downstream tasks to pending so they can be retried
    retry_runs = db.scalars(
        select(TaskRun).where(
            TaskRun.job_id == job.id,
            TaskRun.state.in_([TaskState.FAILED.value, TaskState.SKIPPED.value]),
        )
    ).all()
    for tr in retry_runs:
        tr.state = TaskState.PENDING.value
        tr.error_code = None
        tr.error_message = None

    job.state = JobState.RUNNING.value
    job.error_summary = None
    db.commit()
    db.refresh(job)
    return job


def refresh_job_progress(db: Session, job_id: str) -> Job:
    job = get_job(db, job_id)
    runs = db.scalars(select(TaskRun).where(TaskRun.job_id == job.id)).all()
    total = len(runs)
    completed = sum(1 for r in runs if r.state == TaskState.COMPLETED.value)
    failed = sum(1 for r in runs if r.state == TaskState.FAILED.value)
    skipped = sum(1 for r in runs if r.state == TaskState.SKIPPED.value)
    pending = sum(1 for r in runs if r.state in (TaskState.PENDING.value, TaskState.RUNNING.value))
    pct = round(((completed + failed + skipped) / total) * 100.0, 2) if total > 0 else 100.0

    spent = (
        db.scalar(
            select(func.coalesce(func.sum(LLMCall.estimated_cost), 0.0)).where(
                LLMCall.job_id == job.id
            )
        )
        or 0.0
    )
    job.spent_cost = round(float(spent), 6)
    job.progress_json = {
        **(job.progress_json or {}),
        "total_tasks": total,
        "completed_tasks": completed,
        "failed_tasks": failed,
        "skipped_tasks": skipped,
        "pending_tasks": pending,
        "completion_pct": pct,
    }
    db.commit()
    db.refresh(job)
    return job


def get_novel_usage_summary(db: Session, novel_id: str) -> dict:
    rows = db.execute(
        select(
            LLMCall.stage,
            LLMCall.model,
            func.count(LLMCall.id),
            func.coalesce(func.sum(LLMCall.input_tokens), 0),
            func.coalesce(func.sum(LLMCall.output_tokens), 0),
            func.coalesce(func.sum(LLMCall.estimated_cost), 0.0),
        )
        .where(LLMCall.novel_id == novel_id)
        .group_by(LLMCall.stage, LLMCall.model)
    ).all()

    by_stage = []
    total_in = 0
    total_out = 0
    total_cost = 0.0
    for stage, model, calls, in_tok, out_tok, cost in rows:
        in_tok = int(in_tok)
        out_tok = int(out_tok)
        cost = round(float(cost), 6)
        total_in += in_tok
        total_out += out_tok
        total_cost += cost
        by_stage.append(
            {
                "stage": stage,
                "model": model,
                "call_count": int(calls),
                "input_tokens": in_tok,
                "output_tokens": out_tok,
                "estimated_cost": cost,
            }
        )

    return {
        "novel_id": novel_id,
        "total_input_tokens": total_in,
        "total_output_tokens": total_out,
        "total_estimated_cost": round(total_cost, 6),
        "by_stage": by_stage,
    }
