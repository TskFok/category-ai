from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, Response, status
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy.orm import Session

from app.api.deps import get_storage
from app.api.schemas import (
    AudioExportCreateIn,
    AudioExportOut,
    AudioSegmentOut,
    BookRenderJobCreateIn,
    BookRenderJobOut,
    MediaTokenOut,
    MediaTokenRequestIn,
    NovelAudioReadinessOut,
    ProviderUsageSummaryOut,
    RenderJobCreateIn,
    RenderJobOut,
)
from app.application import audio_export_service, audio_service
from app.core.config import Settings, get_settings
from app.core.security import (
    Principal,
    UserRole,
    create_signed_media_token,
    get_current_principal,
    require_roles,
)
from app.infrastructure.db.session import SessionLocal, get_db
from app.infrastructure.redis.client import get_redis_client
from app.infrastructure.storage.local import LocalStorage
from worker.tasks.tts_tasks import run_book_render_job_task, run_render_job_task

logger = logging.getLogger(__name__)
router = APIRouter(tags=["audio"])


def _run_render_bg(render_job_id: str, only_failed: bool = False) -> None:
    db = SessionLocal()
    try:
        settings = get_settings()
        storage = LocalStorage(settings.storage_path)
        audio_service.execute_render_job(db, storage, settings, render_job_id, only_failed=only_failed)
    finally:
        db.close()


def _run_book_render_bg(book_job_id: str) -> None:
    db = SessionLocal()
    try:
        settings = get_settings()
        storage = LocalStorage(settings.storage_path)
        audio_service.execute_book_render_job(db, storage, settings, book_job_id)
    finally:
        db.close()


def _dispatch_render_job(
    background_tasks: BackgroundTasks,
    render_job_id: str,
    *,
    only_failed: bool = False,
) -> None:
    dispatched = False
    try:
        if get_redis_client() is not None:
            run_render_job_task.delay(render_job_id, only_failed=only_failed)
            dispatched = True
    except Exception as exc:
        logger.warning("Celery render dispatch unavailable (%s); falling back to background", exc)
    if not dispatched:
        background_tasks.add_task(_run_render_bg, render_job_id, only_failed)


def _serialize_audio_export(exp) -> AudioExportOut:
    return AudioExportOut(
        id=exp.id,
        novel_id=exp.novel_id,
        episode_id=exp.episode_id,
        plan_id=exp.plan_id,
        format=exp.format,
        actual_format=exp.actual_format or exp.format,
        status=exp.status,
        file_size_bytes=exp.file_size_bytes,
        duration_ms=exp.duration_ms,
        checksum=exp.checksum,
        source_snapshot_hash=exp.source_snapshot_hash,
        is_stale=exp.is_stale,
        is_mock=exp.is_mock,
        error_message=exp.error_message,
        download_url=f"/api/v1/audio-exports/{exp.id}/download",
        created_at=exp.created_at,
    )


@router.post("/auth/media-token", response_model=MediaTokenOut)
def mint_media_token(
    body: MediaTokenRequestIn,
    settings: Settings = Depends(get_settings),
    principal: Principal = Depends(get_current_principal),
):
    """P1-08: Issue a short-lived path-scoped HMAC token for <audio> playback or downloads."""
    token, exp = create_signed_media_token(principal, body.path, settings)
    return MediaTokenOut(media_token=token, expires_at=exp)


@router.post("/auth/session")
def establish_session_cookie(
    response: Response,
    settings: Settings = Depends(get_settings),
    principal: Principal = Depends(get_current_principal),
):
    """P1-08: Establish SameSite=Lax HttpOnly session cookie for media endpoints."""
    role_token_map = {
        UserRole.ADMIN: settings.admin_api_token,
        UserRole.REVIEWER: settings.reviewer_api_token or settings.admin_api_token,
        UserRole.READER: settings.reader_api_token or settings.admin_api_token,
    }
    raw_token = role_token_map.get(principal.role) or ""
    if raw_token:
        response.set_cookie(
            key="ncke_session_token",
            value=raw_token,
            httponly=True,
            samesite="lax",
            secure=(settings.app_env == "production"),
            max_age=3600 * 8,
            path="/",
        )
    return {"status": "ok", "user_id": principal.user_id, "role": principal.role.value}


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
            _dispatch_render_job(background_tasks, job.id)
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


@router.post("/render-jobs/{job_id}/pause", response_model=RenderJobOut)
def pause_render_job(
    job_id: str,
    db: Session = Depends(get_db),
    _principal: Principal = Depends(require_roles(UserRole.ADMIN, UserRole.REVIEWER)),
):
    return audio_service.pause_render_job(db, job_id)


