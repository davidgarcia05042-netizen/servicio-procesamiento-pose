"""
Ata las migraciones de Alembic a los modelos de SQLAlchemy.

Si alguien agrega una columna al modelo y olvida generar la migración, este
test falla: es el guardarraíl del flujo documentado en el README. Antes ese
olvido no se notaba hasta que la app explotaba en una base real, porque
`create_all` tapaba la diferencia.

Corre OFFLINE (`alembic upgrade head --sql`), así que no necesita PostgreSQL:
compara el SQL que emitiría cada camino, no una base ya construida.
"""

import re
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_mock_engine

from app.utils.db_connect import Base

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Sentencias que son del andamiaje de Alembic, no del esquema de la aplicación.
_ALEMBIC_BOOKKEEPING = (
    "begin",
    "commit",
    "create table alembic_version",
    "alter table alembic_version",
    "insert into alembic_version",
    "update alembic_version",
    "delete from alembic_version",
)


_CONSTRAINT_PREFIXES = ("primary key", "foreign key", "unique", "check", "constraint")


def _normalize(ddl: str) -> list[str]:
    """Parte en sentencias y aplana el formato, para comparar por contenido."""
    ddl = re.sub(r"--.*", "", ddl)

    statements = []
    for raw in ddl.split(";"):
        cleaned = " ".join(raw.split())
        if cleaned and not cleaned.lower().startswith(_ALEMBIC_BOOKKEEPING):
            statements.append(cleaned)

    return sorted(statements)


def _split_top_level(body: str) -> list[str]:
    """Parte por comas, ignorando las que van dentro de paréntesis (VARCHAR(36))."""
    parts: list[str] = []
    depth = 0
    current: list[str] = []

    for char in body:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1

        if char == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(char)

    if current:
        parts.append("".join(current).strip())

    return [part for part in parts if part]


def _schema_from(statements: list[str]) -> dict[str, dict]:
    """
    Reduce una lista de sentencias DDL al esquema que dejan.

    HACE FALTA porque los dos caminos llegan al mismo esquema por rutas
    distintas: los modelos emiten un CREATE TABLE con todas las columnas, y
    las migraciones emiten el CREATE TABLE original más un ALTER TABLE por
    cada columna agregada después. Comparar el texto plano marcaría diferencia
    donde no la hay.

    Ante una sentencia que no sabe interpretar, FALLA en vez de ignorarla: si
    una migración futura borra o renombra una columna, es preferible que este
    test pida que lo extiendan a que pase en verde tapando el cambio.
    """
    columns: dict[str, set[str]] = {}
    constraints: dict[str, set[str]] = {}
    indexes: set[str] = set()
    row_security: set[str] = set()
    storage: set[str] = set()

    for statement in statements:
        flat = " ".join(statement.split())
        lowered = flat.lower()

        created = re.match(r"create table (\w+) \((.*)\)$", flat, re.IGNORECASE)
        if created:
            table, body = created.group(1), created.group(2)
            for item in _split_top_level(body):
                target = (
                    constraints if item.lower().startswith(_CONSTRAINT_PREFIXES) else columns
                )
                target.setdefault(table, set()).add(" ".join(item.split()).lower())
            continue

        added = re.match(r"alter table (\w+) add column (.+)$", flat, re.IGNORECASE)
        if added:
            columns.setdefault(added.group(1), set()).add(added.group(2).lower())
            continue

        if lowered.startswith(("create index", "create unique index")):
            indexes.add(lowered)
            continue

        # Control de acceso, no forma del esquema: los modelos no lo expresan,
        # así que se junta aparte y no entra en la comparación.
        rls = re.match(r"alter table (\w+) (enable|disable) row level security$", lowered)
        if rls:
            row_security.add(f"{rls.group(1)} {rls.group(2)}")
            continue

        # Cómo guarda PostgreSQL una columna en disco, no su forma: tampoco lo
        # expresan los modelos.
        stored = re.match(r"alter table (\w+) alter column (\w+) set storage (\w+)$", lowered)
        if stored:
            storage.add(f"{stored.group(1)}.{stored.group(2)} {stored.group(3)}")
            continue

        raise AssertionError(
            f"El test no sabe interpretar esta sentencia DDL: {flat!r}. "
            "Si es un cambio legítimo (borrar o renombrar una columna), hay que "
            "enseñarle a _schema_from a entenderlo."
        )

    return {
        "columns": columns,
        "constraints": constraints,
        "indexes": indexes,
        "row_security": row_security,
        "storage": storage,
    }


