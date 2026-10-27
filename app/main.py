"""
Entrypoint de la API. Ejecutar con: uvicorn app.main:app --reload

La autenticación va como middleware a nivel de aplicación (patrón del repo de
referencia), pero con firma HMAC en vez de login de usuario: acá no hay
usuarios finales, solo la plataforma cliente.
"""

from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.api import health, video_processing
from app.core.config import settings
from app.core.rate_limit import limiter
from app import platforms
from app.utils import video_storage
from app.utils.db_connect import database
from app.utils.hmac_auth import is_valid_signature
from app.utils.logger import get_logger

logger = get_logger("main")

# Rutas que no requieren firma (monitoreo y documentación).
_PUBLIC_PATHS = {"/health", "/docs", "/redoc", "/openapi.json"}


def _too_large_response() -> JSONResponse:
    return JSONResponse(status_code=413, content={"detail": "video demasiado grande"})


@asynccontextmanager
async def lifespan(app: FastAPI):
    video_storage.ensure_configured()
    await database.connect()
    yield
    await database.disconnect()
    # La plataforma puede mantener abiertos sus propios clientes (en Render,
    # el de sondeo de Redis); si no se cierran acá, el apagado los deja
    # colgando.
    await platforms.current().close()


app = FastAPI(
    title="Servicio de Procesamiento Asíncrono de Pose",
    description="Recibe video de la plataforma de telefisioterapia, calcula ángulos "
    "articulares con MediaPipe y devuelve los resultados por webhook.",
    version="0.1.0",
    lifespan=lifespan,
)

# slowapi busca el limiter en app.state; el manejador traduce el exceso de
# solicitudes a un 429 con el header Retry-After.
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


# ORDEN DE LOS MIDDLEWARES -- LEER ANTES DE REORDENAR ESTAS DOS FUNCIONES.
#
# Starlette registra con `user_middleware.insert(0, ...)`, así que el ÚLTIMO
# middleware declarado en este archivo es el más EXTERNO y corre PRIMERO. Es
# al revés de lo que sugiere el orden de lectura.
#
# Por eso `reject_oversized_payload` se declara DESPUÉS de
# `verify_hmac_signature`: así corre ANTES que él y corta las solicitudes
# demasiado grandes sin que el de HMAC llegue a bufferizar el cuerpo en
# memoria. Verificado empíricamente: una solicitud de 2MB contra un límite de
# 1MB devuelve 413 y el middleware de HMAC nunca se ejecuta.
#
# Si se invierte el orden de declaración, el de HMAC pasa a correr primero y
# el chequeo de tamaño deja de servir para lo que existe: el cuerpo entero ya
# habría quedado en RAM antes de rechazarse.


async def _read_body_within_limit(request: Request, limit: int) -> Optional[bytes]:
    """
    Lee el cuerpo por trozos y se detiene en cuanto supera `limit`. Devuelve
    None si lo superó.

    POR QUÉ NO `request.body()`: el chequeo de `reject_oversized_payload` mira
    el header `Content-Length`, y una solicitud enviada por trozos
    (`Transfer-Encoding: chunked`) no lo trae. Con `request.body()` ese cuerpo
    se leía ENTERO a memoria, sin tope, antes de cualquier validación: un
    cuerpo de varios GB tumbaba la instancia. Verificado el 2026-09-24: con el
    límite en 1 MB, 3 MB sin `Content-Length` llegaban hasta la verificación de
    firma en vez de cortarse con 413.

    El cuerpo leído se deja en `request._body` porque es de donde Starlette lo
    reenvía hacia FastAPI (ver `_CachedRequest` en starlette/middleware/base.py).
    Es un detalle interno de Starlette; `test_security.py` comprueba que el
    endpoint siga recibiendo el cuerpo completo.
    """
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)

    body = b"".join(chunks)
    request._body = body
    return body


@app.middleware("http")
async def verify_hmac_signature(request: Request, call_next):
    if request.url.path in _PUBLIC_PATHS:
        return await call_next(request)

    # Firmar el cuerpo completo obliga a bufferizarlo acá antes de que FastAPI
    # parsee el multipart, así que el video entero queda en memoria (acotado
    # por MAX_VIDEO_SIZE_MB). Es asumible porque la validación en memoria ya
    # lo carga completo de todas formas; si en el futuro hay que soportar
    # videos mucho más grandes en streaming, habría que firmar solo los
    # headers/metadatos en vez del cuerpo.
    body = await _read_body_within_limit(request, settings.max_video_size_bytes)
    if body is None:
        logger.warning("Request rechazado: el cuerpo supera el límite sin declararlo")
        return _too_large_response()
    timestamp = request.headers.get("X-Timestamp", "")
    signature = request.headers.get("X-Signature", "")

    if not is_valid_signature(timestamp, signature, body):
        if settings.HMAC_ENFORCE:
            logger.warning("Request rechazado por firma HMAC inválida: %s", request.url.path)
            return JSONResponse(status_code=401, content={"detail": "firma inválida"})

        # Modo advertencia: la plataforma todavía no firma de su lado, así que
        # se deja pasar y se registra. Cambiar HMAC_ENFORCE a True en cuanto
        # confirmen que ya envían la firma.
        logger.warning(
            "Solicitud SIN firma HMAC válida aceptada por HMAC_ENFORCE=false "
            "(ruta: %s, trae X-Signature: %s)",
            request.url.path,
            bool(signature),
        )

    return await call_next(request)


@app.middleware("http")
async def reject_oversized_payload(request: Request, call_next):
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            declared_bytes = int(declared)
        except ValueError:
            return JSONResponse(status_code=400, content={"detail": "Content-Length inválido"})

        if declared_bytes > settings.max_video_size_bytes:
            logger.warning(
                "Request rechazado por tamaño declarado: %d bytes > %d permitidos",
                declared_bytes,
                settings.max_video_size_bytes,
            )
            return _too_large_response()

    return await call_next(request)


app.include_router(health.router)
app.include_router(video_processing.router)
