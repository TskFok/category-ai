from __future__ import annotations

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.domain.enums import ReviewStatus
from app.infrastructure.db.models import (
    Alias,
    AnalysisClaim,
    AnalysisReport,
    Chapter,
    Character,
    CharacterState,
    Event,
    EventParticipant,
    EvidenceLink,
    Fact,
    Mention,
    Relationship,
)


def list_characters(
    db: Session,
    novel_id: str,
    *,
    q: str | None = None,
    status: str | None = None,
    entity_type: str | None = None,
    include_superseded: bool = False,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[Character], int]:
    stmt = select(Character).where(Character.novel_id == novel_id)
    count_stmt = select(func.count()).select_from(Character).where(Character.novel_id == novel_id)

    if not include_superseded:
        stmt = stmt.where(Character.status != ReviewStatus.SUPERSEDED.value)
        count_stmt = count_stmt.where(Character.status != ReviewStatus.SUPERSEDED.value)

    if status:
        stmt = stmt.where(Character.status == status)
        count_stmt = count_stmt.where(Character.status == status)

    if entity_type:
        stmt = stmt.where(Character.entity_type == entity_type)
        count_stmt = count_stmt.where(Character.entity_type == entity_type)

    if q and q.strip():
        like = f"%{q.strip()}%"
        alias_char_ids = select(Alias.character_id).where(
            Alias.novel_id == novel_id, Alias.alias_text.ilike(like)
        )
        cond = or_(Character.canonical_name.ilike(like), Character.id.in_(alias_char_ids))
        stmt = stmt.where(cond)
        count_stmt = count_stmt.where(cond)

    total = db.scalar(count_stmt) or 0
    rows = (
        db.scalars(
            stmt.order_by(Character.mention_count.desc(), Character.canonical_name.asc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        .all()
    )
    return list(rows), total


def get_character_detail(db: Session, character_id: str) -> dict:
    char = db.get(Character, character_id)
    if not char:
        raise AppError(code="CHARACTER_NOT_FOUND", message="Character not found", status_code=404)

    aliases = list(
        db.scalars(
            select(Alias).where(Alias.character_id == char.id).order_by(Alias.alias_text.asc())
        ).all()
    )
    states = list(
        db.scalars(
            select(CharacterState)
            .where(CharacterState.character_id == char.id)
            .order_by(CharacterState.valid_from.asc().nulls_last(), CharacterState.created_at.asc())
        ).all()
    )
    facts = list(
        db.scalars(
            select(Fact)
            .where(Fact.subject_id == char.id)
            .order_by(Fact.chapter_no.asc().nulls_last(), Fact.created_at.asc())
        ).all()
    )

    fact_ids = [f.id for f in facts]
    ev_map: dict[str, list[str]] = {}
    if fact_ids:
        links = db.scalars(
            select(EvidenceLink).where(
                EvidenceLink.subject_kind == "fact", EvidenceLink.subject_id.in_(fact_ids)
            )
        ).all()
        for lk in links:
            ev_map.setdefault(lk.subject_id, []).append(lk.evidence_id)

    return {
        "character": char,
        "aliases": aliases,
        "states": states,
        "facts": facts,
        "fact_evidence_map": ev_map,
    }


def list_character_mentions(
    db: Session,
    character_id: str,
    *,
    page: int = 1,
    page_size: int = 30,
) -> tuple[list[tuple[Mention, int, str]], int]:
    char = db.get(Character, character_id)
    if not char:
        raise AppError(code="CHARACTER_NOT_FOUND", message="Character not found", status_code=404)

    total = (
        db.scalar(
            select(func.count()).select_from(Mention).where(Mention.character_id == character_id)
        )
        or 0
    )
    rows = db.execute(
        select(Mention, Chapter.chapter_no, Chapter.title)
        .join(Chapter, Mention.chapter_id == Chapter.id)
        .where(Mention.character_id == character_id)
        .order_by(Chapter.chapter_no.asc(), Mention.start_offset.asc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return [(r[0], r[1], r[2]) for r in rows], total


def list_character_events(
    db: Session,
    character_id: str,
    *,
    chapter_from: int | None = None,
    chapter_to: int | None = None,
) -> list[tuple[Event, str, list[str]]]:
    char = db.get(Character, character_id)
    if not char:
        raise AppError(code="CHARACTER_NOT_FOUND", message="Character not found", status_code=404)

    stmt = (
        select(Event, EventParticipant.participant_role)
        .join(EventParticipant, EventParticipant.event_id == Event.id)
        .where(EventParticipant.character_id == character_id)
        .order_by(Event.narrative_order.asc(), Event.chapter_no.asc().nulls_last())
    )
    if chapter_from is not None:
        stmt = stmt.where(Event.chapter_no >= chapter_from)
    if chapter_to is not None:
        stmt = stmt.where(Event.chapter_no <= chapter_to)

    rows = db.execute(stmt).all()
    event_ids = [r[0].id for r in rows]
    ev_map: dict[str, list[str]] = {}
    if event_ids:
        links = db.scalars(
            select(EvidenceLink).where(
                EvidenceLink.subject_kind == "event", EvidenceLink.subject_id.in_(event_ids)
            )
        ).all()
        for lk in links:
            ev_map.setdefault(lk.subject_id, []).append(lk.evidence_id)

    return [(ev, role, ev_map.get(ev.id, [])) for ev, role in rows]


def list_character_relationships(db: Session, character_id: str) -> list[dict]:
    char = db.get(Character, character_id)
    if not char:
        raise AppError(code="CHARACTER_NOT_FOUND", message="Character not found", status_code=404)

    rels = list(
        db.scalars(
            select(Relationship).where(
                or_(
                    Relationship.source_character_id == character_id,
                    Relationship.target_character_id == character_id,
                )
            )
        ).all()
    )
    if not rels:
        return []

    other_ids = {
        r.target_character_id if r.source_character_id == character_id else r.source_character_id
        for r in rels
    }
    chars = {
        c.id: c
        for c in db.scalars(select(Character).where(Character.id.in_(other_ids))).all()
    }
    rel_ids = [r.id for r in rels]
    ev_links = db.scalars(
        select(EvidenceLink).where(
            EvidenceLink.subject_kind == "relationship", EvidenceLink.subject_id.in_(rel_ids)
        )
    ).all()
    ev_map: dict[str, list[str]] = {}
    for lk in ev_links:
        ev_map.setdefault(lk.subject_id, []).append(lk.evidence_id)

    results = []
    for r in rels:
        src_name = char.canonical_name if r.source_character_id == character_id else chars.get(r.source_character_id, char).canonical_name
        tgt_name = char.canonical_name if r.target_character_id == character_id else chars.get(r.target_character_id, char).canonical_name
        results.append(
            {
                "id": r.id,
                "source_character_id": r.source_character_id,
                "source_character_name": src_name,
                "target_character_id": r.target_character_id,
                "target_character_name": tgt_name,
                "relation_type": r.relation_type,
                "direction": r.direction,
                "description": r.description,
                "start_chapter": r.start_chapter,
                "end_chapter": r.end_chapter,
                "confidence": r.confidence,
                "review_status": r.review_status,
                "evidence_ids": ev_map.get(r.id, []),
            }
        )
    return results


def list_character_analyses(db: Session, character_id: str) -> list[dict]:
    char = db.get(Character, character_id)
    if not char:
        raise AppError(code="CHARACTER_NOT_FOUND", message="Character not found", status_code=404)

    reports = list(
        db.scalars(
            select(AnalysisReport)
            .where(AnalysisReport.character_id == character_id)
            .order_by(AnalysisReport.generation_version.desc(), AnalysisReport.created_at.desc())
        ).all()
    )
    out = []
    for rep in reports:
        claims = list(
            db.scalars(
                select(AnalysisClaim).where(AnalysisClaim.report_id == rep.id)
            ).all()
        )
        claim_ids = [c.id for c in claims]
        ev_map: dict[str, list[str]] = {}
        if claim_ids:
            links = db.scalars(
                select(EvidenceLink).where(
                    EvidenceLink.subject_kind == "claim", EvidenceLink.subject_id.in_(claim_ids)
                )
            ).all()
            for lk in links:
                ev_map.setdefault(lk.subject_id, []).append(lk.evidence_id)

        out.append(
            {
                "id": rep.id,
                "character_id": rep.character_id,
                "report_type": rep.report_type,
                "chapter_from": rep.chapter_from,
                "chapter_to": rep.chapter_to,
                "content_json": rep.content_json,
                "generation_version": rep.generation_version,
                "review_status": rep.review_status,
                "model_id": rep.model_id,
                "prompt_version": rep.prompt_version,
                "created_at": rep.created_at,
                "claims": [
                    {
                        "id": c.id,
                        "section": c.section,
                        "claim_text": c.claim_text,
                        "claim_type": c.claim_type,
                        "certainty": c.certainty,
                        "evidence_ids": ev_map.get(c.id, []),
                    }
                    for c in claims
                ],
            }
        )
    return out
