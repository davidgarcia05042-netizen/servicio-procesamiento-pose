"""
Cloudflare R2 como almacenamiento del video (plataforma Render).

POR QUÉ EXISTE (2026-10-22): el plan gratuito de Supabase ofrece 1 GB de
almacenamiento y PAUSA el proyecto tras una semana sin actividad. Si el grupo
deja de usar la plataforma unos días, el bucket deja de responder y ningún
video nuevo se procesa hasta que alguien reactive el proyecto a mano. R2 da
10 GB gratuitos, no se pausa y no cobra la salida de datos. La plataforma de
telefisioterapia ya guarda sus videos en R2 (bucket `telerehab-videos`);
este servicio usa un bucket PROPIO y PRIVADO (`pose-videos`), con un token
que solo tiene permiso sobre ese bucket.

Se elige con STORAGE_BACKEND=r2 (ver `video_storage()` en __init__.py).
Supabase, la base de Render y Firebase siguen disponibles en el código.

Implementa el contrato de almacenamiento de app/platforms/__init__.py: la ruta
la decide app/utils/video_storage.py.

R2 HABLA LA API DE S3, así que se usa boto3 contra el endpoint de la cuenta
(https://<cuenta>.r2.cloudflarestorage.com, región "auto"). EL SDK ES
SÍNCRONO, así que cada llamada va por `asyncio.to_thread`, igual que Firebase
y Cloud Storage: no puede bloquear ni el event loop de uvicorn ni el del
worker (ver docs/DECISIONES_TECNICAS.md, "event loop persistente").

SIN PRECONDICIÓN "SOLO SI NO EXISTE" en la subida, por la misma razón que en
Cloud Storage: boto3 reintenta solo, y si el primer intento llega y se pierde
la respuesta, el reintento chocaría con su propio archivo. La ruta ya es
única (un uuid), así que reescribir los mismos bytes es inocuo.

BORRAR EN S3 NO DICE SI EL OBJETO EXISTÍA (DeleteObject responde 204 siempre).
El contrato pide saberlo, así que primero se consulta con HeadObject. Entre
las dos llamadas no hay carrera que importe: el único que borra es el worker
al cerrar el job, y el barrido de retención para lo que haya quedado.

LEY 1581: el worker borra el video al cerrar cada job. Como red de seguridad,
el bucket lleva una regla de ciclo de vida que borra cualquier objeto con más
de unos días (se configura en el dashboard de Cloudflare; ver README). El
bucket NO tiene acceso público: solo entra quien tenga el token.
"""

import asyncio
import re

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.platforms.render.config import render_settings
from app.utils.logger import get_logger

logger = get_logger("r2_storage")

# Conectar no debería tardar; subir o bajar un video de 50 MB, sí.
_CONFIG = Config(
    signature_version="s3v4",
    connect_timeout=10,
    read_timeout=300,
    retries={"max_attempts": 3, "mode": "standard"},
)

# Descarga por partes de 1 MB: el worker nunca tiene el video entero en memoria.
_CHUNK = 1024 * 1024

# Formas de los identificadores de Cloudflare. Comprobarlas al arrancar ataja
# el error más común al copiar el token: pegar el Secret (64) donde va el
# Access Key ID (32), o al revés. R2 lo rechaza con un mensaje que no nombra
# la variable, y solo se nota con el primer video.
_ACCOUNT_ID = re.compile(r"^[0-9a-f]{32}$")
_ACCESS_KEY_LENGTH = 32
_SECRET_KEY_LENGTH = 64


class StorageError(RuntimeError):
    """R2 respondió con un error (sin credenciales en el mensaje)."""


