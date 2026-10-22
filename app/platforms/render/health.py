"""
Sondeo propio de Render para /health: el broker de Celery (Redis).

La base la comprueba app/api/health.py, que es común a todas las plataformas;
acá solo va lo que existe en Render.
"""

import asyncio
from typing import Optional

import redis.asyncio as aioredis

from app.platforms.render.config import render_settings
from app.utils.logger import get_logger

logger = get_logger("health")

# Tope por sondeo. Ver _PROBE_TIMEOUT_SECONDS en app/api/health.py: si una
# dependencia se cuelga, /health tiene que responder igual.
_PROBE_TIMEOUT_SECONDS = 3.0

# Cliente reutilizado entre sondeos. Crear uno nuevo en cada llamada abriría y
# cerraría una conexión a Redis cada pocos segundos, para siempre.
_queue_client: Optional[aioredis.Redis] = None


def _get_queue_client() -> aioredis.Redis:
    global _queue_client
    if _queue_client is None:
        # Se usa la propiedad `broker_url` y no la variable cruda: es la que
        # inyecta la contraseña y exige TLS en producción (ver config.py).
        _queue_client = aioredis.from_url(
            render_settings.broker_url,
            socket_connect_timeout=_PROBE_TIMEOUT_SECONDS,
            socket_timeout=_PROBE_TIMEOUT_SECONDS,
        )
    return _queue_client


async def close_probes() -> None:
    """Cierra el cliente de sondeo. La llama el lifespan de la app al apagar."""
    global _queue_client
    if _queue_client is not None:
        await _queue_client.aclose()
        _queue_client = None


async def check_queue() -> str:
    """
    PING al broker de Celery.

    Se comprueba el broker y no el backend de resultados: sin broker la API no
    puede encolar nada, que es lo único que hace con la cola. El backend solo
    guarda el resultado de la tarea, que este servicio ni siquiera lee (el
    estado vive en la tabla `jobs`).
    """
    try:
        async with asyncio.timeout(_PROBE_TIMEOUT_SECONDS):
            await _get_queue_client().ping()
        return "ok"
    except Exception:  # noqa: BLE001 -- cualquier fallo es "no está sano"
        logger.exception("Health check: el broker de Redis no responde")
        return "error"
