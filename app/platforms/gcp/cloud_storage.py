"""
Google Cloud Storage como almacenamiento del video (plataforma gcp).

Implementa el contrato de almacenamiento de app/platforms/__init__.py: la ruta
la decide app/utils/video_storage.py.

CREDENCIALES: ninguna en el código ni en variables de entorno. En Cloud Run el
cliente usa la cuenta de servicio del servicio (ver config.py). Esa cuenta
necesita sobre el bucket el rol `roles/storage.objectUser` (crear, leer y
borrar objetos) y nada más: no administra el bucket. Por eso el cliente se arma
con `client.bucket(nombre)`, que no hace ninguna llamada, y no con
`get_bucket`, que pediría el permiso de leer la configuración del bucket.

EL SDK ES SÍNCRONO (usa requests por debajo), así que cada llamada va por
`asyncio.to_thread`, igual que Firebase en Render: no puede bloquear ni el
event loop de uvicorn ni el del worker.

REINTENTOS: los pone la librería. Desde la versión 3 reintenta subidas,
descargas y borrados ante errores transitorios (429, 5xx, conexión cortada).
La subida no lleva la precondición "solo si no existe" a propósito: con
reintentos automáticos, si el primer intento llega y se pierde la respuesta,
el reintento chocaría con su propio archivo y fallaría (HTTP 412). La ruta ya
es única, así que reescribir los mismos bytes es inocuo.

LEY 1581: el worker borra el video al cerrar cada job y el barrido de
retención cubre lo que quede. Como red de seguridad, el bucket lleva además
una regla de ciclo de vida que borra cualquier objeto con más de unos días (se
configura al crear el bucket; ver README).

EMULADOR: con STORAGE_EMULATOR_HOST (tests y desarrollo local), cliente
anónimo contra fake-gcs-server, y el bucket se crea si no existe porque el
emulador arranca vacío. En producción, prohibido (ver config.py).
"""

import asyncio
import threading

from google.cloud import storage
from google.cloud.exceptions import NotFound

from app.platforms.gcp.config import gcp_settings
from app.utils.logger import get_logger

logger = get_logger("cloud_storage")

# (conectar, leer) en segundos, por solicitud HTTP. Subir o bajar un video de
# cientos de MB puede tardar; conectar no debería.
_TIMEOUT = (10, 300)

_init_lock = threading.Lock()
_bucket = None


def ensure_configured() -> None:
    """Falla al arrancar si no hay bucket: no hace llamadas de red."""
    if not gcp_settings.GCS_BUCKET.strip():
        raise RuntimeError(
            "La plataforma gcp exige GCS_BUCKET: el bucket de Cloud Storage "
            "donde esperan los videos."
        )


def _get_bucket() -> storage.Bucket:
    """El bucket, con un cliente por proceso (se arma al primer uso)."""
    global _bucket
    with _init_lock:
        if _bucket is None:
            _bucket = _emulator_bucket() if gcp_settings.STORAGE_EMULATOR_HOST else _real_bucket()
    return _bucket


def _real_bucket() -> storage.Bucket:
    # Sin argumentos: credenciales y proyecto salen del entorno (la cuenta de
    # servicio en Cloud Run, `gcloud auth application-default login` en local).
    bucket = storage.Client().bucket(gcp_settings.GCS_BUCKET)
    logger.info("Cloud Storage listo (bucket: %s)", gcp_settings.GCS_BUCKET)
    return bucket


def _emulator_bucket() -> storage.Bucket:
    from google.auth.credentials import AnonymousCredentials

    client = storage.Client(
        project="local",
        credentials=AnonymousCredentials(),
        client_options={"api_endpoint": gcp_settings.STORAGE_EMULATOR_HOST},
    )
    name = gcp_settings.GCS_BUCKET or "pose-local"
    bucket = client.lookup_bucket(name) or client.create_bucket(name)
    logger.warning(
        "Almacenamiento en EMULADOR LOCAL (%s, bucket %s): solo para tests y desarrollo",
        gcp_settings.STORAGE_EMULATOR_HOST,
        name,
    )
    return bucket


def _upload_sync(content: bytes, path: str, content_type: str) -> None:
    _get_bucket().blob(path).upload_from_string(content, content_type=content_type, timeout=_TIMEOUT)


def _download_sync(path: str, destination: str) -> None:
    # Si el objeto no existe, la librería lanza NotFound y borra el archivo a
    # medio crear: no queda un video vacío en el disco del worker.
    _get_bucket().blob(path).download_to_filename(destination, timeout=_TIMEOUT)


def _delete_sync(path: str) -> bool:
    try:
        _get_bucket().blob(path).delete(timeout=_TIMEOUT)
        return True
    except NotFound:
        return False


async def upload_video(content: bytes, path: str, content_type: str) -> None:
    """Sube el video ya validado en memoria a `path`."""
    await asyncio.to_thread(_upload_sync, content, path, content_type)
    logger.info("Video subido a Cloud Storage: %s", path)


async def download_video(path: str, destination: str) -> None:
    await asyncio.to_thread(_download_sync, path, destination)
    logger.info("Video descargado de Cloud Storage: %s -> %s", path, destination)


async def delete_video(path: str) -> bool:
    """
    Borra el video. Devuelve False si ya no estaba, sin tratarlo como error:
    el barrido de retención puede pasar por un video que el worker ya borró
    al cerrar el job.
    """
    deleted = await asyncio.to_thread(_delete_sync, path)
    if deleted:
        logger.info("Video eliminado de Cloud Storage: %s", path)
    else:
        logger.info("El video ya no estaba en Cloud Storage: %s", path)
    return deleted
