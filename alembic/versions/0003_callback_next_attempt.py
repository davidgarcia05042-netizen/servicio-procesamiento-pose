"""Hora del próximo intento de cada aviso

Agrega `callback_next_attempt_at`: cuándo toca volver a intentar un aviso que
la plataforma todavía no recibió.

Hasta acá esa espera vivía en Redis, como una tarea de Celery programada con
countdown. Eso tenía dos problemas verificados el 2026-09-24: si la espera
superaba el visibility_timeout, Redis re-encolaba la tarea y la plataforma
recibía el aviso duplicado; y si el worker se reiniciaba mientras la guardaba,
el reintento quedaba varado hasta que venciera ese plazo. Con la hora en la
tabla, la cola solo lleva tareas inmediatas y un reinicio no retiene nada.

El índice es para el despachador, que busca cada pocos segundos los avisos
cuyo intento ya venció.

Revision ID: 0003_callback_next_attempt
Revises: 0002_callback_delivery
Create Date: 2026-09-24

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0003_callback_next_attempt"
down_revision: Union[str, None] = "0002_callback_delivery"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column("callback_next_attempt_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        op.f("ix_jobs_callback_next_attempt_at"),
        "jobs",
        ["callback_next_attempt_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_jobs_callback_next_attempt_at"), table_name="jobs")
    op.drop_column("jobs", "callback_next_attempt_at")
