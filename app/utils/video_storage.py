"""
El puerto del almacenamiento: dónde espera el video hasta que lo procesa el
worker.

Es el único módulo que importan quienes suben, descargan o borran videos (la
API, el worker y los barridos). DÓNDE se guarda lo decide la plataforma
activa, con su `video_storage()` (ver app/platforms/__init__.py): en Render, la
propia base, Supabase Storage o Firebase según STORAGE_BACKEND; en Google
Cloud, Cloud Storage.

Lo que SÍ es común vive acá: el nombre con el que se guarda cada video. Los
adaptadores de cada plataforma solo guardan, bajan y borran bytes en la ruta
que se les da, así que dos plataformas no pueden terminar nombrando distinto
el mismo video.
"""

import re
import uuid
from types import ModuleType

from app import platforms

# La extensión que se conserva del nombre que mandó el cliente: corta y sin
# nada más que letras y números.
_EXTENSION = re.compile(r"\.[A-Za-z0-9]{1,5}$")


def _backend() -> ModuleType:
    return platforms.current().video_storage()


def ensure_configured() -> None:
    """
    Falla al arrancar si el almacenamiento de la plataforma no está
    configurado.

    Lo llaman la API y el worker al iniciar, que son los que suben, bajan y
    borran videos. Sin esto, una clave o un bucket faltante fallaría recién con
    el primer video: la API aceptaría la solicitud y la subida reventaría.
    """
    _backend().ensure_configured()


def new_video_path(filename: str) -> str:
    """
    La ruta donde se guarda un video nuevo: `videos/<uuid><extensión>`.

    DEL NOMBRE QUE MANDÓ EL CLIENTE SOLO QUEDA LA EXTENSIÓN. Antes la ruta lo
    conservaba "por legibilidad", y un nombre de archivo puede identificar al
    paciente (el video de prueba del equipo lleva el nombre de una persona).
    Por la ruta, el nombre terminaba en el log de cada subida, descarga y
    borrado, en `jobs.video_url` durante todo el plazo de retención y en el
    archivo temporal del worker. Es la misma razón por la que el log no
    escribe el `resultsEndpoint` completo (ver `endpoint_origin` en
    app/utils/request_validations.py).

    La extensión sí se conserva: OpenCV elige el demuxer en parte por ella, y
    el worker abre el video con el nombre de la ruta (ver app/core/video_job.py).
    Sin una extensión reconocible, `.mp4`.

    Se guarda la RUTA, no una URL firmada: las URLs firmadas expiran y el worker
    puede tomar el trabajo mucho después de que se subió el archivo.
    """
    extension = _EXTENSION.search(filename or "")
    suffix = extension.group(0).lower() if extension else ".mp4"
    return f"videos/{uuid.uuid4().hex}{suffix}"


async def upload_video(content: bytes, path: str, content_type: str) -> None:
    """
    Sube el video ya validado a `path`, que sale de `new_video_path`.

    La ruta la decide quien sube, ANTES de subir: así queda escrita en el job
    junto con su creación, y no hay un instante en que el video exista en el
    almacenamiento sin una fila que lo nombre (ver
    app/core/video_processing_service.py).
    """
    await _backend().upload_video(content, path, content_type)


async def download_video(path: str, destination: str) -> None:
    await _backend().download_video(path, destination)


async def delete_video(path: str) -> bool:
    """Borra el video. False si ya no estaba (no es un error)."""
    return await _backend().delete_video(path)
