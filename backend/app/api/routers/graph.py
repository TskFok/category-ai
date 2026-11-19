from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.schemas import EgoGraphOut, EvidenceSummaryOut
from app.application import graph_service
from app.core.security import Principal, UserRole, require_roles
from app.infrastructure.db.session import get_db

router = APIRouter(tags=["graph"])


@router.get("/graph/neighbors", response_model=EgoGraphOut)
def get_graph_neighbors(
    center_character_id: str = Query(...),
    depth: int = Query(default=1, ge=1, le=2),
    relation_types: str | None = Query(
        default=None, description="Comma-separated relation types to filter"
    ),
    chapter_from: int | None = Query(default=None, ge=1),
    chapter_to: int | None = Query(default=None, ge=1),
    review_status: str | None = Query(default=None),
    max_nodes: int = Query(default=50, ge=5, le=200),
    max_edges: int = Query(default=120, ge=5, le=500),
    db: Session = Depends(get_db),
    _principal: Principal = Depends(require_roles(UserRole.ADMIN, UserRole.REVIEWER, UserRole.READER)),
):
    rel_list = (
        [r.strip() for r in relation_types.split(",") if r.strip()] if relation_types else None
    )
    return graph_service.get_ego_graph(
        db,
        center_character_id=center_character_id,
        depth=depth,
        relation_types=rel_list,
        chapter_from=chapter_from,
        chapter_to=chapter_to,
        review_status=review_status,
        max_nodes=max_nodes,
        max_edges=max_edges,
    )


@router.get("/relationships/{relationship_id}/evidence", response_model=list[EvidenceSummaryOut])
def get_relationship_evidence(
    relationship_id: str,
    db: Session = Depends(get_db),
    _principal: Principal = Depends(require_roles(UserRole.ADMIN, UserRole.REVIEWER, UserRole.READER)),
):
    return graph_service.get_relationship_evidence(db, relationship_id)
