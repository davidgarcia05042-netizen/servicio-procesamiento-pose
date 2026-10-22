"""
Worker de la plataforma Render: la app de Celery, las tareas periódicas de
Celery Beat y el puente sync -> async.

Cada tarea es un envoltorio de una línea sobre una función async de la parte
común (app/core); acá solo vive cómo llegan a ejecutarse en Render.

Comandos (ver deploy/render/render.yaml):
  worker: celery -A app.platforms.render.worker.celery worker --concurrency=4
  beat:   celery -A app.platforms.render.worker.celery beat

PUNTO CRÍTICO: Celery no soporta async/await nativamente. El código que
necesitamos correr dentro de las tareas SÍ es async (SQLAlchemy con asyncpg,
httpx para el webhook de vuelta, Firebase en threadpool).

La solución acá es un ÚNICO event loop persistente por proceso worker, creado
en la señal `worker_process_init` y reutilizado por todas las tareas de ese
proceso:

  - `asyncio.run()` por tarea NO sirve: crea y cierra un loop nuevo cada vez,
    lo que rompe las conexiones de asyncpg (quedan atadas a un loop ya
    cerrado) y bajo concurrencia produce "RuntimeError: event loop is already
    running".
  - El baile de `get_event_loop()` con try/except tampoco: termina creando
    loops nuevos silenciosamente y reintroduce el mismo problema.

Con un loop persistente, el engine de SQLAlchemy se crea una sola vez dentro
de ese loop (ver `init_worker`) y todas las tareas lo reusan.

Nota sobre concurrencia: cada proceso worker tiene su propio loop y corre sus
tareas de forma secuencial (`run_until_complete` bloquea hasta terminar). El
paralelismo viene de tener varios procesos (`--concurrency N` con el pool
prefork, que es el default), no de correr varias corrutinas a la vez dentro de
un mismo loop.
"""

import asyncio
from typing import Any, Coroutine, Optional

from celery import Celery
from celery.signals import setup_logging, worker_process_init, worker_process_shutdown

from app.core.config import settings
from app.platforms.render.config import render_settings
from app.utils import video_storage
from app.utils.db_connect import database
from app.utils.logger import get_logger

logger = get_logger("worker")

# Cuánto espera Redis antes de dar por perdida una tarea entregada y no
# confirmada, y volver a encolarla.
#
# Se fija explícito, aunque coincida con el valor por defecto de Celery, porque
# de él depende una invariante: NINGUNA tarea se programa con countdown ni
# eta. Una tarea programada queda "sin confirmar" desde que se publica; si la
# espera supera este plazo, Redis la re-encola y se ejecutan todas las copias.
# Pasaba con los reintentos de callback de 1 y 2 horas: la plataforma recibía
# el aviso dos o tres veces (verificado contra Redis real el 2026-09-24). Por
# eso esas esperas viven ahora en PostgreSQL (ver app/core/callback_retry.py),
# y un test impide volver a programar tareas con espera.
#
# Con solo tareas inmediatas, este plazo nada más tiene que superar lo que
# tarda la tarea más larga: procesar un video.
VISIBILITY_TIMEOUT_SECONDS = 3600

# Se usan las propiedades, no REDIS_BROKER_URL/REDIS_BACKEND_URL crudas: son las
# que inyectan la contraseña y exigen TLS en producción (ver config.py, acá al
# lado).
celery = Celery(
    "pose_worker", broker=render_settings.broker_url, backend=render_settings.backend_url
)
celery.conf.update(
    task_track_started=True,
    task_acks_late=True,          # si el worker muere a mitad, la tarea se re-encola
    worker_prefetch_multiplier=1,  # video = tareas largas: no acaparar la cola
    broker_transport_options={"visibility_timeout": VISIBILITY_TIMEOUT_SECONDS},
    # Ver `_keep_our_logging` más abajo.
    worker_hijack_root_logger=False,
)

# Ver BEAT_SCHEDULE_PATH en config.py (acá al lado): en el contenedor, el
# directorio de trabajo no es escribible y Beat muere al arrancar si intenta
# guardar acá.
celery.conf.beat_schedule_filename = render_settings.BEAT_SCHEDULE_PATH

# Barrido de jobs atascados. Necesita que corra Celery Beat además del worker:
#   celery -A app.platforms.render.worker.celery beat
# Sin Beat, el worker sigue procesando videos normalmente, solo que nadie cierra
# los jobs que quedaron colgados (ver app/core/stuck_jobs.py).
celery.conf.beat_schedule = {
    "sweep-stuck-jobs": {
        "task": "sweep_stuck_jobs_task",
        "schedule": settings.STUCK_JOB_SCAN_INTERVAL_MINUTES * 60,
    },
    # Resumen del estado del servicio (ver app/core/service_status.py). También
    # depende de Beat: sin él no hay latido y el log queda mudo mientras todo
    # parece normal desde afuera.
    "report-service-status": {
        "task": "report_service_status_task",
        "schedule": settings.STATUS_REPORT_INTERVAL_MINUTES * 60,
    },
    # Retención de datos personales (ver app/core/data_retention.py). También
    # depende de Beat: sin él, los datos se conservarían indefinidamente y el
    # servicio quedaría incumpliendo el principio de temporalidad de la Ley
    # 1581 sin que nada lo hiciera evidente.
    "purge-expired-data": {
        "task": "purge_expired_data_task",
        "schedule": settings.RETENTION_SCAN_INTERVAL_HOURS * 3600,
    },
    # Reintentos de los avisos a la plataforma (ver app/core/callback_retry.py).
    # También dependen de Beat: sin él, un aviso que falló una vez no se
    # vuelve a intentar nunca.
    "dispatch-due-callbacks": {
        "task": "dispatch_due_callbacks_task",
        "schedule": settings.CALLBACK_DISPATCH_INTERVAL_SECONDS,
        # Corre cada pocos segundos y comparte la cola con los videos. Con los
        # workers ocupados, las corridas se acumularían detrás de ellos; con
        # esto, una corrida que no alcanzó a ejecutarse antes de la siguiente
        # se descarta en vez de sumarse.
        "options": {"expires": settings.CALLBACK_DISPATCH_INTERVAL_SECONDS},
    },
    "recover-lost-callbacks": {
        "task": "recover_lost_callbacks_task",
        "schedule": settings.STUCK_JOB_SCAN_INTERVAL_MINUTES * 60,
    },
}

