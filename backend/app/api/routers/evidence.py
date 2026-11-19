from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_storage
from app.api.schemas import EvidenceDetailOut
from app.application import evidence_service
from app.core.security import Principal, UserRole, require_roles
from app.infrastructure.db.session import get_db
from app.infrastructure.storage.local import LocalStorage

router = APIRouter(tags=["evidence"])


@router.get("/evidence/{evidence_id}", response_model=EvidenceDetailOut)
def get_evidence(
    evidence_id: str,
    context_chars: int = Query(default=160, ge=20, le=1000),
    db: Session = Depends(get_db),
    storage: LocalStorage = Depends(get_storage),
    _principal: Principal = Depends(require_roles(UserRole.ADMIN, UserRole.REVIEWER, UserRole.READER)),
):
    return evidence_service.get_evidence_detail(
        db, storage, evidence_id, context_chars=context_chars
    )
