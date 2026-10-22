"""
Firebase Storage (reemplaza el s3_utils.py del repo de referencia). Uno de los
dos almacenamientos de Render; se elige con STORAGE_BACKEND=firebase.

Implementa el contrato de almacenamiento de app/platforms/__init__.py: la ruta
la decide app/utils/video_storage.py.

El SDK de firebase-admin es SÍNCRONO y hace I/O de red bloqueante, así que
las funciones que se llaman desde código async lo corren en un threadpool
(`asyncio.to_thread`) para no bloquear el event loop -- ni el de uvicorn en la
API, ni el loop persistente del worker de Celery.
"""

import asyncio
import threading

import firebase_admin
from firebase_admin import credentials, storage
from google.cloud.exceptions import NotFound

from app.platforms.render.config import render_settings
from app.utils.logger import get_logger

logger = get_logger("firebase_storage")

_init_lock = threading.Lock()


_emulator_bucket = None


def ensure_configured() -> None:
    """
    No comprueba nada al arrancar: la credencial de Firebase es un archivo que
    se lee al primer uso, y ese camino ya existía antes del contrato.
    """


def _bucket():
    """Inicializa la app de Firebase una sola vez por proceso y devuelve el bucket."""
    if render_settings.STORAGE_EMULATOR_HOST:
        return _local_emulator_bucket()

    with _init_lock:
        if not firebase_admin._apps:
            cred = credentials.Certificate(render_settings.FIREBASE_CREDENTIALS_PATH)
            bucket = render_settings.FIREBASE_STORAGE_BUCKET
            firebase_admin.initialize_app(cred, {"storageBucket": bucket})
            logger.info("Firebase inicializado (bucket: %s)", bucket)
    return storage.bucket()


def _local_emulator_bucket():
    """
    El bucket en el emulador local (ver STORAGE_EMULATOR_HOST en config.py).

    Cliente de Google Cloud Storage con credenciales ANÓNIMAS, no el de
    firebase-admin: ese le pasa sus credenciales al cliente, y el cliente pide
    un token a Google antes de cada llamada, cosa que con el emulador no tiene
    sentido y sin credenciales reales falla. El resto del módulo no se entera:
    recibe un bucket con la misma interfaz.

    El bucket se crea si no existe porque el emulador arranca vacío en cada
    demo.
    """
    global _emulator_bucket
    with _init_lock:
        if _emulator_bucket is None:
            from google.auth.credentials import AnonymousCredentials
            from google.cloud import storage as gcs

            client = gcs.Client(
                project="demo-local",
                credentials=AnonymousCredentials(),
                client_options={"api_endpoint": render_settings.STORAGE_EMULATOR_HOST},
            )
            name = render_settings.FIREBASE_STORAGE_BUCKET or "pose-demo"
            _emulator_bucket = client.lookup_bucket(name) or client.create_bucket(name)
            logger.warning(
                "Almacenamiento en EMULADOR LOCAL (%s, bucket %s): solo para la demo",
                render_settings.STORAGE_EMULATOR_HOST,
                name,
            )
    return _emulator_bucket


def _upload_bytes_sync(content: bytes, blob_path: str, content_type: str) -> None:
    _bucket().blob(blob_path).upload_from_string(content, content_type=content_type)


def _download_to_path_sync(blob_path: str, destination: str) -> None:
    _bucket().blob(blob_path).download_to_filename(destination)


async def upload_video(content: bytes, blob_path: str, content_type: str) -> None:
    """Sube el video ya validado en memoria a `blob_path`."""
    await asyncio.to_thread(_upload_bytes_sync, content, blob_path, content_type)
    logger.info("Video subido a Firebase: %s", blob_path)


async def download_video(blob_path: str, destination: str) -> None:
    await asyncio.to_thread(_download_to_path_sync, blob_path, destination)
    logger.info("Video descargado de Firebase: %s -> %s", blob_path, destination)


def _delete_sync(blob_path: str) -> bool:
    """
    Borra el blob. Devuelve False si ya no estaba, sin tratarlo como error.

    Que no esté es un desenlace NORMAL, no un fallo: el barrido de retención
    puede pasar por un video que el worker ya borró al cerrar el job, y dos
    intentos sobre el mismo blob no son un problema que nadie deba investigar.
    """
    blob = _bucket().blob(blob_path)
    try:
        blob.delete()
        return True
    except NotFound:
        return False


async def delete_video(blob_path: str) -> bool:
    """
    Borra el video del almacenamiento externo.

    POR QUÉ SE BORRA (Ley 1581 de 2012): el video de una persona haciendo un
    ejercicio es un dato sensible -- de salud y biométrico a la vez -- y el
    principio de temporalidad prohíbe conservarlo más allá de la finalidad que
    justificó recogerlo. Esa finalidad es calcular los ángulos articulares; una
    vez calculados y guardados, el video no tiene ningún uso posterior acá.

    Este servicio tampoco es el sistema de registro del video: la plataforma de
    telefisioterapia lo envió y lo conserva. Nuestra copia existe solo para
    procesarlo.
    """
    deleted = await asyncio.to_thread(_delete_sync, blob_path)
    if deleted:
        logger.info("Video eliminado de Firebase: %s", blob_path)
    else:
        logger.info("El video ya no estaba en Firebase: %s", blob_path)
    return deleted
