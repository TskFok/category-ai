import json
import logging
from typing import Any

import redis

from app.core.config import get_settings

logger = logging.getLogger(__name__)


def get_redis_client() -> redis.Redis | None:
    settings = get_settings()
    try:
        client = redis.Redis.from_url(settings.redis_url, decode_responses=True, socket_timeout=2)
        client.ping()
        return client
    except Exception:
        return None


def publish_job_event(job_id: str, event_id: str, payload: dict[str, Any]) -> None:
    client = get_redis_client()
    if client is None:
        return
    try:
        message = json.dumps({"event_id": event_id, "data": payload}, ensure_ascii=False)
        client.publish(f"ncke:job:{job_id}", message)
    except Exception as exc:
        logger.debug("Redis publish skipped for job %s: %s", job_id, exc)
