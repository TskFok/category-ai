import asyncio
import json
import logging
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.api.schemas import JobCreateIn, JobOut, PaginatedResponse, UsageSummaryOut
from app.application import job_service
from app.core.config import Settings, get_settings
from app.core.security import Principal, UserRole, require_roles
from app.domain.enums import JobState
from app.infrastructure.db.session import SessionLocal, get_db
from app.infrastructure.redis.client import get_redis_client
from worker.tasks.pipeline_tasks import execute_job, run_job_task

logger = logging.getLogger(__name__)
router = APIRouter(tags=["jobs"])


def _run_job_in_background(job_id: str) -> None:
    db = SessionLocal()
    try:
        execute_job(db, job_id)
    finally:
        db.close()


def _dispatch_job(job_id: str, run_inline: bool, background_tasks: BackgroundTasks) -> None:
    if run_inline:
        background_tasks.add_task(_run_job_in_background, job_id)
        return
    try:
        redis_client = get_redis_client()
        if redis_client is not None:
            run_job_task.delay(job_id)
            return
    except Exception as exc:
        logger.warning("Celery dispatch unavailable (%s); falling back to background task", exc)
    background_tasks.add_task(_run_job_in_background, job_id)


@router.post("/novels/{novel_id}/jobs", response_model=JobOut, status_code=status.HTTP_201_CREATED)
def create_job(
    novel_id: str,
    body: JobCreateIn,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    principal: Principal = Depends(require_roles(UserRole.ADMIN)),
):
    job = job_service.create_analysis_job(
        db,
        settings,
        novel_id=novel_id,
        job_type=body.job_type,
        requested_by=principal.user_id,
        budget_limit=body.budget_limit,
        chapter_from=body.chapter_from,
        chapter_to=body.chapter_to,
    )
    _dispatch_job(job.id, body.run_inline, background_tasks)
    return job


@router.get("/novels/{novel_id}/jobs", response_model=PaginatedResponse[JobOut])
def list_novel_jobs(
    novel_id: str,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
    _principal: Principal = Depends(require_roles(UserRole.ADMIN, UserRole.REVIEWER, UserRole.READER)),
):
    items, total = job_service.list_jobs(db, novel_id, page=page, page_size=page_size)
    return PaginatedResponse(items=items, page=page, page_size=page_size, total=total)


@router.get("/jobs/{job_id}", response_model=JobOut)
def get_job(
    job_id: str,
    db: Session = Depends(get_db),
    _principal: Principal = Depends(require_roles(UserRole.ADMIN, UserRole.REVIEWER, UserRole.READER)),
):
    return job_service.get_job(db, job_id)


@router.post("/jobs/{job_id}/pause", response_model=JobOut)
def pause_job(
    job_id: str,
    db: Session = Depends(get_db),
    _principal: Principal = Depends(require_roles(UserRole.ADMIN)),
):
    return job_service.pause_job(db, job_id)


@router.post("/jobs/{job_id}/resume", response_model=JobOut)
def resume_job(
    job_id: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    _principal: Principal = Depends(require_roles(UserRole.ADMIN)),
):
    job = job_service.resume_job(db, job_id)
    _dispatch_job(job.id, False, background_tasks)
    return job


@router.get("/jobs/{job_id}/events")
async def stream_job_events(
    job_id: str,
    db: Session = Depends(get_db),
    _principal: Principal = Depends(require_roles(UserRole.ADMIN, UserRole.REVIEWER, UserRole.READER)),
):
    """
    Server-Sent Events (SSE) stream for live job progress (Section 4.2 & 9).
    Emits unique `id:` per event for client-side deduplication and terminates cleanly
    when the job reaches a terminal state.
    """
    job_service.get_job(db, job_id)

    async def event_generator():
        last_snapshot = None
        for _ in range(300):
            poll_db = SessionLocal()
            try:
                job = job_service.get_job(poll_db, job_id)
                payload = {
                    "job_id": job.id,
                    "state": job.state,
                    "spent_cost": job.spent_cost,
                    "budget_limit": job.budget_limit,
                    "progress": job.progress_json,
                    "error_summary": job.error_summary,
                }
            finally:
                poll_db.close()

            encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            if encoded != last_snapshot:
                last_snapshot = encoded
                event_id = str(uuid.uuid4())
                yield f"id: {event_id}\nevent: job_update\ndata: {encoded}\n\n"

            if payload["state"] in (
                JobState.COMPLETED.value,
                JobState.FAILED.value,
                JobState.CANCELLED.value,
                JobState.PAUSED.value,
            ):
                break
            await asyncio.sleep(1.0)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/novels/{novel_id}/usage", response_model=UsageSummaryOut)
def get_usage(
    novel_id: str,
    db: Session = Depends(get_db),
    _principal: Principal = Depends(require_roles(UserRole.ADMIN, UserRole.REVIEWER)),
):
    return job_service.get_novel_usage_summary(db, novel_id)
