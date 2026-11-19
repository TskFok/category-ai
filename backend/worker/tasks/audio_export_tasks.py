from __future__ import annotations

from app.application import audio_export_service
from app.core.config import get_settings
from app.infrastructure.db.session import SessionLocal
from app.infrastructure.storage.local import LocalStorage
from worker.celery_app import celery_app


@celery_app.task(name="worker.tasks.audio_export_tasks.export_audio_task")
def export_audio_task(novel_id: str, episode_id: str | None = None, fmt: str = "mp3") -> dict:
    db = SessionLocal()
    try:
        settings = get_settings()
        storage = LocalStorage(settings.storage_path)
        exp = audio_export_service.create_audio_export(
            db, storage, settings, novel_id=novel_id, episode_id=episode_id, fmt=fmt
        )
        return {"export_id": exp.id, "status": exp.status, "format": exp.format}
    finally:
        db.close()
