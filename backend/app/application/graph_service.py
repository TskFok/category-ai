from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.infrastructure.db.models import Character, Evidence, EvidenceLink, Relationship


def get_ego_graph(
    db: Session,
    *,
    center_character_id: str,
    depth: int = 1,
    relation_types: list[str] | None = None,
    chapter_from: int | None = None,
    chapter_to: int | None = None,
    review_status: str | None = None,
    max_nodes: int = 50,
    max_edges: int = 120,
) -> dict:
    """
    Local ego-network graph projection (Section 4.3).
    Never fetches the entire character graph at once; enforces depth (1 or 2),
    max_nodes, and max_edges with explicit truncation flag.
    """
    center = db.get(Character, center_character_id)
    if not center:
        raise AppError(code="CHARACTER_NOT_FOUND", message="Center character not found", status_code=404)

    depth = max(1, min(depth, 2))
    visited_nodes: set[str] = {center.id}
    frontier: set[str] = {center.id}
    collected_edges: dict[str, Relationship] = {}
    truncated = False

    for _ in range(depth):
        if not frontier:
            break
        stmt = select(Relationship).where(
            Relationship.novel_id == center.novel_id,
            or_(
                Relationship.source_character_id.in_(frontier),
                Relationship.target_character_id.in_(frontier),
            ),
        )
        if relation_types:
            stmt = stmt.where(Relationship.relation_type.in_(relation_types))
        if review_status:
            stmt = stmt.where(Relationship.review_status == review_status)
        if chapter_from is not None:
            stmt = stmt.where(
                or_(Relationship.end_chapter.is_(None), Relationship.end_chapter >= chapter_from)
            )
        if chapter_to is not None:
            stmt = stmt.where(
                or_(Relationship.start_chapter.is_(None), Relationship.start_chapter <= chapter_to)
            )

        rels = list(db.scalars(stmt).all())
        next_frontier: set[str] = set()
        for r in rels:
            if len(collected_edges) >= max_edges:
                truncated = True
                break
            other_ids = {r.source_character_id, r.target_character_id}
            new_nodes = other_ids - visited_nodes
            if len(visited_nodes) + len(new_nodes) > max_nodes:
                truncated = True
                continue
            visited_nodes.update(new_nodes)
            next_frontier.update(new_nodes)
            collected_edges[r.id] = r
        frontier = next_frontier

    chars = list(db.scalars(select(Character).where(Character.id.in_(visited_nodes))).all())
    edge_ids = list(collected_edges.keys())
    ev_counts: dict[str, int] = {}
    if edge_ids:
        links = db.scalars(
            select(EvidenceLink).where(
                EvidenceLink.subject_kind == "relationship",
                EvidenceLink.subject_id.in_(edge_ids),
            )
        ).all()
        for lk in links:
            ev_counts[lk.subject_id] = ev_counts.get(lk.subject_id, 0) + 1

    return {
        "center_character_id": center.id,
        "depth": depth,
        "truncated": truncated,
        "nodes": [
            {
                "id": c.id,
                "label": c.canonical_name,
                "entity_type": c.entity_type,
                "status": c.status,
                "mention_count": c.mention_count,
                "is_center": c.id == center.id,
            }
            for c in chars
        ],
        "edges": [
            {
                "id": r.id,
                "source": r.source_character_id,
                "target": r.target_character_id,
                "relation_type": r.relation_type,
                "direction": r.direction,
                "description": r.description,
                "start_chapter": r.start_chapter,
                "end_chapter": r.end_chapter,
                "review_status": r.review_status,
                "evidence_count": ev_counts.get(r.id, 0),
            }
            for r in collected_edges.values()
        ],
    }


def get_relationship_evidence(db: Session, relationship_id: str) -> list[Evidence]:
    rel = db.get(Relationship, relationship_id)
    if not rel:
        raise AppError(code="RELATIONSHIP_NOT_FOUND", message="Relationship not found", status_code=404)

    ev_ids = list(
        db.scalars(
            select(EvidenceLink.evidence_id).where(
                EvidenceLink.subject_kind == "relationship",
                EvidenceLink.subject_id == relationship_id,
            )
        ).all()
    )
    if not ev_ids:
        return []
    return list(db.scalars(select(Evidence).where(Evidence.id.in_(ev_ids))).all())
