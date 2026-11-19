from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.schemas import (
    AliasOut,
    AnalysisReportOut,
    CharacterDetailOut,
    CharacterStateOut,
    CharacterSummaryOut,
    EventOut,
    FactOut,
    MentionOut,
    PaginatedResponse,
    RelationshipOut,
)
from app.application import character_service
from app.core.security import Principal, UserRole, require_roles
from app.infrastructure.db.session import get_db

router = APIRouter(tags=["characters"])


@router.get("/novels/{novel_id}/characters", response_model=PaginatedResponse[CharacterSummaryOut])
def list_characters(
    novel_id: str,
    q: str | None = Query(default=None),
    status: str | None = Query(default=None),
    entity_type: str | None = Query(default=None),
    include_superseded: bool = Query(default=False),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
    _principal: Principal = Depends(require_roles(UserRole.ADMIN, UserRole.REVIEWER, UserRole.READER)),
):
    items, total = character_service.list_characters(
        db,
        novel_id,
        q=q,
        status=status,
        entity_type=entity_type,
        include_superseded=include_superseded,
        page=page,
        page_size=page_size,
    )
    return PaginatedResponse(items=items, page=page, page_size=page_size, total=total)


@router.get("/characters/{character_id}", response_model=CharacterDetailOut)
def get_character(
    character_id: str,
    db: Session = Depends(get_db),
    _principal: Principal = Depends(require_roles(UserRole.ADMIN, UserRole.REVIEWER, UserRole.READER)),
):
    detail = character_service.get_character_detail(db, character_id)
    char = detail["character"]
    ev_map = detail["fact_evidence_map"]
    return CharacterDetailOut(
        id=char.id,
        novel_id=char.novel_id,
        canonical_name=char.canonical_name,
        entity_type=char.entity_type,
        status=char.status,
        first_appearance_chapter=char.first_appearance_chapter,
        mention_count=char.mention_count,
        summary=char.summary,
        merged_into_id=char.merged_into_id,
        revision=char.revision,
        aliases=[AliasOut.model_validate(a) for a in detail["aliases"]],
        states=[CharacterStateOut.model_validate(s) for s in detail["states"]],
        facts=[
            FactOut(
                id=f.id,
                predicate=f.predicate,
                value_json=f.value_json,
                chapter_no=f.chapter_no,
                certainty=f.certainty,
                review_status=f.review_status,
                evidence_ids=ev_map.get(f.id, []),
            )
            for f in detail["facts"]
        ],
    )


@router.get("/characters/{character_id}/mentions", response_model=PaginatedResponse[MentionOut])
def list_mentions(
    character_id: str,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=30, ge=1, le=100),
    db: Session = Depends(get_db),
    _principal: Principal = Depends(require_roles(UserRole.ADMIN, UserRole.REVIEWER, UserRole.READER)),
):
    rows, total = character_service.list_character_mentions(
        db, character_id, page=page, page_size=page_size
    )
    items = [
        MentionOut(
            id=m.id,
            chapter_id=m.chapter_id,
            chapter_no=ch_no,
            chapter_title=ch_title,
            surface_text=m.surface_text,
            start_offset=m.start_offset,
            end_offset=m.end_offset,
            context_snippet=m.context_snippet,
            resolution_status=m.resolution_status,
            confidence=m.confidence,
        )
        for m, ch_no, ch_title in rows
    ]
    return PaginatedResponse(items=items, page=page, page_size=page_size, total=total)


@router.get("/characters/{character_id}/events", response_model=list[EventOut])
def list_events(
    character_id: str,
    chapter_from: int | None = Query(default=None, ge=1),
    chapter_to: int | None = Query(default=None, ge=1),
    db: Session = Depends(get_db),
    _principal: Principal = Depends(require_roles(UserRole.ADMIN, UserRole.REVIEWER, UserRole.READER)),
):
    rows = character_service.list_character_events(
        db, character_id, chapter_from=chapter_from, chapter_to=chapter_to
    )
    return [
        EventOut(
            id=ev.id,
            chapter_id=ev.chapter_id,
            chapter_no=ev.chapter_no,
            title=ev.title,
            event_type=ev.event_type,
            description=ev.description,
            narrative_order=ev.narrative_order,
            story_time_text=ev.story_time_text,
            certainty=ev.certainty,
            review_status=ev.review_status,
            participant_role=role,
            evidence_ids=ev_ids,
        )
        for ev, role, ev_ids in rows
    ]


@router.get("/characters/{character_id}/relationships", response_model=list[RelationshipOut])
def list_relationships(
    character_id: str,
    db: Session = Depends(get_db),
    _principal: Principal = Depends(require_roles(UserRole.ADMIN, UserRole.REVIEWER, UserRole.READER)),
):
    return character_service.list_character_relationships(db, character_id)


@router.get("/characters/{character_id}/analyses", response_model=list[AnalysisReportOut])
def list_analyses(
    character_id: str,
    db: Session = Depends(get_db),
    _principal: Principal = Depends(require_roles(UserRole.ADMIN, UserRole.REVIEWER, UserRole.READER)),
):
    return character_service.list_character_analyses(db, character_id)
