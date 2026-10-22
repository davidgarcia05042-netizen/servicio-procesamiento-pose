"""Desenlace de la entrega del callback

Agrega a `jobs` el resultado del aviso a la plataforma. Es una dimensión
aparte de `status`: un job puede quedar `done` (los ángulos se calcularon y
están guardados) y aun así tener el callback en `exhausted` porque la
plataforma estuvo caída durante toda la ventana de reintentos.

Va en la tabla y no solo en el log porque los logs de Render son efímeros, y
esto es justo lo que hay que poder consultar después para saber qué resultados
nunca llegaron.

Revision ID: 0002_callback_delivery
Revises: 0001_initial_jobs
Create Date: 2026-09-19

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0002_callback_delivery"
down_revision: Union[str, None] = "0001_initial_jobs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Nullable las tres: las filas que ya existen se procesaron antes de que
    # esto existiera, y no hay valor honesto que ponerles. NULL significa
    # "nunca se registró", que es la verdad.
    op.add_column("jobs", sa.Column("callback_status", sa.String(length=20), nullable=True))
    op.add_column("jobs", sa.Column("callback_attempts", sa.Integer(), nullable=True))
    op.add_column("jobs", sa.Column("callback_error", sa.Text(), nullable=True))
    # Indexada porque la consulta que importa es "¿qué entregas quedaron sin
    # resolver?", que filtra justamente por esta columna.
    op.create_index(op.f("ix_jobs_callback_status"), "jobs", ["callback_status"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_jobs_callback_status"), table_name="jobs")
    op.drop_column("jobs", "callback_error")
    op.drop_column("jobs", "callback_attempts")
    op.drop_column("jobs", "callback_status")