def _ddl_from_models() -> list[str]:
    """El DDL que produciría metadata.create_all contra PostgreSQL."""
    statements: list[str] = []

    def capture(sql, *args, **kwargs):
        statements.append(str(sql.compile(dialect=engine.dialect)))

    engine = create_mock_engine("postgresql://", capture)
    Base.metadata.create_all(engine, checkfirst=False)

    # El mock no agrega ';': cada sentencia ya viene separada en la lista.
    return _normalize(";\n".join(statements) + ";")


def _ddl_from_migrations() -> list[str]:
    """El DDL que emite aplicar todas las migraciones, sin tocar una base real."""
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head", "--sql"],
        capture_output=True,
        text=True,
        cwd=PROJECT_ROOT,
    )
    if result.returncode != 0:
        pytest.fail(f"'alembic upgrade head --sql' falló:\n{result.stderr}")

    return _normalize(result.stdout)


def test_migrations_produce_the_same_schema_as_the_models():
    from_models = _schema_from(_ddl_from_models())
    from_migrations = _schema_from(_ddl_from_migrations())

    for table, expected in from_models["columns"].items():
        actual = from_migrations["columns"].get(table, set())
        assert actual == expected, (
            f"Las columnas de '{table}' no coinciden con el modelo.\n"
            "¿Cambiaste un modelo sin generar su migración?\n"
            f"Solo en el modelo:      {sorted(expected - actual)}\n"
            f"Solo en la migración:   {sorted(actual - expected)}"
        )

    assert from_migrations["columns"].keys() == from_models["columns"].keys()
    assert from_migrations["constraints"] == from_models["constraints"]
    assert from_migrations["indexes"] == from_models["indexes"]


def test_migrations_create_the_jobs_table():
    """Comprobación explícita, para que un fallo del test de arriba se lea fácil."""
    ddl = " ".join(_ddl_from_migrations()).lower()

    assert "create table jobs" in ddl
    assert "results jsonb" in ddl  # el payload variable sigue siendo JSONB
    assert "create index ix_jobs_status" in ddl


def test_migrations_close_the_tables_to_the_supabase_rest_api():
    """
    Con RLS activo y sin políticas, los roles anon/authenticated de Supabase
    no ven ni tocan nada. `alembic_version` también: borrar su fila haría que
    el siguiente despliegue intentara crear de nuevo tablas que ya existen.
    """
    # Sin pasar por _normalize, que descarta lo que toca a alembic_version.
    raw = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head", "--sql"],
        capture_output=True,
        text=True,
        cwd=PROJECT_ROOT,
        check=True,
    ).stdout
    raw = re.sub(r"--.*", "", raw)
    statements = [
        " ".join(s.split()) for s in raw.split(";") if "row level security" in s.lower()
    ]

    assert _schema_from(statements)["row_security"] == {
        "jobs enable",
        "alembic_version enable",
        "videos enable",
    }


def test_the_videos_table_stores_the_content_without_compressing_it():
    """
    Un video ya viene comprimido: PostgreSQL gastaría CPU en intentarlo de
    nuevo para terminar guardándolo igual (ver 0005_videos_table).
    """
    ddl = " ".join(_ddl_from_migrations()).lower()

    assert "create table videos" in ddl
    assert "content bytea not null" in ddl
    assert _schema_from(_ddl_from_migrations())["storage"] == {"videos.content external"}
