from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.enums import (
    ClaimType,
    EntityType,
    JobState,
    RelationDirection,
    ReviewStatus,
    ReviewTaskState,
    TaskState,
    VerificationStatus,
)
from app.infrastructure.db.session import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )


class Novel(Base, TimestampMixin):
    __tablename__ = "novels"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    author: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    source_format: Mapped[str] = mapped_column(String(16), default="txt", nullable=False)
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    source_storage_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    encoding: Mapped[str] = mapped_column(String(32), default="utf-8", nullable=False)
    total_chars: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    chapter_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    data_revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="ready", nullable=False)
    diagnostics_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


class Chapter(Base, TimestampMixin):
    __tablename__ = "chapters"
    __table_args__ = (
        UniqueConstraint(
            "novel_id",
            "chapter_no",
            "source_version",
            name="uq_chapters_novel_no_ver",
        ),
        Index("ix_chapters_novel_no", "novel_id", "chapter_no"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    novel_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("novels.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    chapter_no: Mapped[int] = mapped_column(Integer, nullable=False)
    declared_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    char_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    text_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    content_ref: Mapped[str] = mapped_column(String(512), nullable=False)


class Chunk(Base, TimestampMixin):
    __tablename__ = "chunks"
    __table_args__ = (
        UniqueConstraint(
            "chapter_id",
            "start_offset",
            "end_offset",
            name="uq_chunks_chapter_span",
        ),
        Index("ix_chunks_novel_chapter", "novel_id", "chapter_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    novel_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("novels.id", ondelete="CASCADE"), nullable=False, index=True
    )
    chapter_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("chapters.id", ondelete="CASCADE"), nullable=False, index=True
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    start_offset: Mapped[int] = mapped_column(Integer, nullable=False)
    end_offset: Mapped[int] = mapped_column(Integer, nullable=False)
    text_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    processing_status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)


class Character(Base, TimestampMixin):
    __tablename__ = "characters"
    __table_args__ = (Index("ix_characters_novel_name", "novel_id", "canonical_name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    novel_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("novels.id", ondelete="CASCADE"), nullable=False, index=True
    )
    canonical_name: Mapped[str] = mapped_column(String(255), nullable=False)
    entity_type: Mapped[str] = mapped_column(
        String(32), default=EntityType.PERSON.value, nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(32), default=ReviewStatus.CANDIDATE.value, nullable=False
    )
    first_appearance_chapter: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mention_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    merged_into_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("characters.id"), nullable=True
    )
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    model_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class Alias(Base, TimestampMixin):
    __tablename__ = "aliases"
    __table_args__ = (Index("ix_aliases_novel_text", "novel_id", "alias_text"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    novel_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("novels.id", ondelete="CASCADE"), nullable=False, index=True
    )
    character_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("characters.id", ondelete="CASCADE"), nullable=False, index=True
    )
    alias_text: Mapped[str] = mapped_column(String(255), nullable=False)
    alias_type: Mapped[str] = mapped_column(String(32), default="alias", nullable=False)
    valid_from_chapter: Mapped[int | None] = mapped_column(Integer, nullable=True)
    valid_to_chapter: Mapped[int | None] = mapped_column(Integer, nullable=True)
    review_status: Mapped[str] = mapped_column(
        String(32), default=ReviewStatus.CANDIDATE.value, nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class Mention(Base, TimestampMixin):
    __tablename__ = "mentions"
    __table_args__ = (
        UniqueConstraint(
            "chapter_id",
            "start_offset",
            "end_offset",
            "surface_text",
            name="uq_mentions_chapter_span_surface",
        ),
        Index("ix_mentions_novel_char", "novel_id", "character_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    novel_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("novels.id", ondelete="CASCADE"), nullable=False, index=True
    )
    chapter_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("chapters.id", ondelete="CASCADE"), nullable=False, index=True
    )
    chunk_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("chunks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    surface_text: Mapped[str] = mapped_column(String(255), nullable=False)
    start_offset: Mapped[int] = mapped_column(Integer, nullable=False)
    end_offset: Mapped[int] = mapped_column(Integer, nullable=False)
    context_snippet: Mapped[str | None] = mapped_column(Text, nullable=True)
    character_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("characters.id", ondelete="SET NULL"), nullable=True, index=True
    )
    resolution_status: Mapped[str] = mapped_column(
        String(32), default=ReviewStatus.CANDIDATE.value, nullable=False
    )
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    model_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(32), nullable=True)


class Fact(Base, TimestampMixin):
    __tablename__ = "facts"
    __table_args__ = (Index("ix_facts_novel_subject", "novel_id", "subject_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    novel_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("novels.id", ondelete="CASCADE"), nullable=False, index=True
    )
    subject_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("characters.id", ondelete="CASCADE"), nullable=False, index=True
    )
    predicate: Mapped[str] = mapped_column(String(128), nullable=False)
    value_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    chapter_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    certainty: Mapped[str] = mapped_column(String(32), default="medium", nullable=False)
    review_status: Mapped[str] = mapped_column(
        String(32), default=ReviewStatus.CANDIDATE.value, nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    model_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(32), nullable=True)


class CharacterState(Base, TimestampMixin):
    __tablename__ = "character_states"
    __table_args__ = (Index("ix_states_novel_char", "novel_id", "character_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    novel_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("novels.id", ondelete="CASCADE"), nullable=False, index=True
    )
    character_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("characters.id", ondelete="CASCADE"), nullable=False, index=True
    )
    state_type: Mapped[str] = mapped_column(
        String(64), nullable=False
    )  # cultivation, faction, identity, location, status
    value_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    valid_from: Mapped[int | None] = mapped_column(Integer, nullable=True)
    valid_to: Mapped[int | None] = mapped_column(Integer, nullable=True)
    review_status: Mapped[str] = mapped_column(
        String(32), default=ReviewStatus.CANDIDATE.value, nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class Relationship(Base, TimestampMixin):
    __tablename__ = "relationships"
    __table_args__ = (
        Index("ix_rel_novel_src_tgt", "novel_id", "source_character_id", "target_character_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    novel_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("novels.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_character_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("characters.id", ondelete="CASCADE"), nullable=False, index=True
    )
    target_character_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("characters.id", ondelete="CASCADE"), nullable=False, index=True
    )
    relation_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    direction: Mapped[str] = mapped_column(
        String(16), default=RelationDirection.DIRECTED.value, nullable=False
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    start_chapter: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_chapter: Mapped[int | None] = mapped_column(Integer, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    review_status: Mapped[str] = mapped_column(
        String(32), default=ReviewStatus.CANDIDATE.value, nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    model_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(32), nullable=True)


class Event(Base, TimestampMixin):
    __tablename__ = "events"
    __table_args__ = (Index("ix_events_novel_order", "novel_id", "narrative_order"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    novel_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("novels.id", ondelete="CASCADE"), nullable=False, index=True
    )
    chapter_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("chapters.id", ondelete="SET NULL"), nullable=True
    )
    chapter_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), default="plot", nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    narrative_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    story_time_text: Mapped[str | None] = mapped_column(String(255), nullable=True)
    certainty: Mapped[str] = mapped_column(String(32), default="medium", nullable=False)
    review_status: Mapped[str] = mapped_column(
        String(32), default=ReviewStatus.CANDIDATE.value, nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class EventParticipant(Base, TimestampMixin):
    __tablename__ = "event_participants"
    __table_args__ = (
        UniqueConstraint("event_id", "character_id", "participant_role", name="uq_event_part"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    novel_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("novels.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("events.id", ondelete="CASCADE"), nullable=False, index=True
    )
    character_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("characters.id", ondelete="CASCADE"), nullable=False, index=True
    )
    participant_role: Mapped[str] = mapped_column(String(64), default="participant", nullable=False)


class Evidence(Base, TimestampMixin):
    __tablename__ = "evidence"
    __table_args__ = (Index("ix_evidence_novel_chapter", "novel_id", "chapter_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    novel_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("novels.id", ondelete="CASCADE"), nullable=False, index=True
    )
    chapter_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("chapters.id", ondelete="CASCADE"), nullable=False, index=True
    )
    start_offset: Mapped[int] = mapped_column(Integer, nullable=False)
    end_offset: Mapped[int] = mapped_column(Integer, nullable=False)
    quote_text: Mapped[str] = mapped_column(Text, nullable=False)
    text_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    verification_status: Mapped[str] = mapped_column(
        String(32), default=VerificationStatus.PENDING.value, nullable=False
    )
    verification_note: Mapped[str | None] = mapped_column(Text, nullable=True)


class EvidenceLink(Base, TimestampMixin):
    __tablename__ = "evidence_links"
    __table_args__ = (Index("ix_evlink_subject", "novel_id", "subject_kind", "subject_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    novel_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("novels.id", ondelete="CASCADE"), nullable=False, index=True
    )
    evidence_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("evidence.id", ondelete="CASCADE"), nullable=False, index=True
    )
    subject_kind: Mapped[str] = mapped_column(
        String(32), nullable=False
    )  # fact | relationship | event | claim | state | mention
    subject_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    support_type: Mapped[str] = mapped_column(String(32), default="supports", nullable=False)


class IdentityDecision(Base, TimestampMixin):
    __tablename__ = "identity_decisions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    novel_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("novels.id", ondelete="CASCADE"), nullable=False, index=True
    )
    candidate_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    target_character_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    decision: Mapped[str] = mapped_column(String(32), nullable=False)  # merge | split | alias_bind
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    reviewed_by: Mapped[str] = mapped_column(String(128), nullable=False)
    reversible_action_ref: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


class AnalysisReport(Base, TimestampMixin):
    __tablename__ = "analysis_reports"
    __table_args__ = (Index("ix_reports_novel_char", "novel_id", "character_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    novel_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("novels.id", ondelete="CASCADE"), nullable=False, index=True
    )
    character_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("characters.id", ondelete="CASCADE"), nullable=False, index=True
    )
    report_type: Mapped[str] = mapped_column(
        String(32), default="deep", nullable=False
    )  # stage_summary | brief | deep
    chapter_from: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chapter_to: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    generation_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    review_status: Mapped[str] = mapped_column(
        String(32), default=ReviewStatus.CANDIDATE.value, nullable=False
    )
    model_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(32), nullable=True)


class AnalysisClaim(Base, TimestampMixin):
    __tablename__ = "analysis_claims"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    novel_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("novels.id", ondelete="CASCADE"), nullable=False, index=True
    )
    report_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("analysis_reports.id", ondelete="CASCADE"), nullable=False, index=True
    )
    section: Mapped[str] = mapped_column(String(64), default="general", nullable=False)
    claim_text: Mapped[str] = mapped_column(Text, nullable=False)
    claim_type: Mapped[str] = mapped_column(
        String(16), default=ClaimType.INFERENCE.value, nullable=False
    )  # fact | inference | unknown
    certainty: Mapped[str] = mapped_column(String(32), default="medium", nullable=False)


class Job(Base, TimestampMixin):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    novel_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("novels.id", ondelete="CASCADE"), nullable=False, index=True
    )
    job_type: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(32), default=JobState.PENDING.value, nullable=False)
    requested_by: Mapped[str] = mapped_column(String(128), default="local-admin", nullable=False)
    budget_limit: Mapped[float] = mapped_column(Float, default=50.0, nullable=False)
    spent_cost: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    chapter_from: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chapter_to: Mapped[int | None] = mapped_column(Integer, nullable=True)
    progress_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)


