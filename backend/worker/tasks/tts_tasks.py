from __future__ import annotations

from app.application import audio_service
from app.core.config import get_settings
from app.infrastructure.db.session import SessionLocal
from app.infrastructure.storage.local import LocalStorage
from worker.celery_app import celery_app


@celery_app.task(name="worker.tasks.tts_tasks.run_render_job_task")
def run_render_job_task(render_job_id: str) -> dict:
    db = SessionLocal()
    try:
        settings = get_settings()
        storage = LocalStorage(settings.storage_path)
        job = audio_service.execute_render_job(db, storage, settings, render_job_id)
        return {"render_job_id": job.id, "status": job.status, "progress": job.progress_json}
    finally:
        db.close()
