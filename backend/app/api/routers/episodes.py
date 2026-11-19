from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.api.deps import get_storage
from app.api.schemas import (
    CoverageValidationOut,
    EpisodeOut,
    EpisodePlanCreateIn,
    EpisodePlanOut,
    EpisodePlanWithEpisodesOut,
    EpisodeUpdateIn,
)
from app.application import episode_service
from app.core.config import Settings, get_settings
from app.core.security import Principal, UserRole, require_roles
from app.infrastructure.db.session import get_db
from app.infrastructure.storage.local import LocalStorage

router = APIRouter(tags=["episodes"])


@router.post(
    "/novels/{novel_id}/episode-plans",
    response_model=EpisodePlanWithEpisodesOut,
    status_code=status.HTTP_201_CREATED,
)
def create_episode_plan(
    novel_id: str,
    body: EpisodePlanCreateIn,
    db: Session = Depends(get_db),
    storage: LocalStorage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
    _principal: Principal = Depends(require_roles(UserRole.ADMIN)),
):
    plan, episodes = episode_service.create_episode_plan(
        db,
        storage,
        settings,
        novel_id=novel_id,
        target_minutes=body.target_minutes,
        chars_per_minute=body.chars_per_minute,
        strategy=body.strategy,
        max_deviation_pct=body.max_deviation_pct,
        use_llm_titles=body.use_llm_titles,
    )
    out = EpisodePlanWithEpisodesOut.model_validate(plan)
    out.episodes = [EpisodeOut.model_validate(e) for e in episodes]
    return out


@router.get("/novels/{novel_id}/episode-plans", response_model=list[EpisodePlanOut])
def list_episode_plans(
    novel_id: str,
    db: Session = Depends(get_db),
    _principal: Principal = Depends(require_roles(UserRole.ADMIN, UserRole.REVIEWER, UserRole.READER)),
):
    return episode_service.list_episode_plans(db, novel_id)


@router.get("/episode-plans/{plan_id}/episodes", response_model=list[EpisodeOut])
def list_plan_episodes(
    plan_id: str,
    db: Session = Depends(get_db),
    _principal: Principal = Depends(require_roles(UserRole.ADMIN, UserRole.REVIEWER, UserRole.READER)),
):
    return episode_service.list_episodes(db, plan_id)


@router.patch("/episodes/{episode_id}", response_model=EpisodeOut)
def update_episode(
    episode_id: str,
    body: EpisodeUpdateIn,
    db: Session = Depends(get_db),
    storage: LocalStorage = Depends(get_storage),
    _principal: Principal = Depends(require_roles(UserRole.ADMIN, UserRole.REVIEWER)),
):
    return episode_service.update_episode(
        db,
        storage,
        episode_id,
        title=body.title,
        rationale=body.rationale,
        start_chapter_id=body.start_chapter_id,
        start_offset=body.start_offset,
        end_chapter_id=body.end_chapter_id,
        end_offset=body.end_offset,
        status=body.status,
    )


@router.post("/episode-plans/{plan_id}/validate", response_model=CoverageValidationOut)
def validate_plan(
    plan_id: str,
    db: Session = Depends(get_db),
    storage: LocalStorage = Depends(get_storage),
    _principal: Principal = Depends(require_roles(UserRole.ADMIN, UserRole.REVIEWER)),
):
    plan, res = episode_service.validate_episode_plan(db, storage, plan_id)
    return CoverageValidationOut(
        plan_id=plan.id,
        valid=res.valid,
        coverage_ratio=res.coverage_ratio,
        total_chars=res.total_chars,
        covered_chars=res.covered_chars,
        errors=res.errors,
    )
