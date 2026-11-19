"""Initial NCKE schema (all 21 core tables + pgvector extension on PostgreSQL).

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-11-19
"""

from alembic import op

from app.infrastructure.db import models  # noqa: F401
from app.infrastructure.db.session import Base

revision = "0001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    Base.metadata.create_all(bind=bind)


def downgrade() -> None:
    bind = op.get_bind()
    Base.metadata.drop_all(bind=bind)
