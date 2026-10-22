"""
Render Postgres como almacenamiento del video (STORAGE_BACKEND=postgres).

POR QUÉ: el 2026-10-08 el grupo de investigación decidió llevar todo a Render,
incluidos los videos. Render no tiene un almacenamiento de objetos estable (el
suyo está en early access y se autentica con una clave de API de toda la
cuenta), y un disco persistente lo ve un solo servicio: la API y el worker no
podrían compartirlo, y un servicio con disco no escala ni se despliega sin
cortarse. La base, en cambio, ya la comparten los dos por la red privada.

POR QUÉ FUNCIONA ACÁ, aunque en general no se guardan archivos en una base:
cada video pesa como mucho MAX_VIDEO_SIZE_MB, espera alrededor de un minuto y
se borra apenas el worker cierra el job (ver app/core/video_job.py). La tabla
nunca guarda más que los videos en cola. Medido el 2026-10-08 contra
PostgreSQL 16: 50 MB se guardan en ~1 s y se leen en ~0,25 s, y el espacio de
un video borrado lo libera el autovacuum en segundos.

LEY 1581 -- LO QUE QUEDA DESPUÉS DE BORRAR: para el servicio, el video
desaparece al instante. Pero PostgreSQL anota cada escritura en su registro de
cambios (WAL), y Render lo guarda para poder restaurar la base a cualquier
momento: 3 días con cuenta Hobby, 7 con Pro. Mientras dure ese plazo, el video
se puede reconstruir restaurando la base. Está cifrado (AES-256) y hace falta
entrar a la cuenta de Render. Ver README, "Los videos en Render Postgres".

Usa la misma conexión que los jobs (app/utils/db_connect.py), que la API y el
worker abren al arrancar. No hay nada propio que configurar.
"""

import asyncio
from pathlib import Path

from sqlalchemy import delete, insert, select

from app.utils import db_connect
from app.utils.db_connect import StoredVideo
from app.utils.logger import get_logger

logger = get_logger("postgres_storage")

# Para quien use este almacenamiento fuera de la API y el worker (que conectan
# la base al arrancar): app/tools/storage_check.py la conecta si ve esto.
USES_DATABASE = True


def ensure_configured() -> None:
    """
    Nada propio que comprobar: usa la misma base que los jobs, y esa se
    configura y se valida en app/core/config.py.
    """


async def upload_video(content: bytes, path: str, content_type: str) -> None:  # noqa: ARG001
    """
    Guarda el video ya validado en `path`. El tipo no se guarda: el worker
    solo necesita los bytes, y la extensión va en la ruta.

    Una sentencia `insert` y no un objeto del ORM: la sesión no se queda con
    una copia de los bytes en su mapa de identidad.
    """
    async with db_connect.database.session() as session:
        await session.execute(insert(StoredVideo).values(path=path, content=content))
        await session.commit()
    logger.info("Video guardado en PostgreSQL: %s (%d bytes)", path, len(content))


async def download_video(path: str, destination: str) -> None:
    """Lo escribe en `destination`. Lanza FileNotFoundError si no está."""
    async with db_connect.database.session() as session:
        content = await session.scalar(select(StoredVideo.content).where(StoredVideo.path == path))
    if content is None:
        raise FileNotFoundError(f"El video no está en PostgreSQL: {path}")

    await asyncio.to_thread(Path(destination).write_bytes, content)
    logger.info("Video leído de PostgreSQL: %s -> %s", path, destination)


async def delete_video(path: str) -> bool:
    """
    Borra el video. Devuelve False si ya no estaba, sin tratarlo como error: el
    barrido de retención puede pasar por un video que el worker ya borró.
    """
    async with db_connect.database.session() as session:
        result = await session.execute(
            delete(StoredVideo).where(StoredVideo.path == path).returning(StoredVideo.path)
        )
        deleted = result.first() is not None
        await session.commit()

    if deleted:
        logger.info("Video eliminado de PostgreSQL: %s", path)
    else:
        logger.info("El video ya no estaba en PostgreSQL: %s", path)
    return deleted