@setup_logging.connect
def _keep_our_logging(**kwargs: Any) -> None:
    """
    Le quita a Celery el control del logging.

    Conectarse a esta señal hace que Celery NO configure nada: se queda la
    configuración de app/utils/logger.py, que sale de `LOG_LEVEL`.

    POR QUÉ HACE FALTA: sin esto, Celery reconfigura el logger raíz al arrancar
    y el nivel lo fija `--loglevel` de la línea de comandos. O sea que
    `LOG_LEVEL` no tenía efecto justo en el worker -- y en Render la línea de
    comandos vive en render.yaml, así que cambiar el nivel para diagnosticar un
    incidente habría exigido un despliegue, que es exactamente lo que la
    variable existe para evitar.
    `worker_hijack_root_logger=False` por sí solo no alcanzaba: evita que
    Celery pise los manejadores, pero el nivel lo seguía fijando igual.

    Efecto secundario a tener presente: `--loglevel` queda ignorado en los
    comandos de Celery. Por eso no aparece en los comandos documentados.
    """


# Loop persistente del proceso worker. Se crea en worker_process_init y vive
# hasta que el proceso termina.
_loop: Optional[asyncio.AbstractEventLoop] = None


@worker_process_init.connect
def init_worker(**kwargs: Any) -> None:
    """Crea el loop persistente y conecta la base de datos DENTRO de ese loop."""
    global _loop
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)
    video_storage.ensure_configured()
    _loop.run_until_complete(database.connect())
    logger.info("Worker inicializado: event loop persistente + PostgreSQL conectado")


@worker_process_shutdown.connect
def shutdown_worker(**kwargs: Any) -> None:
    global _loop
    if _loop is None:
        return
    _loop.run_until_complete(database.disconnect())
    _loop.close()
    _loop = None
    logger.info("Worker apagado: conexiones cerradas y loop liberado")


def run_async(coro: Coroutine[Any, Any, Any]) -> Any:
    """
    Corre una corrutina en el loop persistente de este proceso.

    Falla explícitamente si el loop no existe en vez de crear uno al vuelo:
    que no exista significa que la tarea está corriendo fuera de un worker
    inicializado (ej. llamada directa en un test), y crear un loop nuevo ahí
    escondería el problema en vez de mostrarlo.
    """
    if _loop is None:
        raise RuntimeError(
            "No hay event loop de worker inicializado. Las tareas de Celery deben "
            "correr en un worker (que dispara worker_process_init). Para tests, "
            "llama directamente a la corrutina con asyncio.run()."
        )
    return _loop.run_until_complete(coro)


@celery.task(name="process_video_task")
def process_video_task(job_id: str) -> str:
    """Tarea encolada por la API. El cuerpo real es async, ver app/core/video_job.py."""
    from app.core.video_job import process_video_job

    return run_async(process_video_job(job_id))


@celery.task(name="sweep_stuck_jobs_task")
def sweep_stuck_jobs_task() -> list[str]:
    """Tarea periódica disparada por Celery Beat, ver app/core/stuck_jobs.py."""
    from app.core.stuck_jobs import sweep_stuck_jobs

    return run_async(sweep_stuck_jobs())


@celery.task(name="purge_expired_data_task")
def purge_expired_data_task() -> int:
    """Tarea periódica disparada por Celery Beat, ver app/core/data_retention.py."""
    from app.core.data_retention import purge_expired_data

    return run_async(purge_expired_data())


@celery.task(name="dispatch_due_callbacks_task")
def dispatch_due_callbacks_task() -> list[str]:
    """Tarea periódica disparada por Celery Beat, ver app/core/callback_retry.py."""
    from app.core.callback_retry import dispatch_due_callbacks

    return run_async(dispatch_due_callbacks())


@celery.task(name="recover_lost_callbacks_task")
def recover_lost_callbacks_task() -> list[str]:
    """Tarea periódica disparada por Celery Beat, ver app/core/callback_retry.py."""
    from app.core.callback_retry import recover_lost_callbacks

    return run_async(recover_lost_callbacks())


@celery.task(name="report_service_status_task")
def report_service_status_task() -> dict[str, Any]:
    """Tarea periódica disparada por Celery Beat, ver app/core/service_status.py."""
    from app.core.service_status import report_service_status

    return run_async(report_service_status())


@celery.task(name="send_callback_task")
def send_callback_task(job_id: str) -> str:
    """
    UN intento de entregar el resultado a la plataforma.

    No reintenta por su cuenta: si falla, deja escrita en la tabla la hora del
    próximo intento y el despachador lo encola cuando llegue (ver
    app/core/callback_retry.py). Va en su propia tarea y no dentro de
    `process_video_job` para que reintentar cueste un POST y no volver a
    procesar el video entero.
    """
    from app.core.callback_retry import attempt_callback

    return run_async(attempt_callback(job_id))
