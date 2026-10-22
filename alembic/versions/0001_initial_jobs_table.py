"""Esquema inicial: tabla jobs

Captura el estado que hasta ahora producía `Base.metadata.create_all`, incluidas
las columnas `pose_model` y `processing_seconds` del registro estructurado.

Revision ID: 0001_initial_jobs
Revises:
Create Date: 2026-09-16

"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0001_initial_jobs"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("exercise", sa.String(length=100), nullable=True),
        sa.Column("results_endpoint", sa.Text(), nullable=True),
        sa.Column("video_url", sa.Text(), nullable=True),
        # JSONB y no JSON: el payload de resultados cambia de forma según el
        # ejercicio, y JSONB permite indexarlo y consultarlo si hace falta.
        sa.Column("results", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("max_angle", sa.Float(), nullable=True),
        sa.Column("min_angle", sa.Float(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("pose_model", sa.String(length=50), nullable=True),
        sa.Column("processing_seconds", sa.Float(), nullable=True),
        # Sin server_default: los timestamps los pone Python (_utcnow) en el
        # modelo, igual que hacía create_all.
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_jobs_status"), "jobs", ["status"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_jobs_status"), table_name="jobs")
    op.drop_table("jobs")
