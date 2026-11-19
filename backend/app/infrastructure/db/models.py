from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
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
    AnalysisStatus,
    AudioSegmentStatus,
    ClaimType,
    EntityType,
    EpisodePlanStatus,
    EpisodeStatus,
    JobState,
    RelationDirection,
    ReviewStatus,
    ReviewTaskState,
    SegmentReviewStatus,
    SegmentType,
    SynthesisUnitStatus,
    TaskState,
    VerificationStatus,
    VoiceApprovalStatus,
    VoiceProviderStatus,
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
    license_scope: Mapped[str] = mapped_column(String(32), default="private", nullable=False)
    copyright_confirmed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
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
    analysis_status: Mapped[str] = mapped_column(
        String(32), default=AnalysisStatus.PENDING.value, nullable=False
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


# ==============================================================================
# Audiobook Extension Models (M1: Episodes & Script, M2: Voice, M3: Audio/Export)
# ==============================================================================


class EpisodePlan(Base, TimestampMixin):
    __tablename__ = "episode_plans"
    __table_args__ = (
        UniqueConstraint("novel_id", "plan_version", name="uq_episode_plans_novel_ver"),
        Index("ix_episode_plans_novel", "novel_id", "plan_version"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    novel_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("novels.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    plan_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    target_minutes: Mapped[int] = mapped_column(Integer, default=25, nullable=False)
    chars_per_minute: Mapped[int] = mapped_column(Integer, default=260, nullable=False)
    strategy: Mapped[str] = mapped_column(String(32), default="chapter_first", nullable=False)
    max_deviation_pct: Mapped[int] = mapped_column(Integer, default=25, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), default=EpisodePlanStatus.DRAFT.value, nullable=False
    )
    validation_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


class Episode(Base, TimestampMixin):
    __tablename__ = "episodes"
    __table_args__ = (
        UniqueConstraint("plan_id", "episode_no", name="uq_episodes_plan_no"),
        Index("ix_episodes_plan_no", "plan_id", "episode_no"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    plan_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("episode_plans.id", ondelete="CASCADE"), nullable=False, index=True
    )
    novel_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("novels.id", ondelete="CASCADE"), nullable=False, index=True
    )
    episode_no: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    start_chapter_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("chapters.id", ondelete="CASCADE"), nullable=False
    )
    start_offset: Mapped[int] = mapped_column(Integer, nullable=False)
    end_chapter_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("chapters.id", ondelete="CASCADE"), nullable=False
    )
    end_offset: Mapped[int] = mapped_column(Integer, nullable=False)
    char_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    paragraph_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    estimated_seconds: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    text_hash: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), default=EpisodeStatus.DRAFT.value, nullable=False
    )


class ScriptSegment(Base, TimestampMixin):
    __tablename__ = "script_segments"
    __table_args__ = (
        UniqueConstraint("episode_id", "order_no", name="uq_script_segments_ep_order"),
        Index("ix_script_segments_ep_order", "episode_id", "order_no"),
        Index("ix_script_segments_novel_speaker", "novel_id", "speaker_character_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    novel_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("novels.id", ondelete="CASCADE"), nullable=False, index=True
    )
    episode_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("episodes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    chapter_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("chapters.id", ondelete="CASCADE"), nullable=False, index=True
    )
    order_no: Mapped[int] = mapped_column(Integer, nullable=False)
    start_offset: Mapped[int] = mapped_column(Integer, nullable=False)
    end_offset: Mapped[int] = mapped_column(Integer, nullable=False)
    text_content: Mapped[str] = mapped_column(Text, nullable=False)
    text_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    segment_type: Mapped[str] = mapped_column(
        String(32), default=SegmentType.NARRATION.value, nullable=False
    )
    speaker_character_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("characters.id", ondelete="SET NULL"), nullable=True, index=True
    )
    narrator_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    inference_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    emotion: Mapped[str] = mapped_column(String(32), default="neutral", nullable=False)
    emotion_intensity: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    speech_rate: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    review_status: Mapped[str] = mapped_column(
        String(32), default=SegmentReviewStatus.AUTO_ACCEPTED.value, nullable=False
    )
    manual_override: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    stable_span_key: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    content_revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    analysis_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    is_render_blocked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class SegmentCandidate(Base, TimestampMixin):
    __tablename__ = "segment_candidates"
    __table_args__ = (Index("ix_segment_candidates_seg", "segment_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    segment_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("script_segments.id", ondelete="CASCADE"), nullable=False
    )
    character_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("characters.id", ondelete="SET NULL"), nullable=True
    )
    candidate_name: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    model_version: Mapped[str] = mapped_column(String(64), default="rule", nullable=False)


