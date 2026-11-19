from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.infrastructure.db.session import get_db
from app.infrastructure.redis.client import get_redis_client

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live")
def health_live() -> dict:
    return {"status": "ok"}


@router.get("/ready")
def health_ready(
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    checks = {"database": "ok", "redis": "ok"}
    http_status = status.HTTP_200_OK

    try:
        db.execute(text("SELECT 1"))
    except Exception as exc:
        checks["database"] = f"error: {exc.__class__.__name__}"
        http_status = status.HTTP_503_SERVICE_UNAVAILABLE

    redis_client = get_redis_client()
    if redis_client is None:
        checks["redis"] = "unavailable"

    llm_mode = (
        "real"
        if (not settings.deepseek_mock_mode and bool((settings.deepseek_api_key or "").strip()))
        else ("misconfigured" if settings.app_env == "production" else "mock")
    )
    tts_mode = (
        "real"
        if (not settings.tts_mock_mode and bool((settings.cosyvoice_api_key or "").strip()))
        else ("misconfigured" if settings.app_env == "production" else "mock")
    )
    checks["deepseek_mode"] = llm_mode
    checks["cosyvoice_mode"] = tts_mode

    return JSONResponse(
        status_code=http_status,
        content={
            "status": "ok" if http_status == 200 else "degraded",
            "app_env": settings.app_env,
            "checks": checks,
        },
    )
