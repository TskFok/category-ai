from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_storage
from app.api.schemas import (
    ScriptSegmentOut,
    ScriptSegmentUpdateIn,
    SpeakerReviewBatchIn,
)
from app.application import script_service
from app.core.config import Settings, get_settings
from app.core.security import Principal, UserRole, require_roles
from app.infrastructure.db.session import get_db
from app.infrastructure.storage.local import LocalStorage

router = APIRouter(tags=["scripts"])


@router.post("/episodes/{episode_id}/analyze-script", response_model=list[ScriptSegmentOut])
def analyze_episode_script(
    episode_id: str,
    db: Session = Depends(get_db),
    storage: LocalStorage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
    _principal: Principal = Depends(require_roles(UserRole.ADMIN, UserRole.REVIEWER)),
):
    script_service.analyze_episode_script(db, storage, settings, episode_id=episode_id)
    return script_service.list_episode_segments(db, episode_id)


@router.get("/episodes/{episode_id}/segments", response_model=list[ScriptSegmentOut])
def list_episode_segments(
    episode_id: str,
    review_status: str | None = Query(default=None),
    segment_type: str | None = Query(default=None),
    db: Session = Depends(get_db),
    _principal: Principal = Depends(require_roles(UserRole.ADMIN, UserRole.REVIEWER, UserRole.READER)),
):
    return script_service.list_episode_segments(
        db, episode_id, review_status=review_status, segment_type=segment_type
    )


@router.patch("/script-segments/{segment_id}", response_model=ScriptSegmentOut)
def update_script_segment(
    segment_id: str,
    body: ScriptSegmentUpdateIn,
    db: Session = Depends(get_db),
    _principal: Principal = Depends(require_roles(UserRole.ADMIN, UserRole.REVIEWER)),
):
    seg = script_service.update_script_segment(
        db,
        segment_id,
        segment_type=body.segment_type,
        speaker_character_id=body.speaker_character_id,
        set_narrator=body.set_narrator,
        narrator_key=body.narrator_key,
        emotion=body.emotion,
        emotion_intensity=body.emotion_intensity,
        speech_rate=body.speech_rate,
        instruction=body.instruction,
        review_status=body.review_status,
    )
    items = script_service.list_episode_segments(db, seg.episode_id)
    for item in items:
        if item["id"] == seg.id:
            return item
    return items[0]


@router.post("/episodes/{episode_id}/speaker-review")
def submit_speaker_review(
    episode_id: str,
    body: SpeakerReviewBatchIn,
    db: Session = Depends(get_db),
    _principal: Principal = Depends(require_roles(UserRole.ADMIN, UserRole.REVIEWER)),
):
    updated = script_service.submit_speaker_review(
        db, episode_id, [d.model_dump() for d in body.decisions]
    )
    return {"episode_id": episode_id, "updated_count": updated}
