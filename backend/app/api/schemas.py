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
    license_scope: str = "private"
    copyright_confirmed: bool = False
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
    character_ids: list[str] | None = Field(
        default=None,
        description="Optional character IDs for targeted incremental S5_ANALYZE reruns",
    )
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
    analysis_status: str = "pending"
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


# --- M1: Episodes & Plans ---
class EpisodePlanCreateIn(BaseModel):
    target_minutes: int = Field(default=25, ge=5, le=120)
    chars_per_minute: int = Field(default=260, ge=100, le=600)
    strategy: str = Field(default="chapter_first", pattern="^(chapter_first|story_first)$")
    max_deviation_pct: int = Field(default=25, ge=5, le=100)
    use_llm_titles: bool = True


class EpisodeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    plan_id: str
    novel_id: str
    episode_no: int
    title: str
    rationale: str | None
    start_chapter_id: str
    start_offset: int
    end_chapter_id: str
    end_offset: int
    char_count: int
    paragraph_count: int
    estimated_seconds: int
    text_hash: str
    status: str
    created_at: datetime
    updated_at: datetime


class EpisodePlanOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    novel_id: str
    source_version: int
    plan_version: int
    target_minutes: int
    chars_per_minute: int
    strategy: str
    max_deviation_pct: int
    status: str
    validation_json: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class EpisodePlanWithEpisodesOut(EpisodePlanOut):
    episodes: list[EpisodeOut] = Field(default_factory=list)


class EpisodeUpdateIn(BaseModel):
    title: str | None = None
    rationale: str | None = None
    start_chapter_id: str | None = None
    start_offset: int | None = Field(default=None, ge=0)
    end_chapter_id: str | None = None
    end_offset: int | None = Field(default=None, ge=1)
    status: str | None = None


class CoverageValidationOut(BaseModel):
    plan_id: str
    valid: bool
    coverage_ratio: float
    total_chars: int
    covered_chars: int
    errors: list[str]


# --- M1: Script Segments & Speaker Review ---
class SegmentCandidateOut(BaseModel):
    id: str
    character_id: str | None
    candidate_name: str
    score: float
    reason: str | None


class CurrentAudioMiniOut(BaseModel):
    id: str
    status: str
    voice_id: str
    duration_ms: int
    audio_format: str
    checksum: str
    stream_url: str | None


class ScriptSegmentOut(BaseModel):
    id: str
    novel_id: str
    episode_id: str
    chapter_id: str
    order_no: int
    start_offset: int
    end_offset: int
    text_content: str
    text_hash: str
    segment_type: str
    speaker_character_id: str | None
    speaker_name: str | None = None
    narrator_key: str | None
    inference_reason: str | None
    confidence: float
    emotion: str
    emotion_intensity: float
    speech_rate: float
    review_status: str
    manual_override: bool
    revision: int
    candidates: list[SegmentCandidateOut] = Field(default_factory=list)
    current_audio: CurrentAudioMiniOut | None = None


class ScriptSegmentUpdateIn(BaseModel):
    segment_type: str | None = None
    speaker_character_id: str | None = None
    set_narrator: bool = False
    narrator_key: str | None = None
    emotion: str | None = None
    emotion_intensity: float | None = Field(default=None, ge=0.0, le=1.0)
    speech_rate: float | None = Field(default=None, ge=0.5, le=2.0)
    instruction: str | None = None
    review_status: str | None = None


class SpeakerReviewDecisionIn(BaseModel):
    segment_id: str
    action: str = Field(default="confirm", pattern="^(confirm|narrator|skip)$")
    speaker_character_id: str | None = None
    narrator_key: str | None = None
    emotion: str | None = None


class SpeakerReviewBatchIn(BaseModel):
    decisions: list[SpeakerReviewDecisionIn]


# --- M2: Voice Profiles ---
class VoiceProfileCreateIn(BaseModel):
    character_id: str | None = None
    narrator_key: str | None = None
    display_name: str | None = None
    voice_prompt: str = ""
    voice_id: str | None = None
    language: str = "zh-CN"
    default_rate: float = Field(default=1.0, ge=0.5, le=2.0)
    pitch: float = Field(default=1.0, ge=0.5, le=2.0)
    seed: int | None = None
    consent_confirmed: bool = False
    consent_note: str | None = None


class VoiceProfileUpdateIn(BaseModel):
    display_name: str | None = None
    voice_prompt: str | None = None
    voice_id: str | None = None
    default_rate: float | None = Field(default=None, ge=0.5, le=2.0)
    pitch: float | None = Field(default=None, ge=0.5, le=2.0)
    consent_confirmed: bool | None = None
    consent_note: str | None = None


class VoiceDesignIn(BaseModel):
    preview_text: str | None = None


class VoiceLockIn(BaseModel):
    candidate_index: int | None = None
    voice_id: str | None = None
    voice_prompt: str | None = None
    scope: str = Field(default="unrendered_only", pattern="^(unrendered_only|current_episode|all_episodes)$")


class VoiceProfileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    novel_id: str
    character_id: str | None
    narrator_key: str | None
    display_name: str
    provider: str
    target_model: str
    voice_prompt: str
    voice_id: str | None
    sample_audio_ref: str | None
    language: str
    default_rate: float
    pitch: float
    seed: int | None
    profile_version: int
    approval_status: str
    is_active: bool
    locked_at: datetime | None
    candidates_json: list[dict[str, Any]]
    consent_confirmed: bool
    consent_note: str | None
    created_at: datetime
    updated_at: datetime


class VoiceLockResultOut(BaseModel):
    profile: VoiceProfileOut
    impacted_segments: int
    impacted_chars: int
    estimated_rerender_cost: float


# --- M3: Render Jobs, Audio Segments & Audio Exports ---
class RenderJobCreateIn(BaseModel):
    budget_limit: float | None = Field(default=None, ge=0.0)
    idempotency_key: str | None = None
    run_inline: bool = True


class RenderJobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    novel_id: str
    episode_id: str
    status: str
    budget_limit: float
    spent_cost: float
    source_revision: int
    profile_snapshot_json: dict[str, Any]
    progress_json: dict[str, Any]
    idempotency_key: str | None
    error_summary: str | None
    created_at: datetime
    updated_at: datetime


class AudioSegmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    novel_id: str
    episode_id: str
    script_segment_id: str
    render_job_id: str | None
    voice_profile_id: str | None
    voice_profile_version: int
    voice_id: str
    audio_format: str
    duration_ms: int
    file_size_bytes: int
    checksum: str
    cache_key: str
    is_current: bool
    status: str
    error_message: str | None
    created_at: datetime


class AudioExportCreateIn(BaseModel):
    episode_id: str | None = None
    plan_id: str | None = None
    format: str = Field(default="mp3", pattern="^(mp3|wav|zip)$")


class AudioExportOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    novel_id: str
    episode_id: str | None
    format: str
    status: str
    file_size_bytes: int
    duration_ms: int
    error_message: str | None
    download_url: str
    created_at: datetime


class ProviderOperationUsageOut(BaseModel):
    provider: str
    model: str
    operation: str
    call_count: int
    input_chars: int
    audio_duration_ms: int
    actual_cost: float


class ProviderUsageSummaryOut(BaseModel):
    novel_id: str
    total_input_chars: int
    total_audio_duration_ms: int
    total_actual_cost: float
    by_operation: list[ProviderOperationUsageOut]
