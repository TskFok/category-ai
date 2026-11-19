from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.schemas import (
    CharacterSummaryOut,
    IdentityDecisionOut,
    IdentityMergeIn,
    IdentitySplitIn,
    PaginatedResponse,
    ReviewDecisionIn,
    ReviewTaskOut,
)
from app.application import review_service
from app.core.security import Principal, UserRole, require_roles
from app.infrastructure.db.session import get_db

router = APIRouter(tags=["reviews"])


@router.get("/reviews", response_model=PaginatedResponse[ReviewTaskOut])
def list_reviews(
    novel_id: str | None = Query(default=None),
    state: str | None = Query(default=None),
    review_type: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=30, ge=1, le=100),
    db: Session = Depends(get_db),
    _principal: Principal = Depends(require_roles(UserRole.ADMIN, UserRole.REVIEWER)),
):
    items, total = review_service.list_review_tasks(
        db,
        novel_id=novel_id,
        state=state,
        review_type=review_type,
        page=page,
        page_size=page_size,
    )
    return PaginatedResponse(items=items, page=page, page_size=page_size, total=total)


@router.post("/reviews/{review_id}/decisions", response_model=ReviewTaskOut)
def submit_decision(
    review_id: str,
    body: ReviewDecisionIn,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_roles(UserRole.ADMIN, UserRole.REVIEWER)),
):
    return review_service.submit_review_decision(
        db,
        review_id=review_id,
        decision=body.decision,
        note=body.note,
        reviewed_by=principal.user_id,
    )


@router.post("/identities/merge", response_model=IdentityDecisionOut)
def merge_identities(
    body: IdentityMergeIn,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_roles(UserRole.ADMIN, UserRole.REVIEWER)),
):
    return review_service.merge_characters(
        db,
        source_character_id=body.source_character_id,
        target_character_id=body.target_character_id,
        reason=body.reason,
        reviewed_by=principal.user_id,
    )


@router.post("/identities/split")
def split_identity(
    body: IdentitySplitIn,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_roles(UserRole.ADMIN, UserRole.REVIEWER)),
):
    new_char, decision = review_service.split_character(
        db,
        source_character_id=body.source_character_id,
        new_canonical_name=body.new_canonical_name,
        mention_ids=body.mention_ids,
        reason=body.reason,
        reviewed_by=principal.user_id,
    )
    return {
        "new_character": CharacterSummaryOut.model_validate(new_char),
        "audit": IdentityDecisionOut.model_validate(decision),
    }
