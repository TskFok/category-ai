from __future__ import annotations

from datetime import datetime
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class PaginatedResponse(BaseModel, Generic[T]):
    items: list[T]
    page: int
    page_size: int
    total: int


class ErrorResponse(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


# --- Novels & Chapters ---
class NovelOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str
    author: str | None
    source_filename: str
    source_format: str
    source_hash: str
    source_version: int
    encoding: str
    total_chars: int
    chapter_count: int
    chunk_count: int
    data_revision: int
    status: str
    diagnostics_json: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class ChapterSummaryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    novel_id: str
    chapter_no: int
    declared_no: int | None
    title: str
    char_count: int
    text_hash: str


class ChunkOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    chunk_index: int
    start_offset: int
    end_offset: int
    text_hash: str
    processing_status: str


class ChapterDetailOut(ChapterSummaryOut):
    content: str
    chunks: list[ChunkOut]


# --- Jobs & Usage ---
class JobCreateIn(BaseModel):
    job_type: str = Field(default="FULL_PIPELINE")
    budget_limit: float | None = Field(default=None, ge=0.0)
    chapter_from: int | None = Field(default=None, ge=1)
    chapter_to: int | None = Field(default=None, ge=1)
    run_inline: bool = Field(
        default=False,
        description="Execute inline/background without requiring external Celery worker (useful in dev/test)",
    )


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    novel_id: str
    job_type: str
    state: str
    requested_by: str
    budget_limit: float
    spent_cost: float
    chapter_from: int | None
    chapter_to: int | None
    progress_json: dict[str, Any]
    error_summary: str | None
    created_at: datetime
    updated_at: datetime


class StageUsageOut(BaseModel):
    stage: str
    model: str
    call_count: int
    input_tokens: int
    output_tokens: int
    estimated_cost: float


class UsageSummaryOut(BaseModel):
    novel_id: str
    total_input_tokens: int
    total_output_tokens: int
    total_estimated_cost: float
    by_stage: list[StageUsageOut]


# --- Characters, Aliases, States, Facts, Mentions, Events, Relationships ---
class CharacterSummaryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    novel_id: str
    canonical_name: str
    entity_type: str
    status: str
    first_appearance_chapter: int | None
    mention_count: int
    summary: str | None
    merged_into_id: str | None
    revision: int


class AliasOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    character_id: str
    alias_text: str
    alias_type: str
    valid_from_chapter: int | None
    valid_to_chapter: int | None
    review_status: str


class CharacterStateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    state_type: str
    value_json: dict[str, Any]
    valid_from: int | None
    valid_to: int | None
    review_status: str


class FactOut(BaseModel):
    id: str
    predicate: str
    value_json: dict[str, Any]
    chapter_no: int | None
    certainty: str
    review_status: str
    evidence_ids: list[str] = Field(default_factory=list)


class CharacterDetailOut(CharacterSummaryOut):
    aliases: list[AliasOut]
    states: list[CharacterStateOut]
    facts: list[FactOut]


class MentionOut(BaseModel):
    id: str
    chapter_id: str
    chapter_no: int
    chapter_title: str
    surface_text: str
    start_offset: int
    end_offset: int
    context_snippet: str | None
    resolution_status: str
    confidence: float | None


class EventOut(BaseModel):
    id: str
    chapter_id: str | None
    chapter_no: int | None
    title: str
    event_type: str
    description: str | None
    narrative_order: int
    story_time_text: str | None
    certainty: str
    review_status: str
    participant_role: str
    evidence_ids: list[str] = Field(default_factory=list)


class RelationshipOut(BaseModel):
    id: str
    source_character_id: str
    source_character_name: str
    target_character_id: str
    target_character_name: str
    relation_type: str
    direction: str
    description: str | None
    start_chapter: int | None
    end_chapter: int | None
    confidence: float | None
    review_status: str
    evidence_ids: list[str] = Field(default_factory=list)


# --- Graph ---
class GraphNodeOut(BaseModel):
    id: str
    label: str
    entity_type: str
    status: str
    mention_count: int
    is_center: bool


class GraphEdgeOut(BaseModel):
    id: str
    source: str
    target: str
    relation_type: str
    direction: str
    description: str | None
    start_chapter: int | None
    end_chapter: int | None
    review_status: str
    evidence_count: int


class EgoGraphOut(BaseModel):
    center_character_id: str
    depth: int
    truncated: bool
    nodes: list[GraphNodeOut]
    edges: list[GraphEdgeOut]


# --- Evidence ---
class EvidenceSummaryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    chapter_id: str
    start_offset: int
    end_offset: int
    quote_text: str
    text_hash: str
    verification_status: str
    verification_note: str | None


class EvidenceDetailOut(EvidenceSummaryOut):
    novel_id: str
    chapter_no: int
    chapter_title: str
    utf16_start_offset: int
    utf16_end_offset: int
    context_text: str
    context_utf16_start: int
    context_utf16_end: int


# --- Analysis Reports ---
class AnalysisClaimOut(BaseModel):
    id: str
    section: str
    claim_text: str
    claim_type: str
    certainty: str
    evidence_ids: list[str] = Field(default_factory=list)


class AnalysisReportOut(BaseModel):
    id: str
    character_id: str
    report_type: str
    chapter_from: int | None
    chapter_to: int | None
    content_json: dict[str, Any]
    generation_version: int
    review_status: str
    model_id: str | None
    prompt_version: str | None
    created_at: datetime
    claims: list[AnalysisClaimOut]


# --- Reviews & Identity Decisions ---
class ReviewTaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    novel_id: str
    review_type: str
    target_kind: str
    target_id: str
    severity: str
    state: str
    summary: str
    payload_json: dict[str, Any]
    decision_note: str | None
    reviewed_by: str | None
    created_at: datetime


class ReviewDecisionIn(BaseModel):
    decision: str = Field(..., pattern="^(approve|reject|resolve)$")
    note: str | None = None


class IdentityMergeIn(BaseModel):
    source_character_id: str
    target_character_id: str
    reason: str = Field(..., min_length=1)


class IdentitySplitIn(BaseModel):
    source_character_id: str
    new_canonical_name: str = Field(..., min_length=1)
    mention_ids: list[str] = Field(default_factory=list)
    reason: str = Field(..., min_length=1)


class IdentityDecisionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    novel_id: str
    candidate_id: str
    target_character_id: str
    decision: str
    reason: str
    reviewed_by: str
    reversible_action_ref: dict[str, Any]
    created_at: datetime


# --- Exports ---
class ExportCreateIn(BaseModel):
    format: str = Field(default="json", pattern="^(json|csv|graphml)$")


class ExportArtifactOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    novel_id: str
    format: str
    status: str
    data_revision: int
    file_size_bytes: int
    download_url: str
    created_at: datetime