@router.post("/render-jobs/{job_id}/resume", response_model=RenderJobOut)
def resume_render_job(
    job_id: str,
    background_tasks: BackgroundTasks,
    run_inline: bool = False,
    db: Session = Depends(get_db),
    storage: LocalStorage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
    _principal: Principal = Depends(require_roles(UserRole.ADMIN, UserRole.REVIEWER)),
):
    job = audio_service.resume_render_job(db, job_id)
    if run_inline:
        job = audio_service.execute_render_job(db, storage, settings, job.id)
    else:
        _dispatch_render_job(background_tasks, job.id)
    return job


@router.post("/render-jobs/{job_id}/cancel", response_model=RenderJobOut)
def cancel_render_job(
    job_id: str,
    db: Session = Depends(get_db),
    _principal: Principal = Depends(require_roles(UserRole.ADMIN, UserRole.REVIEWER)),
):
    return audio_service.cancel_render_job(db, job_id)


@router.post("/render-jobs/{job_id}/retry-failed", response_model=RenderJobOut)
def retry_failed_render_job(
    job_id: str,
    background_tasks: BackgroundTasks,
    run_inline: bool = False,
    db: Session = Depends(get_db),
    storage: LocalStorage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
    _principal: Principal = Depends(require_roles(UserRole.ADMIN, UserRole.REVIEWER)),
):
    job = audio_service.retry_failed_render_job(db, job_id)
    if run_inline:
        job = audio_service.execute_render_job(db, storage, settings, job.id, only_failed=True)
    else:
        _dispatch_render_job(background_tasks, job.id, only_failed=True)
    return job


@router.get("/render-jobs/{job_id}/events")
async def stream_render_job_events(
    job_id: str,
    _principal: Principal = Depends(require_roles(UserRole.ADMIN, UserRole.REVIEWER, UserRole.READER)),
):
    """P1-01: SSE incremental status stream for a render job."""

    async def _gen():
        last_payload = ""
        for tick in range(120):
            db = SessionLocal()
            try:
                job = audio_service.get_render_job(db, job_id)
                data = {
                    "id": job.id,
                    "episode_id": job.episode_id,
                    "status": job.status,
                    "spent_cost": job.spent_cost,
                    "budget_limit": job.budget_limit,
                    "progress_json": job.progress_json,
                    "error_summary": job.error_summary,
                }
            finally:
                db.close()

            serialized = json.dumps(data, ensure_ascii=False)
            if serialized != last_payload:
                yield f"id: {tick}\ndata: {serialized}\n\n"
                last_payload = serialized
            if data["status"] in ("completed", "failed", "partial_failed", "cancelled", "paused"):
                break
            await asyncio.sleep(0.5)

    return StreamingResponse(_gen(), media_type="text/event-stream")


@router.post(
    "/episode-plans/{plan_id}/render",
    response_model=BookRenderJobOut,
    status_code=status.HTTP_201_CREATED,
)
def create_book_render_job(
    plan_id: str,
    body: BookRenderJobCreateIn,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    storage: LocalStorage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
    _principal: Principal = Depends(require_roles(UserRole.ADMIN, UserRole.REVIEWER)),
):
    book_job = audio_service.create_book_render_job(
        db,
        settings,
        plan_id=plan_id,
        episode_ids=body.episode_ids,
        budget_limit=body.budget_limit,
    )
    if body.run_inline:
        audio_service.execute_book_render_job(db, storage, settings, book_job.id)
    else:
        dispatched = False
        try:
            if get_redis_client() is not None:
                run_book_render_job_task.delay(book_job.id)
                dispatched = True
        except Exception as exc:
            logger.warning("Celery book render dispatch unavailable (%s); falling back to background", exc)
        if not dispatched:
            background_tasks.add_task(_run_book_render_bg, book_job.id)
    return audio_service.get_book_render_job(db, book_job.id)


@router.get("/book-render-jobs/{job_id}", response_model=BookRenderJobOut)
def get_book_render_job(
    job_id: str,
    db: Session = Depends(get_db),
    _principal: Principal = Depends(require_roles(UserRole.ADMIN, UserRole.REVIEWER, UserRole.READER)),
):
    return audio_service.get_book_render_job(db, job_id)


@router.get("/novels/{novel_id}/audio-readiness", response_model=NovelAudioReadinessOut)
def get_novel_audio_readiness(
    novel_id: str,
    plan_id: str | None = None,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    _principal: Principal = Depends(require_roles(UserRole.ADMIN, UserRole.REVIEWER, UserRole.READER)),
):
    return audio_service.get_novel_audio_readiness(db, settings, novel_id, plan_id=plan_id)


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
        allow_wav_fallback=body.allow_wav_fallback,
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
    settings: Settings = Depends(get_settings),
    _principal: Principal = Depends(require_roles(UserRole.ADMIN, UserRole.REVIEWER)),
):
    return audio_service.get_provider_usage_summary(db, novel_id, settings=settings)