def ensure_configured() -> None:
    """
    Falla al arrancar si falta algo o tiene una forma imposible. No hace
    llamadas de red. Los valores secretos nunca aparecen en el mensaje: solo
    el nombre de la variable y, a lo sumo, su longitud.
    """
    account = render_settings.R2_ACCOUNT_ID.strip()
    if not _ACCOUNT_ID.match(account):
        raise RuntimeError(
            "STORAGE_BACKEND=r2 exige R2_ACCOUNT_ID: el Account ID de Cloudflare "
            "(32 caracteres hexadecimales)."
        )
    if not render_settings.R2_BUCKET.strip():
        raise RuntimeError("STORAGE_BACKEND=r2 exige R2_BUCKET (el bucket privado de los videos).")

    access = render_settings.R2_ACCESS_KEY_ID.strip()
    secret = render_settings.R2_SECRET_ACCESS_KEY.strip()
    if len(access) != _ACCESS_KEY_LENGTH:
        raise RuntimeError(
            f"R2_ACCESS_KEY_ID debe tener {_ACCESS_KEY_LENGTH} caracteres y tiene {len(access)}. "
            "¿Se pegó el Secret Access Key en su lugar?"
        )
    if len(secret) != _SECRET_KEY_LENGTH:
        raise RuntimeError(
            f"R2_SECRET_ACCESS_KEY debe tener {_SECRET_KEY_LENGTH} caracteres y tiene {len(secret)}."
        )


def _client():
    """
    Un cliente por operación. Son pocas y espaciadas (una subida en la API,
    una descarga y un borrado en el worker por video), y así no hay estado
    compartido entre hilos ni credenciales viejas tras cambiar el token.
    """
    return boto3.client(
        "s3",
        endpoint_url=f"https://{render_settings.R2_ACCOUNT_ID.strip()}.r2.cloudflarestorage.com",
        aws_access_key_id=render_settings.R2_ACCESS_KEY_ID.strip(),
        aws_secret_access_key=render_settings.R2_SECRET_ACCESS_KEY.strip(),
        region_name="auto",
        config=_CONFIG,
    )


def _bucket() -> str:
    return render_settings.R2_BUCKET.strip()


def _error(action: str, path: str, exc: Exception) -> StorageError:
    """El código de R2 (NoSuchKey, AccessDenied...), nunca las credenciales."""
    if isinstance(exc, ClientError):
        error = exc.response.get("Error", {})
        reason = f"{error.get('Code', '?')}: {error.get('Message', '')}".strip()
    else:
        reason = type(exc).__name__
    return StorageError(f"R2 no pudo {action} {path}: {reason}")


def _is_not_found(exc: ClientError) -> bool:
    code = str(exc.response.get("Error", {}).get("Code", ""))
    status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return code in {"404", "NoSuchKey", "NotFound"} or status == 404


def _upload_sync(content: bytes, path: str, content_type: str) -> None:
    try:
        _client().put_object(Bucket=_bucket(), Key=path, Body=content, ContentType=content_type)
    except (ClientError, BotoCoreError) as exc:
        raise _error("subir", path, exc) from exc


def _download_sync(path: str, destination: str) -> None:
    try:
        response = _client().get_object(Bucket=_bucket(), Key=path)
    except (ClientError, BotoCoreError) as exc:
        raise _error("descargar", path, exc) from exc
    body = response["Body"]
    try:
        with open(destination, "wb") as file:
            for chunk in body.iter_chunks(_CHUNK):
                file.write(chunk)
    finally:
        body.close()


def _delete_sync(path: str) -> bool:
    client = _client()
    try:
        client.head_object(Bucket=_bucket(), Key=path)
    except ClientError as exc:
        if _is_not_found(exc):
            return False
        raise _error("consultar", path, exc) from exc
    except BotoCoreError as exc:
        raise _error("consultar", path, exc) from exc
    try:
        client.delete_object(Bucket=_bucket(), Key=path)
    except (ClientError, BotoCoreError) as exc:
        raise _error("borrar", path, exc) from exc
    return True


async def upload_video(content: bytes, path: str, content_type: str) -> None:
    """Sube el video ya validado en memoria a `path`, dentro del bucket."""
    await asyncio.to_thread(_upload_sync, content, path, content_type)
    logger.info("Video subido a R2: %s", path)


async def download_video(path: str, destination: str) -> None:
    """Descarga el video al disco del worker, por partes."""
    await asyncio.to_thread(_download_sync, path, destination)
    logger.info("Video descargado de R2: %s -> %s", path, destination)


async def delete_video(path: str) -> bool:
    """
    Borra el video. Devuelve False si ya no estaba, sin tratarlo como error:
    el barrido de retención puede pasar por un video que el worker ya borró
    al cerrar el job.
    """
    deleted = await asyncio.to_thread(_delete_sync, path)
    if deleted:
        logger.info("Video eliminado de R2: %s", path)
    else:
        logger.info("El video ya no estaba en R2: %s", path)
    return deleted
