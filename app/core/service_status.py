"""
Resumen periódico del estado del servicio.

QUÉ PROBLEMA RESUELVE: desplegado en Render, lo único que se ve desde afuera es
el log. `/health` responde si PostgreSQL y Redis contestan, pero no si los
videos se están procesando: con las dependencias sanas y el worker atascado, la
respuesta sigue siendo 200. Y Flower, que da la profundidad de la cola, solo
está en el docker-compose local -- en producción no hay ninguna vista de la
cola.

Este módulo emite cada `STATUS_REPORT_INTERVAL_MINUTES` una línea JSON con tres
cosas que responden preguntas distintas:

  - **Actividad reciente** (`received`, y el desglose por estado): cuántos
    videos entraron en la última ventana y cómo terminaron. Dice si el servicio
    está haciendo algo y con qué tasa de fallo.
  - **Backlog** (`queued`, `processing`): cuántos jobs siguen sin terminar
    AHORA, sin importar cuándo entraron. Un job que entró hace tres horas y
    sigue en `queued` no aparece en la ventana reciente, pero es exactamente el
    síntoma de que el worker no da abasto.
  - **Deuda con la plataforma** (`undelivered_callbacks`): resultados
    calculados y guardados que la plataforma nunca recibió, porque rechazó el
    callback o porque se agotó la ventana de reintentos. Aparte va
    `retrying_callbacks`: los que todavía se están intentando. No son deuda
    aún, pero si crecen es que la plataforma no está respondiendo.

El resumen sale siempre, también cuando todo está bien: es el latido que
permite distinguir "no pasó nada" de "el worker lleva horas muerto". Lo que
solo sale cuando hay algo que mirar es la advertencia.
"""

from datetime import datetime, timedelta, timezone
from typing import Any

from app.core.config import settings
from app.core.job_logging import log_job_event
from app.utils.db_connect import (
    count_jobs_by_status,
    count_open_jobs,
    count_retrying_callbacks,
    count_undelivered_callbacks,
)
from app.utils.logger import get_logger

logger = get_logger("service_status")


async def collect_status() -> dict[str, Any]:
    """
    Junta los números del resumen.

    Separada de `report_service_status` para poder probar los números sin
    depender de lo que se registre en el log.
    """
    window = settings.STATUS_REPORT_WINDOW_MINUTES
    since = datetime.now(timezone.utc) - timedelta(minutes=window)

    recent = await count_jobs_by_status(since)
    open_jobs = await count_open_jobs()
    undelivered = await count_undelivered_callbacks()
    retrying = await count_retrying_callbacks()

    return {
        "window_minutes": window,
        # Todo lo que entró en la ventana, terminado o no.
        "received": sum(recent.values()),
        "done": recent.get("done", 0),
        "failed": recent.get("failed", 0),
        # Backlog actual, sin ventana: es lo que dice si el worker se quedó
        # atrás, y un job viejo atascado no aparecería en los de arriba.
        "queued": open_jobs.get("queued", 0),
        "processing": open_jobs.get("processing", 0),
        "undelivered_callbacks": undelivered,
        "retrying_callbacks": retrying,
    }


async def report_service_status() -> dict[str, Any]:
    """
    Emite el resumen y, si hay resultados sin entregar, una advertencia aparte.

    La advertencia va en su propia línea y con nivel WARNING para que se pueda
    filtrar y alertar sobre ella sin tener que parsear el resumen.
    """
    status = await collect_status()
    log_job_event("service_status", **status)

    if status["undelivered_callbacks"]:
        logger.warning(
            "Hay %d resultado(s) que la plataforma nunca recibió. "
            "Ver la sección 'Qué queda registrado' del README para encontrarlos.",
            status["undelivered_callbacks"],
        )

    return status
