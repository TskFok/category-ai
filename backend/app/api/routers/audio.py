from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.api.deps import get_storage
from app.api.schemas import (
    AudioExportCreateIn,
    AudioExportOut,
    AudioSegmentOut,
    ProviderUsageSummaryOut,
    RenderJobCreateIn,
    RenderJobOut,
)
from app.application import audio_export_service, audio_service
from app.core.config import Settings, get_settings
from app.core.security import Principal, UserRole, require_roles
from app.infrastructure.db.session import SessionLocal, get_db
from app.infrastructure.redis.client import get_redis_client
from app.infrastructure.storage.local import LocalStorage
from worker.tasks.tts_tasks import run_render_job_task

logger = logging.getLogger(__name__)
router = APIRouter(tags=["audio"])


def _run_render_bg(render_job_id: str) -> None:
    db = SessionLocal()
    try:
        settings = get_settings()
        storage = LocalStorage(settings.storage_path)
        audio_service.execute_render_job(db, storage, settings, render_job_id)
    finally:
        db.close()


def _serialize_audio_export(exp) -> AudioExportOut:
    return AudioExportOut(
        id=exp.id,
        novel_id=exp.novel_id,
        episode_id=exp.episode_id,
        format=exp.format,
        status=exp.status,
        file_size_bytes=exp.file_size_bytes,
        duration_ms=exp.duration_ms,
        error_message=exp.error_message,
        download_url=f"/api/v1/audio-exports/{exp.id}/download",
        created_at=exp.created_at,
    )


@router.post(
    "/episodes/{episode_id}/render-jobs",
    response_model=RenderJobOut,
    status_code=status.HTTP_201_CREATED,
)
def create_render_job(
    episode_id: str,
    body: RenderJobCreateIn,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    storage: LocalStorage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
    _principal: Principal = Depends(require_roles(UserRole.ADMIN, UserRole.REVIEWER)),
):
    job = audio_service.create_render_job(
        db,
        settings,
        episode_id=episode_id,
        budget_limit=body.budget_limit,
        idempotency_key=body.idempotency_key,
    )
    if job.status == "pending":
        if body.run_inline:
            job = audio_service.execute_render_job(db, storage, settings, job.id)
        else:
            dispatched = False
            try:
                if get_redis_client() is not None:
                    run_render_job_task.delay(job.id)
                    dispatched = True
            except Exception as exc:
                logger.warning("Celery render dispatch unavailable (%s); falling back to background", exc)
            if not dispatched:
                background_tasks.add_task(_run_render_bg, job.id)
    return job


@router.get("/episodes/{episode_id}/render-jobs", response_model=list[RenderJobOut])
def list_render_jobs(
    episode_id: str,
    db: Session = Depends(get_db),
    _principal: Principal = Depends(require_roles(UserRole.ADMIN, UserRole.REVIEWER, UserRole.READER)),
):
    return audio_service.list_render_jobs(db, episode_id)


@router.get("/render-jobs/{job_id}", response_model=RenderJobOut)
def get_render_job(
    job_id: str,
    db: Session = Depends(get_db),
    _principal: Principal = Depends(require_roles(UserRole.ADMIN, UserRole.REVIEWER, UserRole.READER)),
):
    return audio_service.get_render_job(db, job_id)


@router.post("/script-segments/{segment_id}/regenerate", response_model=AudioSegmentOut)
def regenerate_segment(
    segment_id: str,
    db: Session = Depends(get_db),
    storage: LocalStorage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
    _principal: Principal = Depends(require_roles(UserRole.ADMIN, UserRole.REVIEWER)),
):
    return audio_service.regenerate_script_segment(db, storage, settings, segment_id=segment_id)


@router.get("/audio-segments/{audio_id}/stream")
def stream_audio_segment(
    audio_id: str,
    db: Session = Depends(get_db),
    storage: LocalStorage = Depends(get_storage),
    _principal: Principal = Depends(require_roles(UserRole.ADMIN, UserRole.REVIEWER, UserRole.READER)),
):
    aud, path = audio_service.get_audio_segment_file(db, storage, audio_id)
    media_type = "audio/wav" if aud.audio_format.lower() == "wav" else "audio/mpeg"
    return FileResponse(path=path, media_type=media_type, filename=path.name)


@router.post(
    "/novels/{novel_id}/audio-exports",
    response_model=AudioExportOut,
    status_code=status.HTTP_201_CREATED,
)
def create_audio_export(
    novel_id: str,
    body: AudioExportCreateIn,
    db: Session = Depends(get_db),
    storage: LocalStorage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
    _principal: Principal = Depends(require_roles(UserRole.ADMIN, UserRole.REVIEWER)),
):
    exp = audio_export_service.create_audio_export(
        db,
        storage,
        settings,
        novel_id=novel_id,
        episode_id=body.episode_id,
        plan_id=body.plan_id,
        fmt=body.format,
    )
    return _serialize_audio_export(exp)


@router.get("/novels/{novel_id}/audio-exports", response_model=list[AudioExportOut])
def list_audio_exports(
    novel_id: str,
    db: Session = Depends(get_db),
    _principal: Principal = Depends(require_roles(UserRole.ADMIN, UserRole.REVIEWER, UserRole.READER)),
):
    return [_serialize_audio_export(e) for e in audio_export_service.list_audio_exports(db, novel_id)]


@router.get("/audio-exports/{export_id}/download")
def download_audio_export(
    export_id: str,
    db: Session = Depends(get_db),
    storage: LocalStorage = Depends(get_storage),
    _principal: Principal = Depends(require_roles(UserRole.ADMIN, UserRole.REVIEWER, UserRole.READER)),
):
    exp, path = audio_export_service.get_audio_export_file(db, storage, export_id)
    media_map = {
        "mp3": "audio/mpeg",
        "wav": "audio/wav",
        "zip": "application/zip",
    }
    return FileResponse(
        path=path,
        media_type=media_map.get(exp.format.lower(), "application/octet-stream"),
        filename=f"audiobook_{exp.novel_id[:8]}_{exp.id[:8]}.{exp.format}",
    )


@router.get("/novels/{novel_id}/provider-usage", response_model=ProviderUsageSummaryOut)
def get_provider_usage(
    novel_id: str,
    db: Session = Depends(get_db),
    _principal: Principal = Depends(require_roles(UserRole.ADMIN, UserRole.REVIEWER)),
):
    return audio_service.get_provider_usage_summary(db, novel_id)
