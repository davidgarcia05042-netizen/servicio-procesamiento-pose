"""Tabla videos: los videos en espera, cuando se guardan en la propia base

Decisión del 2026-10-08: todo el servicio en Render, incluidos los videos. Render
no tiene un almacenamiento de objetos estable, así que el video espera en esta
tabla hasta que el worker lo procesa y lo borra (ver
app/platforms/render/postgres_storage.py). Con otro almacenamiento, la tabla
queda vacía.

Dos detalles que el modelo no expresa:

  - La columna del contenido va SIN COMPRIMIR (STORAGE EXTERNAL). Un video ya
    viene comprimido; PostgreSQL intentaría comprimirlo de nuevo, gastando CPU
    para terminar guardándolo igual.
  - RLS activo, igual que `jobs` (ver 0004_row_level_security): si la base
    terminara en Supabase, su API REST no puede ver los videos.

Revision ID: 0005_videos_table
Revises: 0004_row_level_security
Create Date: 2026-10-08

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0005_videos_table"
down_revision: Union[str, None] = "0004_row_level_security"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "videos",
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("path"),
    )
    op.execute("ALTER TABLE videos ALTER COLUMN content SET STORAGE EXTERNAL")
    op.execute("ALTER TABLE videos ENABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.drop_table("videos")
