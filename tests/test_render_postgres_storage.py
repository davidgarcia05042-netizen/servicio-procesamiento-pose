"""
Los videos en Render Postgres (app/platforms/render/postgres_storage.py) y el
ciclo del job que los rodea, contra un PostgreSQL REAL.

Corren si POSTGRES_URL_FOR_TESTS apunta a un PostgreSQL al que se pueda entrar
como administrador (ej. postgresql://postgres:postgres@localhost:5432/postgres):
las migraciones corren una vez sobre una base plantilla, y cada test trabaja
sobre una copia que se borra al final.
El workflow de tests lo levanta; en local, con Docker:

    docker run -d --rm --name pg-pruebas -e POSTGRES_PASSWORD=postgres -p 127.0.0.1:5432:5432 postgres:16-alpine
    $env:POSTGRES_URL_FOR_TESTS = "postgresql://postgres:postgres@localhost:5432/postgres"

Sin la variable se saltan. Lo que no necesita una base (el contrato del
adaptador, la elección por STORAGE_BACKEND) está en test_video_storage.py y
test_render_storage.py.
"""

import asyncio
import os
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit, urlunsplit

import pytest

from app.constants.job_constants import CallbackStatus, JobStatus
from app.core.config import settings

ROOT = Path(__file__).resolve().parent.parent
ADMIN_URL = os.environ.get("POSTGRES_URL_FOR_TESTS", "").strip()

ANALYSIS = {"points": [{"frame": 0, "angle": 91.5}], "max_angle": 91.5, "min_angle": 91.5}


def _with_database(url: str, name: str, driver: str = "postgresql") -> str:
    parts = urlsplit(url)
    return urlunsplit(parts._replace(scheme=driver, path=f"/{name}"))


async def _admin(sql: str) -> None:
    import asyncpg

    conn = await asyncpg.connect(ADMIN_URL)
    try:
        await conn.execute(sql)
    finally:
        await conn.close()


@pytest.fixture(scope="session")
def migrated_template():
    """
    Una base con las migraciones corridas, UNA vez por sesión: correr alembic
    en un proceso aparte tarda segundos, y cada test copia esta base en vez de
    migrar la suya.
    """
    if not ADMIN_URL:
        pytest.skip("POSTGRES_URL_FOR_TESTS no está definido (ver el docstring del módulo)")

    name = f"plantilla_{uuid.uuid4().hex[:12]}"
    asyncio.run(_admin(f'CREATE DATABASE "{name}"'))
    try:
        env = {**os.environ, "DATABASE_URL": _with_database(ADMIN_URL, name), "ENVIRONMENT": "development"}
        migrated = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=ROOT, env=env, capture_output=True, text=True,
        )
        assert migrated.returncode == 0, migrated.stderr[-2000:]
        yield name
    finally:
        asyncio.run(_admin(f'DROP DATABASE "{name}" WITH (FORCE)'))


@pytest.fixture
def database_url(migrated_template):
    """Una base nueva por test, copia de la migrada. Devuelve su URL (con asyncpg)."""
    name = f"pruebas_{uuid.uuid4().hex[:12]}"
    asyncio.run(_admin(f'CREATE DATABASE "{name}" TEMPLATE "{migrated_template}"'))
    try:
        yield _with_database(ADMIN_URL, name, driver="postgresql+asyncpg")
    finally:
        asyncio.run(_admin(f'DROP DATABASE "{name}" WITH (FORCE)'))


@pytest.fixture
async def connected(database_url, monkeypatch):
    """El servicio conectado a esa base, con el video guardado en ella."""
    from app.platforms.render.config import render_settings
    from app.utils import db_connect

    monkeypatch.setattr(settings, "DATABASE_URL", database_url)
    monkeypatch.setattr(settings, "PLATFORM", "render")
    monkeypatch.setattr(render_settings, "STORAGE_BACKEND", "postgres")
    monkeypatch.setattr(db_connect, "database", db_connect.Database())

    await db_connect.database.connect()
    yield db_connect
    await db_connect.database.disconnect()


async def _scalar(db_connect, sql: str, **params):
    from sqlalchemy import text

    async with db_connect.database.session() as session:
        value = await session.scalar(text(sql), params)
        await session.commit()
        return value


# --- El almacenamiento ---


async def test_a_video_survives_upload_download_and_delete(connected, tmp_path):
    from app.platforms.render import postgres_storage

    # Del tamaño de un video real (MAX_VIDEO_SIZE_MB es 50 en Render).
    content = b"\x00\x00\x00\x18ftypmp42" + os.urandom(40 * 1024 * 1024)
    path = "videos/0123456789abcdef0123456789abcdef.mp4"

    await postgres_storage.upload_video(content, path, "video/mp4")
    destination = tmp_path / "bajado.mp4"
    await postgres_storage.download_video(path, str(destination))

    assert destination.read_bytes() == content
    assert await postgres_storage.delete_video(path) is True
    assert await postgres_storage.delete_video(path) is False
    assert await _scalar(connected, "SELECT count(*) FROM videos") == 0


async def test_downloading_a_missing_video_raises(connected, tmp_path):
    from app.platforms.render import postgres_storage

    with pytest.raises(FileNotFoundError, match="no está"):
        await postgres_storage.download_video("videos/nada.mp4", str(tmp_path / "x.mp4"))
    assert not (tmp_path / "x.mp4").exists()


