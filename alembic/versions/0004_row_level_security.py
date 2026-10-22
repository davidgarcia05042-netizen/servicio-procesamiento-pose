"""Cerrar las tablas a la API REST de Supabase

La base de este servicio puede vivir en Supabase (decisión del 2026-10-06, fase
piloto). Supabase publica por REST (su "Data API") las tablas del esquema
`public`, y por defecto les da permisos de lectura y escritura a los roles
`anon` y `authenticated`. La clave de `anon` está pensada para ir en el código
de un navegador, o sea que se considera pública.

Sin esto, cualquiera con esa clave podría:

  - leer `jobs`, que tiene resultados de salud de pacientes y las URLs de la
    plataforma;
  - borrar la fila de `alembic_version`, y el siguiente despliegue intentaría
    crear desde cero tablas que ya existen.

Activar RLS sin escribir ninguna política deja ambas tablas sin acceso para
esos roles. Al servicio no le afecta: se conecta como el dueño de las tablas, y
el dueño no está sujeto a RLS mientras no se use FORCE ROW LEVEL SECURITY.

Es una segunda barrera. La primera es desactivar la Data API en el proyecto de
Supabase (ver README, "Si la base termina en Supabase"). En un PostgreSQL sin
Supabase (el local, el de la demo o el de Render) no hay roles que lo noten, y
no cambia nada.

Revision ID: 0004_row_level_security
Revises: 0003_callback_next_attempt
Create Date: 2026-10-06

"""

from typing import Sequence, Union

from alembic import op

revision: str = "0004_row_level_security"
down_revision: Union[str, None] = "0003_callback_next_attempt"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLES = ("jobs", "alembic_version")


def upgrade() -> None:
    for table in TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    for table in TABLES:
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
