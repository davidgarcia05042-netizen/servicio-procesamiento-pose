"""
Quién puede entregarle tareas al worker: solo Cloud Tasks.

Cada tarea llega con un token OIDC en `Authorization: Bearer ...`. Se acepta
solo si:

  - lo firmó el emisor configurado (Google; en desarrollo, el emulador), con
    una de sus claves públicas;
  - fue emitido PARA el worker (`aud` = WORKER_URL): un token de Google para
    otro servicio no sirve acá;
  - es de la cuenta de servicio de las tareas (`email` = TASKS_SERVICE_ACCOUNT,
    verificado);
  - no venció.

LA PRIMERA BARRERA ES CLOUD RUN: el worker se despliega sin acceso público y
solo la cuenta de las tareas puede invocarlo. Esto es la segunda. Desplegarlo
público por error es un solo flag, y sin esta verificación cualquiera que
conociera un job_id podría hacer reprocesar un video o reenviar un aviso.

LAS CLAVES PÚBLICAS se piden al emisor y se guardan en memoria. Google las
rota cada pocos días; cuando llega un token firmado con una clave que no se
conoce, PyJWKClient las vuelve a pedir.
"""

import asyncio
import threading
from typing import Any, Optional

import jwt
from fastapi import Header, HTTPException

from app.platforms.gcp.config import GOOGLE_OIDC_ISSUERS, gcp_settings
from app.utils.logger import get_logger

logger = get_logger("task_auth")


class TaskAuthError(Exception):
    """El token falta o no es válido: la solicitud no viene de Cloud Tasks."""


class TaskAuthUnavailable(Exception):
    """No se pudieron obtener las claves públicas del emisor: no se sabe."""


_lock = threading.Lock()
_jwks: Optional[jwt.PyJWKClient] = None


def _jwks_client() -> jwt.PyJWKClient:
    global _jwks
    with _lock:
        if _jwks is None:
            _jwks = jwt.PyJWKClient(gcp_settings.TASKS_OIDC_JWKS_URL, cache_keys=True, lifespan=3600)
    return _jwks


def _allowed_issuers() -> frozenset[str]:
    issuer = gcp_settings.TASKS_OIDC_ISSUER
    # Google firma con cualquiera de las dos formas de su emisor.
    return GOOGLE_OIDC_ISSUERS if issuer in GOOGLE_OIDC_ISSUERS else frozenset({issuer})


def verify_task_token(authorization: Optional[str]) -> dict[str, Any]:
    """Devuelve las claims del token, o lanza TaskAuthError / TaskAuthUnavailable."""
    if not authorization or not authorization.startswith("Bearer "):
        raise TaskAuthError("la solicitud no trae un token Bearer")
    token = authorization.removeprefix("Bearer ").strip()

    try:
        key = _jwks_client().get_signing_key_from_jwt(token)
    except jwt.PyJWKClientConnectionError as exc:
        raise TaskAuthUnavailable(f"no se pudieron obtener las claves del emisor: {exc}") from exc
    except jwt.PyJWTError as exc:
        raise TaskAuthError(f"token sin una clave conocida: {exc}") from exc

    try:
        claims = jwt.decode(
            token,
            key.key,
            algorithms=["RS256"],
            audience=gcp_settings.worker_url,
            options={"require": ["exp", "iat", "iss", "aud", "email"]},
        )
    except jwt.PyJWTError as exc:
        raise TaskAuthError(f"token inválido: {exc}") from exc

    if claims["iss"] not in _allowed_issuers():
        raise TaskAuthError(f"emisor no aceptado: {claims['iss']}")
    if claims["email"] != gcp_settings.TASKS_SERVICE_ACCOUNT or claims.get("email_verified") is not True:
        raise TaskAuthError(f"cuenta no aceptada: {claims['email']}")
    return claims


async def require_cloud_tasks(authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    """
    Dependencia de FastAPI para los endpoints del worker.

    401 si el token no vale; 503 si no se pudo verificar por no alcanzar al
    emisor. Las dos hacen que Cloud Tasks reintente más tarde, que es lo
    correcto en el segundo caso; en el primero, una solicitud que no vino de
    Cloud Tasks nadie la reintenta.

    En un hilo aparte: pedir las claves (cuando no están en memoria) es una
    llamada HTTP bloqueante.
    """
    try:
        return await asyncio.to_thread(verify_task_token, authorization)
    except TaskAuthUnavailable as exc:
        logger.error("No se pudo verificar el token de una tarea: %s", exc)
        raise HTTPException(status_code=503, detail="no se pudo verificar el token") from exc
    except TaskAuthError as exc:
        # El token nunca va al log: solo el motivo del rechazo.
        logger.warning("Tarea rechazada: %s", exc)
        raise HTTPException(status_code=401, detail="token inválido") from exc
