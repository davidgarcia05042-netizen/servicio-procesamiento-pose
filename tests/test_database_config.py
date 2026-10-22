"""
Driver de la URL de PostgreSQL (app/core/config.py).

Render entrega la URL de su base sin driver (`postgresql://...`) y este
servicio habla con PostgreSQL SOLO de forma asíncrona: asyncpg está en
requirements.txt y psycopg2 no. Sin normalizar el esquema, tanto el engine de
la aplicación como `alembic upgrade head` -- que es el comando de
pre-despliegue en Render -- fallarían con ModuleNotFoundError.

Se construyen instancias de Settings propias para no depender del .env del
desarrollador.
"""

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import Settings

# Forma exacta en la que Render expone `connectionString` de su PostgreSQL.
RENDER_URL = "postgresql://pose_user:claveDeRender@dpg-cl4bq5ij1k6c73a1b2c0/pose_service"


def _settings(**overrides) -> Settings:
    base = {"_env_file": None}
    base.update(overrides)
    return Settings(**base)


def test_the_render_url_gets_the_async_driver():
    config = _settings(DATABASE_URL=RENDER_URL)

    assert config.DATABASE_URL.startswith("postgresql+asyncpg://")
    assert "pose_user:claveDeRender@dpg-cl4bq5ij1k6c73a1b2c0/pose_service" in config.DATABASE_URL


def test_the_legacy_postgres_scheme_is_also_normalized():
    """Varios proveedores siguen entregando `postgres://` a secas."""
    config = _settings(DATABASE_URL="postgres://u:p@host/base")

    assert config.DATABASE_URL == "postgresql+asyncpg://u:p@host/base"


def test_an_explicit_driver_is_left_alone():
    config = _settings(DATABASE_URL="postgresql+asyncpg://u:p@host/base")

    assert config.DATABASE_URL == "postgresql+asyncpg://u:p@host/base"


def test_only_the_scheme_is_replaced():
    """
    La sustitución es del prefijo, no global: una contraseña o un nombre de
    base que contengan "postgres" no se deben tocar.
    """
    config = _settings(DATABASE_URL="postgresql://u:postgres@host/postgres")

    assert config.DATABASE_URL == "postgresql+asyncpg://u:postgres@host/postgres"


# --- Lo que de verdad importa: que el engine se pueda construir ---


def test_the_normalized_url_resolves_to_asyncpg():
    """
    `create_async_engine` resuelve e importa el driver al construirse, sin
    abrir ninguna conexión. Que llegue a `asyncpg` es la prueba de que el
    despliegue en Render arrancaría.
    """
    config = _settings(DATABASE_URL=RENDER_URL)

    engine = create_async_engine(config.DATABASE_URL)

    assert engine.dialect.driver == "asyncpg"


def test_the_raw_render_url_would_not_work(recwarn):
    """
    Control negativo: sin la normalización, esta misma URL revienta. Es lo que
    justifica que el validador exista, y lo que haría fallar el
    `preDeployCommand` (alembic upgrade head) antes de crear la tabla jobs.
    """
    with pytest.raises(Exception) as failure:
        create_async_engine(RENDER_URL)

    # psycopg2 no está instalado (ModuleNotFoundError); si alguien lo agregara,
    # SQLAlchemy igual rechaza un driver síncrono bajo la extensión asyncio.
    assert "psycopg2" in str(failure.value) or "async" in str(failure.value).lower()


# --- URLs de proveedores externos (Supabase) y TLS en producción ---

# Forma del "Session pooler" de Supabase: el único modo que sirve desde Render
# (sin IPv6, y el modo "Transaction" no admite prepared statements de asyncpg).
SUPABASE_URL = (
    "postgresql://postgres.abcdefghij:clave@aws-0-us-west-2.pooler.supabase.com:5432/postgres"
)


def test_sslmode_is_translated_to_what_asyncpg_understands():
    config = _settings(DATABASE_URL=SUPABASE_URL + "?sslmode=require")

    assert config.DATABASE_URL.endswith("/postgres?ssl=require")
    assert "sslmode" not in config.DATABASE_URL


def test_the_translated_url_reaches_asyncpg_without_unknown_arguments():
    """
    Sin la traducción, SQLAlchemy le pasa `sslmode` a asyncpg.connect(), que
    no lo conoce: TypeError al primer intento de conexión.
    """
    import asyncpg
    import inspect

    config = _settings(DATABASE_URL=SUPABASE_URL + "?sslmode=require")
    engine = create_async_engine(config.DATABASE_URL)
    _args, kwargs = engine.dialect.create_connect_args(engine.url)

    accepted = inspect.signature(asyncpg.connect).parameters
    assert kwargs["ssl"] == "require"
    assert set(kwargs) <= set(accepted)


def test_an_explicit_ssl_wins_over_sslmode():
    config = _settings(DATABASE_URL=SUPABASE_URL + "?sslmode=disable&ssl=require")

    assert config.DATABASE_URL.endswith("/postgres?ssl=require")


def test_other_query_parameters_survive_the_translation():
    config = _settings(DATABASE_URL=SUPABASE_URL + "?application_name=pose&sslmode=verify-full")

    assert config.DATABASE_URL.endswith("?application_name=pose&ssl=verify-full")


def test_production_rejects_an_external_database_without_tls():
    config = _settings(ENVIRONMENT="production", DATABASE_URL=SUPABASE_URL)

    with pytest.raises(ValueError, match="exige TLS hacia una base fuera de la red privada"):
        _ = config.database_url


