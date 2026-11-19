from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.domain.enums import ReviewStatus, ReviewTaskState
from app.infrastructure.db.models import (
    Alias,
    Character,
    CharacterState,
    EventParticipant,
    Fact,
    IdentityDecision,
    Mention,
    Novel,
    Relationship,
    ReviewTask,
)


def list_review_tasks(
    db: Session,
    *,
    novel_id: str | None = None,
    state: str | None = None,
    review_type: str | None = None,
    page: int = 1,
    page_size: int = 30,
) -> tuple[list[ReviewTask], int]:
    stmt = select(ReviewTask)
    count_stmt = select(func.count()).select_from(ReviewTask)

    if novel_id:
        stmt = stmt.where(ReviewTask.novel_id == novel_id)
        count_stmt = count_stmt.where(ReviewTask.novel_id == novel_id)
    if state:
        stmt = stmt.where(ReviewTask.state == state)
        count_stmt = count_stmt.where(ReviewTask.state == state)
    if review_type:
        stmt = stmt.where(ReviewTask.review_type == review_type)
        count_stmt = count_stmt.where(ReviewTask.review_type == review_type)

    total = db.scalar(count_stmt) or 0
    rows = (
        db.scalars(
            stmt.order_by(ReviewTask.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        .all()
    )
    return list(rows), total


def submit_review_decision(
    db: Session,
    *,
    review_id: str,
    decision: str,  # approve | reject | resolve
    note: str | None,
    reviewed_by: str,
) -> ReviewTask:
    task = db.get(ReviewTask, review_id)
    if not task:
        raise AppError(code="REVIEW_NOT_FOUND", message="Review task not found", status_code=404)

    if decision not in ("approve", "reject", "resolve"):
        raise AppError(code="INVALID_DECISION", message="Decision must be approve, reject, or resolve")

    new_status = ReviewStatus.CONFIRMED.value if decision == "approve" else (
        ReviewStatus.REJECTED.value if decision == "reject" else ReviewStatus.CONFIRMED.value
    )
    task.state = (
        ReviewTaskState.APPROVED.value
        if decision == "approve"
        else ReviewTaskState.REJECTED.value
        if decision == "reject"
        else ReviewTaskState.RESOLVED.value
    )
    task.decision_note = note
    task.reviewed_by = reviewed_by

    # Apply target status update based on target_kind
    if task.target_kind == "character":
        obj = db.get(Character, task.target_id)
        if obj:
            obj.status = new_status
            obj.revision += 1
    elif task.target_kind == "relationship":
        obj = db.get(Relationship, task.target_id)
        if obj:
            obj.review_status = new_status
            obj.revision += 1
    elif task.target_kind == "fact":
        obj = db.get(Fact, task.target_id)
        if obj:
            obj.review_status = new_status
            obj.revision += 1
    elif task.target_kind == "alias":
        obj = db.get(Alias, task.target_id)
        if obj:
            obj.review_status = new_status
            obj.revision += 1

    novel = db.get(Novel, task.novel_id)
    if novel:
        novel.data_revision += 1

    db.commit()
    db.refresh(task)
    return task


def merge_characters(
    db: Session,
    *,
    source_character_id: str,
    target_character_id: str,
    reason: str,
    reviewed_by: str,
) -> IdentityDecision:
    """
    Reversible character merge (Section 6.3 & 7.4):
    - Never deletes the source character; marks it `superseded` with `merged_into_id`.
    - Moves mentions, facts, states, aliases, relationships, and event_participants to target.
    - Records previous IDs in `reversible_action_ref` so the merge can be audited or undone.
    """
    if source_character_id == target_character_id:
        raise AppError(code="INVALID_MERGE", message="Cannot merge a character into itself")

    src = db.get(Character, source_character_id)
    tgt = db.get(Character, target_character_id)
    if not src or not tgt:
        raise AppError(code="CHARACTER_NOT_FOUND", message="Source or target character not found", status_code=404)
    if src.novel_id != tgt.novel_id:
        raise AppError(code="NOVEL_MISMATCH", message="Cannot merge characters across different novels")

    moved_mentions = list(
        db.scalars(select(Mention).where(Mention.character_id == src.id)).all()
    )
    moved_mention_ids = [m.id for m in moved_mentions]
    for m in moved_mentions:
        m.character_id = tgt.id
        m.resolution_status = ReviewStatus.CONFIRMED.value

    moved_facts = list(db.scalars(select(Fact).where(Fact.subject_id == src.id)).all())
    moved_fact_ids = [f.id for f in moved_facts]
    for f in moved_facts:
        f.subject_id = tgt.id

    moved_states = list(
        db.scalars(select(CharacterState).where(CharacterState.character_id == src.id)).all()
    )
    moved_state_ids = [s.id for s in moved_states]
    for s in moved_states:
        s.character_id = tgt.id

    # Preserve source canonical name as an alias on target
    existing_alias = db.scalar(
        select(Alias).where(
            Alias.character_id == tgt.id, Alias.alias_text == src.canonical_name
        )
    )
    if not existing_alias and src.canonical_name != tgt.canonical_name:
        db.add(
            Alias(
                novel_id=tgt.novel_id,
                character_id=tgt.id,
                alias_text=src.canonical_name,
                alias_type="merged_name",
                review_status=ReviewStatus.CONFIRMED.value,
            )
        )

    # Re-point relationships
    src_rels = list(
        db.scalars(select(Relationship).where(Relationship.source_character_id == src.id)).all()
    )
    tgt_rels = list(
        db.scalars(select(Relationship).where(Relationship.target_character_id == src.id)).all()
    )
    for r in src_rels:
        if r.target_character_id != tgt.id:
            r.source_character_id = tgt.id
    for r in tgt_rels:
        if r.source_character_id != tgt.id:
            r.target_character_id = tgt.id

    # Re-point event participants (avoiding unique collisions)
    eps = list(
        db.scalars(select(EventParticipant).where(EventParticipant.character_id == src.id)).all()
    )
    for ep in eps:
        dup = db.scalar(
            select(EventParticipant).where(
                EventParticipant.event_id == ep.event_id,
                EventParticipant.character_id == tgt.id,
                EventParticipant.participant_role == ep.participant_role,
            )
        )
        if not dup:
            ep.character_id = tgt.id

    # Mark source as superseded (never hard-delete)
    src.status = ReviewStatus.SUPERSEDED.value
    src.merged_into_id = tgt.id
    src.revision += 1

    tgt.mention_count = (tgt.mention_count or 0) + (src.mention_count or 0)
    if src.first_appearance_chapter is not None and (
        tgt.first_appearance_chapter is None
        or src.first_appearance_chapter < tgt.first_appearance_chapter
    ):
        tgt.first_appearance_chapter = src.first_appearance_chapter
    tgt.revision += 1

    decision = IdentityDecision(
        novel_id=tgt.novel_id,
        candidate_id=src.id,
        target_character_id=tgt.id,
        decision="merge",
        reason=reason,
        reviewed_by=reviewed_by,
        reversible_action_ref={
            "source_character_id": src.id,
            "target_character_id": tgt.id,
            "moved_mention_ids": moved_mention_ids,
            "moved_fact_ids": moved_fact_ids,
            "moved_state_ids": moved_state_ids,
        },
    )
    db.add(decision)

    novel = db.get(Novel, tgt.novel_id)
    if novel:
        novel.data_revision += 1

    db.commit()
    db.refresh(decision)
    return decision


def split_character(
    db: Session,
    *,
    source_character_id: str,
    new_canonical_name: str,
    mention_ids: list[str],
    reason: str,
    reviewed_by: str,
) -> tuple[Character, IdentityDecision]:
    """
    Split selected mentions from an existing character into a new character entity,
    recording a full reversible audit entry.
    """
    src = db.get(Character, source_character_id)
    if not src:
        raise AppError(code="CHARACTER_NOT_FOUND", message="Character not found", status_code=404)

    new_char = Character(
        novel_id=src.novel_id,
        canonical_name=new_canonical_name.strip(),
        entity_type=src.entity_type,
        status=ReviewStatus.CONFIRMED.value,
        mention_count=0,
    )
    db.add(new_char)
    db.flush()

    moved_ids: list[str] = []
    if mention_ids:
        mentions = list(
            db.scalars(
                select(Mention).where(
                    Mention.id.in_(mention_ids),
                    Mention.character_id == src.id,
                )
            ).all()
        )
        for m in mentions:
            m.character_id = new_char.id
            moved_ids.append(m.id)
        new_char.mention_count = len(moved_ids)
        src.mention_count = max(0, (src.mention_count or 0) - len(moved_ids))
        src.revision += 1

    decision = IdentityDecision(
        novel_id=src.novel_id,
        candidate_id=src.id,
        target_character_id=new_char.id,
        decision="split",
        reason=reason,
        reviewed_by=reviewed_by,
        reversible_action_ref={
            "source_character_id": src.id,
            "new_character_id": new_char.id,
            "moved_mention_ids": moved_ids,
        },
    )
    db.add(decision)

    novel = db.get(Novel, src.novel_id)
    if novel:
        novel.data_revision += 1

    db.commit()
    db.refresh(new_char)
    db.refresh(decision)
    return new_char, decision
