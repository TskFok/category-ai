from __future__ import annotations

from app.application import episode_service, script_service
from app.core.config import get_settings
from app.infrastructure.db.session import SessionLocal
from app.infrastructure.storage.local import LocalStorage
from worker.celery_app import celery_app


@celery_app.task(name="worker.tasks.episode_tasks.analyze_episode_script_task")
def analyze_episode_script_task(episode_id: str) -> dict:
    db = SessionLocal()
    try:
        settings = get_settings()
        storage = LocalStorage(settings.storage_path)
        segs = script_service.analyze_episode_script(db, storage, settings, episode_id=episode_id)
        return {"episode_id": episode_id, "segment_count": len(segs)}
    finally:
        db.close()


@celery_app.task(name="worker.tasks.episode_tasks.plan_episodes_task")
def plan_episodes_task(novel_id: str, target_minutes: int = 25) -> dict:
    db = SessionLocal()
    try:
        settings = get_settings()
        storage = LocalStorage(settings.storage_path)
        plan, eps = episode_service.create_episode_plan(
            db, storage, settings, novel_id=novel_id, target_minutes=target_minutes
        )
        return {"plan_id": plan.id, "episode_count": len(eps)}
    finally:
        db.close()
