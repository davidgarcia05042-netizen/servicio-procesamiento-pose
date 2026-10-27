"""
Supabase Storage como almacenamiento del video (alternativa a Firebase).

POR QUÉ EXISTE: desde 2024 Cloud Storage for Firebase exige el plan Blaze, con
una cuenta de facturación de Google, y para la fase piloto eso fue un bloqueo
(2026-10-07). La base ya vive en un proyecto de Supabase, y su Storage entra en
el plan gratuito. Es uno de los dos almacenamientos de Render: se elige con
STORAGE_BACKEND=supabase (ver `video_storage()` en __init__.py); el código de
Firebase sigue intacto.

Implementa el contrato de almacenamiento de app/platforms/__init__.py: la ruta
la decide app/utils/video_storage.py.

Habla con la API REST de Storage usando httpx, que ya es dependencia del
servicio (es el cliente del callback). No hace falta el SDK de Supabase.

LÍMITES DEL PLAN GRATUITO que condicionan el servicio:

  - 50 MB por archivo. Por eso deploy/render/render.yaml baja
    MAX_VIDEO_SIZE_MB a 50. Los videos de TOMA FRONTAL pesan 28-43 MB
    (11-17 s).
  - 1 GB de almacenamiento. Alcanza porque el worker borra cada video al
    terminar de procesarlo: solo se guardan los que están en cola.
  - 5 GB de egreso al mes. Cada video se descarga una vez (el worker): con
    ~40 MB por video son ~125 videos al mes.

LA CLAVE: la "secret key" del proyecto (sb_secret_...). Opera como el rol
service_role, que se salta RLS: por eso vive solo en el servidor, nunca en un
cliente. Las claves nuevas no son JWT y van en el header `apikey`; una clave
heredada (`service_role`, un JWT "eyJ...") además va como Bearer.
"""

from urllib.parse import quote

import httpx

from app.platforms.render.config import render_settings
from app.utils.logger import get_logger

logger = get_logger("supabase_storage")

# Subir o bajar 50 MB puede tardar; conectar no debería.
_TIMEOUT = httpx.Timeout(connect=10.0, read=300.0, write=300.0, pool=10.0)


class StorageError(RuntimeError):
    """La API de Storage respondió con un error."""


def ensure_configured() -> None:
    """
    Falla al arrancar si faltan la URL o la clave del proyecto. Sin esto, la
    API aceptaría el primer video y la subida reventaría.
    """
    if not render_settings.SUPABASE_URL.startswith("https://"):
        raise RuntimeError(
            "STORAGE_BACKEND=supabase exige SUPABASE_URL con https:// "
            "(la URL del proyecto, https://<ref>.supabase.co)."
        )
    if "CAMBIAR" in render_settings.SUPABASE_URL:
        raise RuntimeError("SUPABASE_URL todavía tiene el valor de relleno de render.yaml.")
    if not render_settings.SUPABASE_SECRET_KEY.strip():
        raise RuntimeError("STORAGE_BACKEND=supabase exige SUPABASE_SECRET_KEY.")


def _headers() -> dict[str, str]:
    key = render_settings.SUPABASE_SECRET_KEY
    headers = {"apikey": key}
    if key.startswith("eyJ"):  # clave heredada: un JWT
        headers["Authorization"] = f"Bearer {key}"
    return headers


def _client() -> httpx.AsyncClient:
    """Un cliente por operación: son pocas y espaciadas, no vale un pool."""
    return httpx.AsyncClient(
        base_url=f"{render_settings.SUPABASE_URL.rstrip('/')}/storage/v1",
        headers=_headers(),
        timeout=_TIMEOUT,
    )


def _object_path(path: str) -> str:
    bucket = quote(render_settings.SUPABASE_STORAGE_BUCKET, safe="")
    return f"{bucket}/{quote(path, safe='/')}"


def _check(response: httpx.Response, action: str, path: str) -> None:
    if response.is_success:
        return
    # El cuerpo trae el motivo ("Bucket not found", "Payload too large"...).
    # La clave va en los headers de la solicitud, nunca en la respuesta.
    raise StorageError(
        f"Supabase Storage no pudo {action} {path}: HTTP {response.status_code} "
        f"{response.text[:300]}"
    )


async def upload_video(content: bytes, path: str, content_type: str) -> None:
    """Sube el video ya validado a `path`, dentro del bucket. Nunca sobrescribe."""
    async with _client() as client:
        response = await client.post(
            f"/object/{_object_path(path)}",
            content=content,
            headers={"content-type": content_type, "x-upsert": "false"},
        )
    _check(response, "subir", path)
    logger.info("Video subido a Supabase Storage: %s", path)


async def download_video(path: str, destination: str) -> None:
    """
    Descarga el video al disco del worker, por partes.

    Escribir con `open()` normal bloquea el event loop un instante por parte:
    acá es inocuo, porque en el worker el loop no tiene otra cosa que hacer
    mientras procesa este job (ver docs/DECISIONES_TECNICAS.md, "event loop persistente").
    """
    async with _client() as client:
        async with client.stream("GET", f"/object/authenticated/{_object_path(path)}") as response:
            if not response.is_success:
                await response.aread()
                _check(response, "descargar", path)
            with open(destination, "wb") as file:
                async for chunk in response.aiter_bytes():
                    file.write(chunk)
    logger.info("Video descargado de Supabase Storage: %s -> %s", path, destination)


async def delete_video(path: str) -> bool:
    """
    Borra el video. Devuelve False si ya no estaba, sin tratarlo como error.

    Usa el borrado por lista porque responde con lo que de verdad borró: una
    lista vacía significa que el objeto no existía. Es la misma semántica que
    la versión de Firebase (ver su docstring: que no esté es un desenlace
    normal, no un fallo).
    """
    bucket = quote(render_settings.SUPABASE_STORAGE_BUCKET, safe="")
    async with _client() as client:
        response = await client.request("DELETE", f"/object/{bucket}", json={"prefixes": [path]})
    _check(response, "borrar", path)

    deleted = bool(response.json())
    if deleted:
        logger.info("Video eliminado de Supabase Storage: %s", path)
    else:
        logger.info("El video ya no estaba en Supabase Storage: %s", path)
    return deleted
