"""
La base en Google Cloud: Cloud SQL para PostgreSQL, que Cloud Run entrega
como un socket Unix en /cloudsql/<proyecto>:<región>:<instancia>.

Dos niveles:

  - Sin red: la regla de la plataforma gcp (ningún host de red se exime de
    TLS). La regla común del socket está en test_database_config.py.
  - Contra un PostgreSQL REAL escuchando en un socket con esa misma forma de
    ruta (dos puntos incluidos): las migraciones y la lectura y escritura de
    jobs, en modo producción. Corren si CLOUDSQL_SOCKET_DIR_FOR_TESTS apunta
    al directorio que hace de /cloudsql; el workflow de tests lo levanta.
    asyncio no tiene sockets Unix en Windows, así que en local corren dentro
    de Linux (ver README, "La base en Cloud SQL").

Que la instancia REAL acepte la conexión lo comprueba `alembic upgrade head`
por el proxy de Cloud SQL (ver README).
"""

import asyncio
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from app.core.config import settings
from app.platforms import gcp

ROOT = Path(__file__).resolve().parent.parent
SOCKET_ROOT = os.environ.get("CLOUDSQL_SOCKET_DIR_FOR_TESTS", "").strip()

# El nombre de conexión de una instancia de Cloud SQL: proyecto:región:instancia.
CONNECTION_NAME = "proyecto-pruebas:us-central1:pose-db"


@pytest.mark.parametrize(
    "hostname",
    [None, "", "10.20.0.3", "localhost", "dpg-cl4bq5ij1k6c73a1b2c0", "db.example.com"],
)
def test_no_network_host_skips_tls_on_google_cloud(hostname):
    """
    En particular "dpg-...": la red privada de Render no significa nada acá.
    Y una IP interna tampoco: el servicio no usa VPC.
    """
    assert gcp.is_private_network_host(hostname) is False


# --- Contra PostgreSQL por socket ---


def _socket_dir() -> str:
    return f"{SOCKET_ROOT.rstrip('/')}/{CONNECTION_NAME}"


async def _admin(sql: str) -> None:
    import asyncpg

    conn = await asyncpg.connect(
        host=_socket_dir(), user="postgres", password="postgres", database="postgres"
    )
    try:
        await conn.execute(sql)
    finally:
        await conn.close()


@pytest.fixture
def socket_database_url():
    """Una base nueva por test, en el PostgreSQL del socket, y la URL hacia ella."""
    if not SOCKET_ROOT:
        pytest.skip("CLOUDSQL_SOCKET_DIR_FOR_TESTS no está definido (ver el docstring del módulo)")
    if sys.platform == "win32":
        pytest.skip("asyncio no tiene sockets Unix en Windows")

    name = f"pruebas_{uuid.uuid4().hex[:12]}"
    asyncio.run(_admin(f'CREATE DATABASE "{name}"'))
    yield f"postgresql://postgres:postgres@/{name}?host={_socket_dir()}"
    asyncio.run(_admin(f'DROP DATABASE "{name}" WITH (FORCE)'))


def _migrate(url: str) -> subprocess.CompletedProcess:
    """`alembic upgrade head` como lo correría el despliegue: en producción."""
    env = {**os.environ, "DATABASE_URL": url, "ENVIRONMENT": "production"}
    return subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT, env=env, capture_output=True, text=True,
    )


def test_the_migrations_run_through_the_socket_in_production(socket_database_url):
    result = _migrate(socket_database_url)

    assert result.returncode == 0, result.stderr[-2000:]


async def test_the_service_writes_and_reads_jobs_through_the_socket(socket_database_url, monkeypatch):
    from app.utils import db_connect

    assert _migrate(socket_database_url).returncode == 0
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "DATABASE_URL", "postgresql+asyncpg" + socket_database_url[len("postgresql"):])
    monkeypatch.setattr(db_connect, "database", db_connect.Database())

    await db_connect.database.connect()
    try:
        await db_connect.insert_job("job-socket", "marcha", "https://plataforma.example/r", "mediapipe_heavy")
        job = await db_connect.get_job("job-socket")
    finally:
        await db_connect.database.disconnect()

    assert (job.status, job.exercise) == ("queued", "marcha")