class TaskRun(Base, TimestampMixin):
    __tablename__ = "task_runs"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_task_runs_idempotency"),
        Index("ix_task_runs_job_state", "job_id", "state"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    novel_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("novels.id", ondelete="CASCADE"), nullable=False, index=True
    )
    job_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    stage: Mapped[str] = mapped_column(String(64), nullable=False)
    chunk_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("chunks.id", ondelete="SET NULL"), nullable=True, index=True
    )
    character_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("characters.id", ondelete="SET NULL"), nullable=True
    )
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    state: Mapped[str] = mapped_column(String(32), default=TaskState.PENDING.value, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class LLMCall(Base, TimestampMixin):
    __tablename__ = "llm_calls"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    novel_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("novels.id", ondelete="CASCADE"), nullable=False, index=True
    )
    job_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    task_run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("task_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    stage: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(32), nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    estimated_cost: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="ok", nullable=False)
    error_type: Mapped[str | None] = mapped_column(String(64), nullable=True)


class ReviewTask(Base, TimestampMixin):
    __tablename__ = "review_tasks"
    __table_args__ = (Index("ix_review_tasks_novel_state", "novel_id", "state"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    novel_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("novels.id", ondelete="CASCADE"), nullable=False, index=True
    )
    review_type: Mapped[str] = mapped_column(
        String(64), nullable=False
    )  # identity_conflict | evidence_mismatch | relation_dispute | fact_conflict
    target_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[str] = mapped_column(String(36), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), default="medium", nullable=False)
    state: Mapped[str] = mapped_column(
        String(32), default=ReviewTaskState.OPEN.value, nullable=False
    )
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    payload_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(String(128), nullable=True)


class ExportArtifact(Base, TimestampMixin):
    __tablename__ = "export_artifacts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    novel_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("novels.id", ondelete="CASCADE"), nullable=False, index=True
    )
    format: Mapped[str] = mapped_column(String(16), nullable=False)  # json | csv | graphml
    status: Mapped[str] = mapped_column(String(32), default="ready", nullable=False)
    storage_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    data_revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
