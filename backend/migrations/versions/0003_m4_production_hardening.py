"""Incremental migration: M4 production hardening (stable segments, voice versions, synthesis units, book render jobs, billing & export snapshots).

Revision ID: 0003_m4_production_hardening
Revises: 0002_audiobook_and_hardening
Create Date: 2026-11-19
"""

from alembic import op
import sqlalchemy as sa

revision = "0003_m4_production_hardening"
down_revision = "0002_audiobook_and_hardening"
branch_labels = None
depends_on = None


def _table_exists(inspector: sa.Inspector, table_name: str) -> bool:
    return table_name in inspector.get_table_names()


def _column_exists(inspector: sa.Inspector, table_name: str, column_name: str) -> bool:
    return any(col["name"] == column_name for col in inspector.get_columns(table_name))


def _index_exists(inspector: sa.Inspector, table_name: str, index_name: str) -> bool:
    return any(idx["name"] == index_name for idx in inspector.get_indexes(table_name))


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    dialect = bind.dialect.name

    # 1. script_segments: stable_span_key, content_revision, analysis_run_id, is_render_blocked
    if _table_exists(inspector, "script_segments"):
        if not _column_exists(inspector, "script_segments", "stable_span_key"):
            op.add_column("script_segments", sa.Column("stable_span_key", sa.String(128), nullable=True))
        if not _column_exists(inspector, "script_segments", "content_revision"):
            op.add_column(
                "script_segments",
                sa.Column("content_revision", sa.Integer(), nullable=False, server_default="1"),
            )
        if not _column_exists(inspector, "script_segments", "analysis_run_id"):
            op.add_column("script_segments", sa.Column("analysis_run_id", sa.String(36), nullable=True))
        if not _column_exists(inspector, "script_segments", "is_render_blocked"):
            op.add_column(
                "script_segments",
                sa.Column("is_render_blocked", sa.Boolean(), nullable=False, server_default=sa.false()),
            )
        if not _index_exists(inspector, "script_segments", "ix_script_segments_stable_span_key"):
            op.create_index("ix_script_segments_stable_span_key", "script_segments", ["stable_span_key"])

    # 2. voice_profiles + voice_profile_versions
    if _table_exists(inspector, "voice_profiles"):
        if not _column_exists(inspector, "voice_profiles", "provider_status"):
            op.add_column(
                "voice_profiles",
                sa.Column("provider_status", sa.String(32), nullable=False, server_default="OK"),
            )
        if not _column_exists(inspector, "voice_profiles", "preview_checksum"):
            op.add_column("voice_profiles", sa.Column("preview_checksum", sa.String(64), nullable=True))
        if not _column_exists(inspector, "voice_profiles", "prompt_version"):
            op.add_column(
                "voice_profiles",
                sa.Column("prompt_version", sa.Integer(), nullable=False, server_default="1"),
            )

    if not _table_exists(inspector, "voice_profile_versions"):
        op.create_table(
            "voice_profile_versions",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "voice_profile_id",
                sa.String(36),
                sa.ForeignKey("voice_profiles.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "novel_id",
                sa.String(36),
                sa.ForeignKey("novels.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("voice_id", sa.String(128), nullable=True),
            sa.Column("target_model", sa.String(64), nullable=False, server_default="cosyvoice-v3.5-flash"),
            sa.Column("voice_prompt", sa.Text(), nullable=False, server_default=""),
            sa.Column("default_rate", sa.Float(), nullable=False, server_default="1.0"),
            sa.Column("pitch", sa.Float(), nullable=False, server_default="1.0"),
            sa.Column("provider_status", sa.String(32), nullable=False, server_default="OK"),
            sa.Column("preview_checksum", sa.String(64), nullable=True),
            sa.Column("approval_status", sa.String(32), nullable=False, server_default="draft"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("voice_profile_id", "version", name="uq_voice_profile_ver"),
        )
        op.create_index(
            "ix_voice_profile_ver_profile",
            "voice_profile_versions",
            ["voice_profile_id", "version"],
        )

    # 3. book_render_jobs
    if not _table_exists(inspector, "book_render_jobs"):
        op.create_table(
            "book_render_jobs",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "novel_id",
                sa.String(36),
                sa.ForeignKey("novels.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "plan_id",
                sa.String(36),
                sa.ForeignKey("episode_plans.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
            sa.Column("episode_ids_json", sa.JSON(), nullable=False),
            sa.Column("budget_limit", sa.Float(), nullable=False, server_default="100.0"),
            sa.Column("spent_cost", sa.Float(), nullable=False, server_default="0.0"),
            sa.Column("currency", sa.String(8), nullable=False, server_default="CNY"),
            sa.Column("progress_json", sa.JSON(), nullable=False),
            sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("error_summary", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_book_render_jobs_novel", "book_render_jobs", ["novel_id", "created_at"])

    # 4. render_jobs
    if _table_exists(inspector, "render_jobs"):
        if not _column_exists(inspector, "render_jobs", "book_render_job_id"):
            op.add_column("render_jobs", sa.Column("book_render_job_id", sa.String(36), nullable=True))
        if not _column_exists(inspector, "render_jobs", "cancel_requested"):
            op.add_column(
                "render_jobs",
                sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
            )
        if not _column_exists(inspector, "render_jobs", "paused_at"):
            op.add_column("render_jobs", sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True))
        if not _column_exists(inspector, "render_jobs", "resume_cursor"):
            op.add_column(
                "render_jobs",
                sa.Column("resume_cursor", sa.Integer(), nullable=False, server_default="0"),
            )

    # 5. synthesis_units
    if not _table_exists(inspector, "synthesis_units"):
        op.create_table(
            "synthesis_units",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "novel_id",
                sa.String(36),
                sa.ForeignKey("novels.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "episode_id",
                sa.String(36),
                sa.ForeignKey("episodes.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "script_segment_id",
                sa.String(36),
                sa.ForeignKey("script_segments.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("unit_index", sa.Integer(), nullable=False),
            sa.Column("start_offset", sa.Integer(), nullable=False),
            sa.Column("end_offset", sa.Integer(), nullable=False),
            sa.Column("text_content", sa.Text(), nullable=False),
            sa.Column("text_hash", sa.String(64), nullable=False),
            sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
            sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("audio_ref", sa.String(512), nullable=True),
            sa.Column("audio_format", sa.String(16), nullable=False, server_default="wav"),
            sa.Column("duration_ms", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("checksum", sa.String(64), nullable=False, server_default=""),
            sa.Column("cache_key", sa.String(128), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("script_segment_id", "unit_index", name="uq_synthesis_units_seg_idx"),
        )
        op.create_index("ix_synthesis_units_seg", "synthesis_units", ["script_segment_id", "unit_index"])
        op.create_index("ix_synthesis_units_ep_status", "synthesis_units", ["episode_id", "status"])

    # 6. audio_segments
    if _table_exists(inspector, "audio_segments"):
        for col_name, col_obj in [
            ("script_revision", sa.Column("script_revision", sa.Integer(), nullable=False, server_default="1")),
            ("render_input_hash", sa.Column("render_input_hash", sa.String(128), nullable=False, server_default="")),
            ("sample_rate", sa.Column("sample_rate", sa.Integer(), nullable=False, server_default="24000")),
            ("channels", sa.Column("channels", sa.Integer(), nullable=False, server_default="1")),
            ("is_mock", sa.Column("is_mock", sa.Boolean(), nullable=False, server_default=sa.false())),
            ("provider", sa.Column("provider", sa.String(64), nullable=False, server_default="cosyvoice")),
            ("model", sa.Column("model", sa.String(64), nullable=False, server_default="cosyvoice-v3.5-flash")),
        ]:
            if not _column_exists(inspector, "audio_segments", col_name):
                op.add_column("audio_segments", col_obj)

        # Partial unique index ensuring at most 1 current AudioSegment per script_segment_id
        if not _index_exists(inspector, "audio_segments", "uq_audio_segments_one_current"):
            if dialect == "postgresql":
                op.execute(
                    "CREATE UNIQUE INDEX uq_audio_segments_one_current "
                    "ON audio_segments (script_segment_id) WHERE is_current = true"
                )
            elif dialect == "sqlite":
                op.execute(
                    "CREATE UNIQUE INDEX uq_audio_segments_one_current "
                    "ON audio_segments (script_segment_id) WHERE is_current = 1"
                )

    # 7. audio_exports
    if _table_exists(inspector, "audio_exports"):
        for col_name, col_obj in [
            ("plan_id", sa.Column("plan_id", sa.String(36), nullable=True)),
            ("actual_format", sa.Column("actual_format", sa.String(16), nullable=False, server_default="")),
            ("checksum", sa.Column("checksum", sa.String(64), nullable=False, server_default="")),
            ("source_snapshot_hash", sa.Column("source_snapshot_hash", sa.String(64), nullable=False, server_default="")),
            ("is_stale", sa.Column("is_stale", sa.Boolean(), nullable=False, server_default=sa.false())),
            ("is_mock", sa.Column("is_mock", sa.Boolean(), nullable=False, server_default=sa.false())),
        ]:
            if not _column_exists(inspector, "audio_exports", col_name):
                op.add_column("audio_exports", col_obj)

    # 8. provider_calls
    if _table_exists(inspector, "provider_calls"):
        for col_name, col_obj in [
            ("billed_characters", sa.Column("billed_characters", sa.Integer(), nullable=True)),
            ("currency", sa.Column("currency", sa.String(8), nullable=False, server_default="CNY")),
            ("cost_kind", sa.Column("cost_kind", sa.String(16), nullable=False, server_default="estimated")),
            ("provider_request_status", sa.Column("provider_request_status", sa.String(32), nullable=True)),
            ("is_mock", sa.Column("is_mock", sa.Boolean(), nullable=False, server_default=sa.false())),
        ]:
            if not _column_exists(inspector, "provider_calls", col_name):
                op.add_column("provider_calls", col_obj)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _table_exists(inspector, "audio_segments") and _index_exists(
        inspector, "audio_segments", "uq_audio_segments_one_current"
    ):
        op.drop_index("uq_audio_segments_one_current", table_name="audio_segments")

    for tbl in ("synthesis_units", "book_render_jobs", "voice_profile_versions"):
        if _table_exists(inspector, tbl):
            op.drop_table(tbl)