class VoiceProfile(Base, TimestampMixin):
    __tablename__ = "voice_profiles"
    __table_args__ = (
        Index("ix_voice_profiles_novel_char", "novel_id", "character_id"),
        Index("ix_voice_profiles_novel_narrator", "novel_id", "narrator_key"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    novel_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("novels.id", ondelete="CASCADE"), nullable=False, index=True
    )
    character_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("characters.id", ondelete="CASCADE"), nullable=True, index=True
    )
    narrator_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), default="cosyvoice", nullable=False)
    target_model: Mapped[str] = mapped_column(String(64), default="cosyvoice-v3.5-flash", nullable=False)
    voice_prompt: Mapped[str] = mapped_column(Text, default="", nullable=False)
    voice_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    sample_audio_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    language: Mapped[str] = mapped_column(String(32), default="zh-CN", nullable=False)
    default_rate: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    pitch: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    seed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    profile_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    approval_status: Mapped[str] = mapped_column(
        String(32), default=VoiceApprovalStatus.DRAFT.value, nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    candidates_json: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    consent_confirmed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    consent_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider_status: Mapped[str] = mapped_column(
        String(32), default=VoiceProviderStatus.OK.value, nullable=False
    )
    preview_checksum: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prompt_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class VoiceProfileVersion(Base, TimestampMixin):
    __tablename__ = "voice_profile_versions"
    __table_args__ = (
        UniqueConstraint("voice_profile_id", "version", name="uq_voice_profile_ver"),
        Index("ix_voice_profile_ver_profile", "voice_profile_id", "version"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    voice_profile_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("voice_profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    novel_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("novels.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    voice_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    target_model: Mapped[str] = mapped_column(String(64), default="cosyvoice-v3.5-flash", nullable=False)
    voice_prompt: Mapped[str] = mapped_column(Text, default="", nullable=False)
    default_rate: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    pitch: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    provider_status: Mapped[str] = mapped_column(
        String(32), default=VoiceProviderStatus.OK.value, nullable=False
    )
    preview_checksum: Mapped[str | None] = mapped_column(String(64), nullable=True)
    approval_status: Mapped[str] = mapped_column(
        String(32), default=VoiceApprovalStatus.DRAFT.value, nullable=False
    )


class VoicePerformance(Base, TimestampMixin):
    __tablename__ = "voice_performances"
    __table_args__ = (Index("ix_voice_perf_segment", "segment_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    segment_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("script_segments.id", ondelete="CASCADE"), nullable=False
    )
    emotion: Mapped[str] = mapped_column(String(32), default="neutral", nullable=False)
    intensity: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    instruction: Mapped[str | None] = mapped_column(Text, nullable=True)
    rate: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    pitch: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class BookRenderJob(Base, TimestampMixin):
    __tablename__ = "book_render_jobs"
    __table_args__ = (Index("ix_book_render_jobs_novel", "novel_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    novel_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("novels.id", ondelete="CASCADE"), nullable=False, index=True
    )
    plan_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("episode_plans.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(32), default=JobState.PENDING.value, nullable=False)
    episode_ids_json: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    budget_limit: Mapped[float] = mapped_column(Float, default=100.0, nullable=False)
    spent_cost: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), default="CNY", nullable=False)
    progress_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)


class RenderJob(Base, TimestampMixin):
    __tablename__ = "render_jobs"
    __table_args__ = (
        Index("ix_render_jobs_episode", "episode_id", "created_at"),
        UniqueConstraint("episode_id", "idempotency_key", name="uq_render_jobs_ep_idempotency"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    novel_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("novels.id", ondelete="CASCADE"), nullable=False, index=True
    )
    episode_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("episodes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    book_render_job_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("book_render_jobs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(32), default=JobState.PENDING.value, nullable=False)
    budget_limit: Mapped[float] = mapped_column(Float, default=20.0, nullable=False)
    spent_cost: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    source_revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    profile_snapshot_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    progress_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resume_cursor: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)


class SynthesisUnit(Base, TimestampMixin):
    __tablename__ = "synthesis_units"
    __table_args__ = (
        UniqueConstraint("script_segment_id", "unit_index", name="uq_synthesis_units_seg_idx"),
        Index("ix_synthesis_units_seg", "script_segment_id", "unit_index"),
        Index("ix_synthesis_units_ep_status", "episode_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    novel_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("novels.id", ondelete="CASCADE"), nullable=False, index=True
    )
    episode_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("episodes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    script_segment_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("script_segments.id", ondelete="CASCADE"), nullable=False
    )
    unit_index: Mapped[int] = mapped_column(Integer, nullable=False)
    start_offset: Mapped[int] = mapped_column(Integer, nullable=False)
    end_offset: Mapped[int] = mapped_column(Integer, nullable=False)
    text_content: Mapped[str] = mapped_column(Text, nullable=False)
    text_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), default=SynthesisUnitStatus.PENDING.value, nullable=False
    )
    attempt: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    audio_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    audio_format: Mapped[str] = mapped_column(String(16), default="wav", nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    cache_key: Mapped[str] = mapped_column(String(128), default="", nullable=False)


class AudioSegment(Base, TimestampMixin):
    __tablename__ = "audio_segments"
    __table_args__ = (
        Index("ix_audio_segments_script_seg", "script_segment_id"),
        Index("ix_audio_segments_cache_key", "cache_key"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    novel_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("novels.id", ondelete="CASCADE"), nullable=False, index=True
    )
    episode_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("episodes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    script_segment_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("script_segments.id", ondelete="CASCADE"), nullable=False
    )
    render_job_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("render_jobs.id", ondelete="SET NULL"), nullable=True
    )
    voice_profile_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("voice_profiles.id", ondelete="SET NULL"), nullable=True
    )
    voice_profile_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    script_revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    render_input_hash: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    voice_id: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    audio_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    audio_format: Mapped[str] = mapped_column(String(16), default="wav", nullable=False)
    sample_rate: Mapped[int] = mapped_column(Integer, default=24000, nullable=False)
    channels: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    cache_key: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_mock: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    provider: Mapped[str] = mapped_column(String(64), default="cosyvoice", nullable=False)
    model: Mapped[str] = mapped_column(String(64), default="cosyvoice-v3.5-flash", nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), default=AudioSegmentStatus.PENDING.value, nullable=False
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class AudioExport(Base, TimestampMixin):
    __tablename__ = "audio_exports"
    __table_args__ = (Index("ix_audio_exports_novel", "novel_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    novel_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("novels.id", ondelete="CASCADE"), nullable=False, index=True
    )
    episode_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("episodes.id", ondelete="SET NULL"), nullable=True
    )
    plan_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("episode_plans.id", ondelete="SET NULL"), nullable=True
    )
    format: Mapped[str] = mapped_column(String(16), default="mp3", nullable=False)  # mp3 | wav | zip
    actual_format: Mapped[str] = mapped_column(String(16), default="", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    manifest_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    file_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    file_size_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    source_snapshot_hash: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    is_stale: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_mock: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class ProviderCall(Base, TimestampMixin):
    __tablename__ = "provider_calls"
    __table_args__ = (Index("ix_provider_calls_novel", "novel_id", "provider"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    novel_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("novels.id", ondelete="CASCADE"), nullable=False, index=True
    )
    render_job_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("render_jobs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    operation: Mapped[str] = mapped_column(String(64), default="synthesize", nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    input_chars: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    billed_characters: Mapped[int | None] = mapped_column(Integer, nullable=True)
    audio_duration_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    estimated_cost: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    actual_cost: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), default="CNY", nullable=False)
    cost_kind: Mapped[str] = mapped_column(String(16), default="estimated", nullable=False)
    provider_request_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    is_mock: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="ok", nullable=False)
    error_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