@pytest.mark.parametrize("weak", ["disable", "allow", "prefer"])
def test_production_rejects_ssl_modes_that_can_fall_back_to_plaintext(weak):
    config = _settings(ENVIRONMENT="production", DATABASE_URL=f"{SUPABASE_URL}?sslmode={weak}")

    with pytest.raises(ValueError, match="exige TLS"):
        _ = config.database_url


@pytest.mark.parametrize("strong", ["require", "verify-ca", "verify-full"])
def test_production_accepts_an_external_database_with_tls(strong):
    config = _settings(ENVIRONMENT="production", DATABASE_URL=f"{SUPABASE_URL}?sslmode={strong}")

    assert config.database_url.endswith(f"ssl={strong}")


def test_production_accepts_the_render_internal_database_without_tls():
    """La base de Render va por la red privada: igual que Redis "red-...". """
    config = _settings(ENVIRONMENT="production", DATABASE_URL=RENDER_URL)

    assert config.database_url.startswith("postgresql+asyncpg://")


@pytest.mark.parametrize(
    "lookalike",
    ["dpg-cl4bq5ij1k6c73a1b2c0-a.oregon-postgres.render.com", "dpg-abc.atacante.com"],
)
def test_production_requires_tls_for_any_host_with_a_domain(lookalike):
    """
    El host EXTERNO de Render (con dominio) sale a internet: no es la red
    privada aunque empiece igual.
    """
    config = _settings(ENVIRONMENT="production", DATABASE_URL=f"postgresql://u:p@{lookalike}/pose")

    with pytest.raises(ValueError, match="exige TLS"):
        _ = config.database_url


def test_development_does_not_require_tls():
    config = _settings(DATABASE_URL="postgresql+asyncpg://postgres:postgres@localhost:5433/pose_service")

    assert config.database_url == config.DATABASE_URL


def test_the_engine_and_alembic_use_the_checked_url():
    """Si alguno leyera DATABASE_URL directo, se saltaría la exigencia de TLS."""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    for relative in ("app/utils/db_connect.py", "alembic/env.py"):
        source = (root / relative).read_text(encoding="utf-8")
        assert "settings.database_url" in source, relative
        assert "settings.DATABASE_URL" not in source, relative


# --- Socket Unix: Cloud SQL desde Cloud Run ---

# Forma en la que Cloud Run entrega Cloud SQL: un socket en
# /cloudsql/<proyecto>:<región>:<instancia>, sin host de red en la URL.
CLOUD_SQL_SOCKET = "/cloudsql/proyecto-pose:us-central1:pose-db"
CLOUD_SQL_URL = f"postgresql://pose:clave@/pose_service?host={CLOUD_SQL_SOCKET}"


def test_production_accepts_a_unix_socket_without_tls():
    """El socket no sale de la máquina; el proxy de Cloud SQL cifra el resto."""
    config = _settings(ENVIRONMENT="production", DATABASE_URL=CLOUD_SQL_URL)

    assert config.database_url == f"postgresql+asyncpg://pose:clave@/pose_service?host={CLOUD_SQL_SOCKET}"


def test_the_socket_rule_does_not_depend_on_the_platform(monkeypatch):
    """Es común: vale igual en Render que en Google, sin preguntarle a la plataforma."""
    from app import platforms

    def no_platform():
        raise AssertionError("un socket no debería consultar a la plataforma")

    monkeypatch.setattr(platforms, "current", no_platform)
    config = _settings(ENVIRONMENT="production", DATABASE_URL=CLOUD_SQL_URL)

    assert config.database_url.endswith(f"host={CLOUD_SQL_SOCKET}")


@pytest.mark.parametrize(
    "url",
    [
        # Un host de red en la URL manda, aunque además haya un socket.
        f"postgresql://pose:clave@db.example.com/pose_service?host={CLOUD_SQL_SOCKET}",
        # `host` que no es una ruta: es un host de red.
        "postgresql://pose:clave@/pose_service?host=db.example.com",
        # Una lista que mezcla un socket con un host de red.
        f"postgresql://pose:clave@/pose_service?host={CLOUD_SQL_SOCKET},db.example.com",
        # Sin host de ningún tipo: asyncpg elegiría por su cuenta.
        "postgresql://pose:clave@/pose_service",
    ],
)
def test_production_still_requires_tls_unless_it_is_only_a_socket(url):
    config = _settings(ENVIRONMENT="production", DATABASE_URL=url)

    with pytest.raises(ValueError, match="exige TLS"):
        _ = config.database_url


def test_the_socket_path_reaches_asyncpg_intact():
    """
    La ruta lleva dos puntos (proyecto:región:instancia), que SQLAlchemy usa
    para separar host y puerto. Con un solo `host` que es una ruta, la deja
    entera; si la partiera, asyncpg buscaría un socket que no existe.
    """
    config = _settings(ENVIRONMENT="production", DATABASE_URL=CLOUD_SQL_URL)
    engine = create_async_engine(config.database_url)
    _args, kwargs = engine.dialect.create_connect_args(engine.url)

    assert kwargs["host"] == CLOUD_SQL_SOCKET
    assert kwargs.get("port") is None
    assert (kwargs["user"], kwargs["password"], kwargs["database"]) == ("pose", "clave", "pose_service")
    assert "ssl" not in kwargs


def test_the_socket_path_survives_the_sslmode_translation():
    """Traducir sslmode re-codifica la consulta (%2F, %3A): la ruta tiene que volver igual."""
    config = _settings(DATABASE_URL=f"{CLOUD_SQL_URL}&sslmode=disable")
    engine = create_async_engine(config.database_url)
    _args, kwargs = engine.dialect.create_connect_args(engine.url)

    assert kwargs["host"] == CLOUD_SQL_SOCKET