async def test_the_content_column_is_stored_without_compression(connected):
    """'e' = EXTERNAL: fuera de la fila y sin intentar comprimir (ver 0005_videos_table)."""
    storage = await _scalar(
        connected,
        "SELECT attstorage::text FROM pg_attribute WHERE attrelid = 'videos'::regclass AND attname = 'content'",
    )
    assert storage == "e"


async def test_the_port_reaches_the_database_through_the_render_platform(connected, tmp_path):
    """De punta a punta por el puerto común, como lo usan la API y el worker."""
    from app.utils import video_storage

    path = video_storage.new_video_path("marcha.mp4")
    await video_storage.upload_video(b"\x00\x00\x00\x18ftypmp42datos", path, "video/mp4")
    await video_storage.download_video(path, str(tmp_path / "v.mp4"))

    assert (tmp_path / "v.mp4").read_bytes() == b"\x00\x00\x00\x18ftypmp42datos"
    assert await video_storage.delete_video(path) is True


# --- El ciclo del job alrededor del video ---


async def _new_job(db_connect, path: str) -> str:
    from app.platforms.render import postgres_storage

    job_id = str(uuid.uuid4())
    await db_connect.insert_job(
        job_id, "marcha", "https://plataforma.example/r", "mediapipe_heavy",
        video_url=path, results={"requested_points": [24, 26, 28]},
    )
    await postgres_storage.upload_video(b"\x00\x00\x00\x18ftypmp42datos", path, "video/mp4")
    return job_id


async def test_an_open_job_is_updated_and_a_closed_one_is_not(connected):
    job_id = await _new_job(connected, "videos/a.mp4")

    assert await connected.update_open_job(job_id, status=JobStatus.PROCESSING.value) is True

    cutoff = datetime.now(timezone.utc) + timedelta(minutes=1)
    await connected.fail_stuck_jobs(JobStatus.PROCESSING.value, cutoff, "processing_timeout")

    assert await connected.update_open_job(job_id, status=JobStatus.DONE.value) is False
    job = await connected.get_job(job_id)
    assert (job.status, job.error) == (JobStatus.FAILED.value, "processing_timeout")


async def test_a_worker_that_finishes_after_the_sweep_does_not_overwrite_it(connected):
    """
    El caso completo: el worker toma el job y, mientras analiza, el barrido lo
    cierra como FAILED (y encola el aviso de fallo). Al terminar, el worker no
    puede dejarlo en DONE ni encolar un aviso de éxito. El video se borra.
    """
    from app.core import video_job

    job_id = await _new_job(connected, "videos/b.mp4")

    def analysis_during_which_the_sweep_runs(path, points):
        # El barrido corre en otro proceso; acá, en un hilo con su propio loop
        # y su propia conexión.
        import asyncpg

        async def sweep():
            conn = await asyncpg.connect(settings.DATABASE_URL.replace("+asyncpg", ""))
            try:
                await conn.execute(
                    "UPDATE jobs SET status = 'failed', error = 'processing_timeout', "
                    "callback_status = 'pending' WHERE id = $1 AND status = 'processing'",
                    job_id,
                )
            finally:
                await conn.close()

        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(1) as pool:
            pool.submit(asyncio.run, sweep()).result()
        return ANALYSIS

    with (
        patch.object(video_job, "analyze_video", side_effect=analysis_during_which_the_sweep_runs),
        patch.object(video_job, "enqueue_callback") as callback,
    ):
        result = await video_job.process_video_job(job_id)

    job = await connected.get_job(job_id)
    assert result == "closed_elsewhere"
    assert (job.status, job.error, job.callback_status) == (
        JobStatus.FAILED.value, "processing_timeout", CallbackStatus.PENDING.value,
    )
    assert job.max_angle is None
    callback.assert_not_called()
    assert await _scalar(connected, "SELECT count(*) FROM videos") == 0


async def test_the_sweep_deletes_the_video_of_the_job_it_closes(connected):
    from app.core import stuck_jobs

    job_id = await _new_job(connected, "videos/c.mp4")
    # Encolado hace un día y nunca tomado: el barrido lo da por perdido.
    await _scalar(
        connected,
        "UPDATE jobs SET updated_at = :when WHERE id = :id RETURNING id",
        when=datetime.now(timezone.utc) - timedelta(days=1),
        id=job_id,
    )

    with patch.object(stuck_jobs, "enqueue_callback"):
        closed = await stuck_jobs.sweep_stuck_jobs()

    assert closed == [job_id]
    assert await _scalar(connected, "SELECT count(*) FROM videos") == 0


async def test_a_job_created_without_results_keeps_sql_null(connected):
    """
    Pasarle `results=None` al modelo guardaría el `null` de JSON, que no es el
    NULL de SQL: `results IS NOT NULL` sería cierto y la retención volvería a
    seleccionar la fila en cada corrida (ver `anonymize_jobs`).
    """
    await connected.insert_job("job-sin-resultados", "marcha", "https://plataforma.example/r")

    assert await _scalar(connected, "SELECT results IS NULL FROM jobs WHERE id = 'job-sin-resultados'") is True
