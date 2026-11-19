from enum import StrEnum


class ReviewStatus(StrEnum):
    CANDIDATE = "candidate"
    CONFIRMED = "confirmed"
    PROBABLE = "probable"
    UNRESOLVED = "unresolved"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class AnalysisStatus(StrEnum):
    PENDING = "pending"
    ANALYZED = "analyzed"
    NEEDS_REVIEW = "needs_review"
    FAILED = "failed"


class EntityType(StrEnum):
    PERSON = "person"
    AVATAR = "avatar"
    UNNAMED = "unnamed"
    GROUP = "group"


class VerificationStatus(StrEnum):
    PENDING = "pending"
    VERIFIED = "verified"
    OUT_OF_RANGE = "out_of_range"
    MISMATCH = "mismatch"
    AMBIGUOUS = "ambiguous"


class ClaimType(StrEnum):
    FACT = "fact"
    INFERENCE = "inference"
    UNKNOWN = "unknown"


class JobState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    PARTIAL_FAILED = "partial_failed"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class PipelineStage(StrEnum):
    S0_INGEST = "S0_INGEST"
    S1_DISCOVER = "S1_DISCOVER"
    S2_EXTRACT = "S2_EXTRACT"
    S3_DISAMBIGUATE = "S3_DISAMBIGUATE"
    S4_CONSOLIDATE = "S4_CONSOLIDATE"
    S5_ANALYZE = "S5_ANALYZE"
    S6_PUBLISH = "S6_PUBLISH"
    FULL_PIPELINE = "FULL_PIPELINE"


class ReviewTaskState(StrEnum):
    OPEN = "open"
    APPROVED = "approved"
    REJECTED = "rejected"
    RESOLVED = "resolved"


class RelationDirection(StrEnum):
    DIRECTED = "directed"
    UNDIRECTED = "undirected"


class EpisodePlanStatus(StrEnum):
    DRAFT = "draft"
    VALIDATED = "validated"
    LOCKED = "locked"
    SUPERSEDED = "superseded"


class EpisodeStatus(StrEnum):
    DRAFT = "draft"
    CONFIRMED = "confirmed"
    SCRIPT_READY = "script_ready"
    AUDIO_READY = "audio_ready"


class SegmentType(StrEnum):
    NARRATION = "narration"
    DIALOGUE = "dialogue"
    INNER_MONOLOGUE = "inner_monologue"
    SCENE_HEADING = "scene_heading"


class SegmentReviewStatus(StrEnum):
    AUTO_ACCEPTED = "auto_accepted"
    NEEDS_REVIEW = "needs_review"
    CONFIRMED = "confirmed"
    SKIP = "skip"


class VoiceApprovalStatus(StrEnum):
    DRAFT = "draft"
    PREVIEWED = "previewed"
    LOCKED = "locked"
    ARCHIVED = "archived"


class AudioSegmentStatus(StrEnum):
    PENDING = "pending"
    SYNTHESIZING = "synthesizing"
    READY = "ready"
    STALE = "stale"
    FAILED = "failed"


class AudioExportFormat(StrEnum):
    MP3 = "mp3"
    WAV = "wav"
    ZIP = "zip"


SYMMETRIC_RELATION_TYPES: frozenset[str] = frozenset(
    {
        "ally",
        "enemy",
        "rival",
        "spouse",
        "sibling",
        "peer",
        "friend",
        "acquaintance",
    }
)
