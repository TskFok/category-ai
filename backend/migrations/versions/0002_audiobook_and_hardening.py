"""Incremental migration: M0 hardening columns + M1/M2/M3 audiobook tables.

Revision ID: 0002_audiobook_and_hardening
Revises: 0001_initial_schema
Create Date: 2026-11-19
"""

from alembic import op
import sqlalchemy as sa

revision = "0002_audiobook_and_hardening"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def _table_exists(inspector: sa.Inspector, table_name: str) -> bool:
    return table_name in inspector.get_table_names()


def _column_exists(inspector: sa.Inspector, table_name: str, column_name: str) -> bool:
    return any(col["name"] == column_name for col in inspector.get_columns(table_name))


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # M0 hardening columns on existing tables
    if _table_exists(inspector, "novels"):
        if not _column_exists(inspector, "novels", "license_scope"):
            op.add_column(
                "novels",
                sa.Column("license_scope", sa.String(32), nullable=False, server_default="private"),
            )
        if not _column_exists(inspector, "novels", "copyright_confirmed"):
            op.add_column(
                "novels",
                sa.Column("copyright_confirmed", sa.Boolean(), nullable=False, server_default=sa.false()),
            )

    if _table_exists(inspector, "characters"):
        if not _column_exists(inspector, "characters", "analysis_status"):
            op.add_column(
                "characters",
                sa.Column("analysis_status", sa.String(32), nullable=False, server_default="pending"),
            )

    # M1: episode_plans
    if not _table_exists(inspector, "episode_plans"):
        op.create_table(
            "episode_plans",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("novel_id", sa.String(36), sa.ForeignKey("novels.id", ondelete="CASCADE"), nullable=False),
            sa.Column("source_version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("plan_version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("target_minutes", sa.Integer(), nullable=False, server_default="25"),
            sa.Column("chars_per_minute", sa.Integer(), nullable=False, server_default="260"),
            sa.Column("strategy", sa.String(32), nullable=False, server_default="chapter_first"),
            sa.Column("max_deviation_pct", sa.Integer(), nullable=False, server_default="25"),
            sa.Column("status", sa.String(32), nullable=False, server_default="draft"),
            sa.Column("validation_json", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("novel_id", "plan_version", name="uq_episode_plans_novel_ver"),
        )
        op.create_index("ix_episode_plans_novel", "episode_plans", ["novel_id", "plan_version"])

    # M1: episodes
    if not _table_exists(inspector, "episodes"):
        op.create_table(
            "episodes",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("plan_id", sa.String(36), sa.ForeignKey("episode_plans.id", ondelete="CASCADE"), nullable=False),
            sa.Column("novel_id", sa.String(36), sa.ForeignKey("novels.id", ondelete="CASCADE"), nullable=False),
            sa.Column("episode_no", sa.Integer(), nullable=False),
            sa.Column("title", sa.String(255), nullable=False),
            sa.Column("rationale", sa.Text(), nullable=True),
            sa.Column("start_chapter_id", sa.String(36), sa.ForeignKey("chapters.id", ondelete="CASCADE"), nullable=False),
            sa.Column("start_offset", sa.Integer(), nullable=False),
            sa.Column("end_chapter_id", sa.String(36), sa.ForeignKey("chapters.id", ondelete="CASCADE"), nullable=False),
            sa.Column("end_offset", sa.Integer(), nullable=False),
            sa.Column("char_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("paragraph_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("estimated_seconds", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("text_hash", sa.String(64), nullable=False, server_default=""),
            sa.Column("status", sa.String(32), nullable=False, server_default="draft"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("plan_id", "episode_no", name="uq_episodes_plan_no"),
        )
        op.create_index("ix_episodes_plan_no", "episodes", ["plan_id", "episode_no"])

    # M1: script_segments
    if not _table_exists(inspector, "script_segments"):
        op.create_table(
            "script_segments",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("novel_id", sa.String(36), sa.ForeignKey("novels.id", ondelete="CASCADE"), nullable=False),
            sa.Column("episode_id", sa.String(36), sa.ForeignKey("episodes.id", ondelete="CASCADE"), nullable=False),
            sa.Column("chapter_id", sa.String(36), sa.ForeignKey("chapters.id", ondelete="CASCADE"), nullable=False),
            sa.Column("order_no", sa.Integer(), nullable=False),
            sa.Column("start_offset", sa.Integer(), nullable=False),
            sa.Column("end_offset", sa.Integer(), nullable=False),
            sa.Column("text_content", sa.Text(), nullable=False),
            sa.Column("text_hash", sa.String(64), nullable=False),
            sa.Column("segment_type", sa.String(32), nullable=False, server_default="narration"),
            sa.Column("speaker_character_id", sa.String(36), sa.ForeignKey("characters.id", ondelete="SET NULL"), nullable=True),
            sa.Column("narrator_key", sa.String(64), nullable=True),
            sa.Column("inference_reason", sa.Text(), nullable=True),
            sa.Column("confidence", sa.Float(), nullable=False, server_default="1.0"),
            sa.Column("emotion", sa.String(32), nullable=False, server_default="neutral"),
            sa.Column("emotion_intensity", sa.Float(), nullable=False, server_default="0.5"),
            sa.Column("speech_rate", sa.Float(), nullable=False, server_default="1.0"),
            sa.Column("review_status", sa.String(32), nullable=False, server_default="auto_accepted"),
            sa.Column("manual_override", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("episode_id", "order_no", name="uq_script_segments_ep_order"),
        )
        op.create_index("ix_script_segments_ep_order", "script_segments", ["episode_id", "order_no"])
        op.create_index("ix_script_segments_novel_speaker", "script_segments", ["novel_id", "speaker_character_id"])

    # M1: segment_candidates
    if not _table_exists(inspector, "segment_candidates"):
        op.create_table(
            "segment_candidates",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("segment_id", sa.String(36), sa.ForeignKey("script_segments.id", ondelete="CASCADE"), nullable=False),
            sa.Column("character_id", sa.String(36), sa.ForeignKey("characters.id", ondelete="SET NULL"), nullable=True),
            sa.Column("candidate_name", sa.String(255), nullable=False, server_default=""),
            sa.Column("score", sa.Float(), nullable=False, server_default="0.0"),
            sa.Column("reason", sa.Text(), nullable=True),
            sa.Column("model_version", sa.String(64), nullable=False, server_default="rule"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_segment_candidates_seg", "segment_candidates", ["segment_id"])

    # M2: voice_profiles
    if not _table_exists(inspector, "voice_profiles"):
        op.create_table(
            "voice_profiles",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("novel_id", sa.String(36), sa.ForeignKey("novels.id", ondelete="CASCADE"), nullable=False),
            sa.Column("character_id", sa.String(36), sa.ForeignKey("characters.id", ondelete="CASCADE"), nullable=True),
            sa.Column("narrator_key", sa.String(64), nullable=True),
            sa.Column("display_name", sa.String(255), nullable=False),
            sa.Column("provider", sa.String(64), nullable=False, server_default="cosyvoice"),
            sa.Column("target_model", sa.String(64), nullable=False, server_default="cosyvoice-v3.5-flash"),
            sa.Column("voice_prompt", sa.Text(), nullable=False, server_default=""),
            sa.Column("voice_id", sa.String(128), nullable=True),
            sa.Column("sample_audio_ref", sa.String(512), nullable=True),
            sa.Column("language", sa.String(32), nullable=False, server_default="zh-CN"),
            sa.Column("default_rate", sa.Float(), nullable=False, server_default="1.0"),
            sa.Column("pitch", sa.Float(), nullable=False, server_default="1.0"),
            sa.Column("seed", sa.Integer(), nullable=True),
            sa.Column("profile_version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("approval_status", sa.String(32), nullable=False, server_default="draft"),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("candidates_json", sa.JSON(), nullable=False),
            sa.Column("consent_confirmed", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("consent_note", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_voice_profiles_novel_char", "voice_profiles", ["novel_id", "character_id"])
        op.create_index("ix_voice_profiles_novel_narrator", "voice_profiles", ["novel_id", "narrator_key"])

    # M2: voice_performances
    if not _table_exists(inspector, "voice_performances"):
        op.create_table(
            "voice_performances",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("segment_id", sa.String(36), sa.ForeignKey("script_segments.id", ondelete="CASCADE"), nullable=False),
            sa.Column("emotion", sa.String(32), nullable=False, server_default="neutral"),
            sa.Column("intensity", sa.Float(), nullable=False, server_default="0.5"),
            sa.Column("instruction", sa.Text(), nullable=True),
            sa.Column("rate", sa.Float(), nullable=False, server_default="1.0"),
            sa.Column("pitch", sa.Float(), nullable=False, server_default="1.0"),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_voice_perf_segment", "voice_performances", ["segment_id"])

    # M3: render_jobs
    if not _table_exists(inspector, "render_jobs"):
        op.create_table(
            "render_jobs",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("novel_id", sa.String(36), sa.ForeignKey("novels.id", ondelete="CASCADE"), nullable=False),
            sa.Column("episode_id", sa.String(36), sa.ForeignKey("episodes.id", ondelete="CASCADE"), nullable=False),
            sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
            sa.Column("budget_limit", sa.Float(), nullable=False, server_default="20.0"),
            sa.Column("spent_cost", sa.Float(), nullable=False, server_default="0.0"),
            sa.Column("source_revision", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("profile_snapshot_json", sa.JSON(), nullable=False),
            sa.Column("progress_json", sa.JSON(), nullable=False),
            sa.Column("idempotency_key", sa.String(128), nullable=True),
            sa.Column("error_summary", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_render_jobs_episode", "render_jobs", ["episode_id", "created_at"])

    # M3: audio_segments
    if not _table_exists(inspector, "audio_segments"):
        op.create_table(
            "audio_segments",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("novel_id", sa.String(36), sa.ForeignKey("novels.id", ondelete="CASCADE"), nullable=False),
            sa.Column("episode_id", sa.String(36), sa.ForeignKey("episodes.id", ondelete="CASCADE"), nullable=False),
            sa.Column("script_segment_id", sa.String(36), sa.ForeignKey("script_segments.id", ondelete="CASCADE"), nullable=False),
            sa.Column("render_job_id", sa.String(36), sa.ForeignKey("render_jobs.id", ondelete="SET NULL"), nullable=True),
            sa.Column("voice_profile_id", sa.String(36), sa.ForeignKey("voice_profiles.id", ondelete="SET NULL"), nullable=True),
            sa.Column("voice_profile_version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("voice_id", sa.String(128), nullable=False, server_default=""),
            sa.Column("audio_ref", sa.String(512), nullable=True),
            sa.Column("audio_format", sa.String(16), nullable=False, server_default="wav"),
            sa.Column("duration_ms", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("file_size_bytes", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("checksum", sa.String(64), nullable=False, server_default=""),
            sa.Column("cache_key", sa.String(128), nullable=False, server_default=""),
            sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_audio_segments_script_seg", "audio_segments", ["script_segment_id"])
        op.create_index("ix_audio_segments_cache_key", "audio_segments", ["cache_key"])

    # M3: audio_exports
    if not _table_exists(inspector, "audio_exports"):
        op.create_table(
            "audio_exports",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("novel_id", sa.String(36), sa.ForeignKey("novels.id", ondelete="CASCADE"), nullable=False),
            sa.Column("episode_id", sa.String(36), sa.ForeignKey("episodes.id", ondelete="SET NULL"), nullable=True),
            sa.Column("format", sa.String(16), nullable=False, server_default="mp3"),
            sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
            sa.Column("manifest_ref", sa.String(512), nullable=True),
            sa.Column("file_ref", sa.String(512), nullable=True),
            sa.Column("file_size_bytes", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("duration_ms", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_audio_exports_novel", "audio_exports", ["novel_id", "created_at"])

    # M3: provider_calls
    if not _table_exists(inspector, "provider_calls"):
        op.create_table(
            "provider_calls",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("novel_id", sa.String(36), sa.ForeignKey("novels.id", ondelete="CASCADE"), nullable=False),
            sa.Column("render_job_id", sa.String(36), sa.ForeignKey("render_jobs.id", ondelete="SET NULL"), nullable=True),
            sa.Column("provider", sa.String(64), nullable=False),
            sa.Column("model", sa.String(64), nullable=False),
            sa.Column("operation", sa.String(64), nullable=False, server_default="synthesize"),
            sa.Column("request_id", sa.String(128), nullable=True),
            sa.Column("input_chars", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("audio_duration_ms", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("estimated_cost", sa.Float(), nullable=False, server_default="0.0"),
            sa.Column("actual_cost", sa.Float(), nullable=False, server_default="0.0"),
            sa.Column("latency_ms", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("status", sa.String(32), nullable=False, server_default="ok"),
            sa.Column("error_type", sa.String(64), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_provider_calls_novel", "provider_calls", ["novel_id", "provider"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    for table in (
        "provider_calls",
        "audio_exports",
        "audio_segments",
        "render_jobs",
        "voice_performances",
        "voice_profiles",
        "segment_candidates",
        "script_segments",
        "episodes",
        "episode_plans",
    ):
        if _table_exists(inspector, table):
            op.drop_table(table)

    if _table_exists(inspector, "characters") and _column_exists(inspector, "characters", "analysis_status"):
        op.drop_column("characters", "analysis_status")
    if _table_exists(inspector, "novels") and _column_exists(inspector, "novels", "copyright_confirmed"):
        op.drop_column("novels", "copyright_confirmed")
    if _table_exists(inspector, "novels") and _column_exists(inspector, "novels", "license_scope"):
        op.drop_column("novels", "license_scope")
