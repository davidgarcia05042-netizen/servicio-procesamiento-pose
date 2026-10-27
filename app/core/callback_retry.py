"""
Entrega de los avisos a la plataforma y planificación de sus reintentos.

DÓNDE VIVE LA ESPERA ENTRE INTENTOS: en la tabla, en `callback_next_attempt_at`,
y no en el broker. Antes cada reintento era una tarea de Celery programada con
countdown, y eso tenía dos problemas verificados el 2026-09-24:

  - Una tarea programada queda "sin confirmar" en Redis desde que se publica.
    Si la espera superaba el visibility_timeout, Redis la re-encolaba mientras
    el worker todavía la guardaba, y al llegar la hora se ejecutaban todas las
    copias: la plataforma recibía el aviso dos o tres veces.
  - Si el worker se reiniciaba mientras guardaba un reintento -- un despliegue
    en Render, o recrear el contenedor en la demo --, ese reintento quedaba
    varado en Redis hasta que venciera el visibility_timeout.

Ahora la cola solo lleva tareas INMEDIATAS, y un reinicio no retiene nada: la
hora del próximo intento sobrevive en PostgreSQL.

EL CICLO DE UN AVISO:

    pending ──intento──► delivered            (la plataforma respondió 2xx)
       ▲          ├────► rejected             (respondió algo no reintentable)
       │          └────► retrying ──┐         (fallo transitorio: queda la hora)
       │                            │
       └──── despachador ◄──────────┘         (cuando la hora llega)

    retrying ──► exhausted   si el próximo intento caería fuera de la ventana

  - `attempt_callback` hace UN intento y deja escrito qué sigue. La política
    (cuánto esperar, cuándo abrir otra ronda, cuándo rendirse) es
    `next_step_after_failure` en app/core/callback_delivery.py.
  - `dispatch_due_callbacks` corre cada `CALLBACK_DISPATCH_INTERVAL_SECONDS`,
    toma los avisos cuya hora ya llegó y los encola.
  - `recover_lost_callbacks` rescata los que quedaron en `pending` sin tarea:
    la tarea inmediata se perdió porque el worker murió a mitad del intento o
    el broker se vació. Sin esto, ese aviso no se volvería a intentar nunca, y
    `pending` no se cuenta como deuda en el resumen de estado.

QUÉ **NO** SE REINTENTA, Y POR QUÉ: los `rejected`. La plataforma respondió
algo no reintentable, típicamente un 4xx: el mismo payload falla igual mañana.
El caso realista es un 401 por el esquema HMAC, que todavía no está acordado
con la plataforma (bloqueador #2 de docs/DECISIONES_TECNICAS.md). Eso se arregla del lado de la
configuración; arreglada la causa, esas filas se recuperan a mano.

ENTREGA "AL MENOS UNA VEZ": si un worker muere justo después de que la
plataforma recibió el aviso y antes de escribirlo en la tabla, el aviso se
vuelve a enviar. Es inevitable en cualquier webhook y es la razón por la que
el receptor debería tolerar duplicados.
"""

from datetime import datetime, timedelta, timezone

from app.constants.job_constants import CallbackStatus
from app.core.callback_delivery import (
    CallbackDeliveryError,
    CallbackRejectedError,
    deliver_callback,
    next_step_after_failure,
)
from app.core.callback_queue import enqueue_callback, payload_from_job
from app.core.config import settings
from app.core.job_logging import log_job_event
from app.utils.db_connect import (
    claim_due_callbacks,
    get_job,
    requeue_stale_callbacks,
    update_job,
)
from app.utils.logger import get_logger

logger = get_logger("callback_retry")


async def attempt_callback(job_id: str) -> str:
    """
    Un intento de entrega. Devuelve el estado en que quedó el aviso.

    Solo actúa sobre un aviso en `pending`. Cualquier otro estado significa
    que esta tarea llegó tarde -- una re-entrega del broker de un intento que
    ya tuvo desenlace -- y actuar igual podría mandar un aviso ya entregado.
    """
    job = await get_job(job_id)
    if job is None or job.callback_status != CallbackStatus.PENDING.value:
        logger.info("Intento de aviso descartado para %s: no está pendiente", job_id)
        return "skipped"
    if not job.results_endpoint:
        # Minimizado por retención mientras esperaba: ya no hay a quién avisar.
        await update_job(job_id, callback_status=CallbackStatus.EXHAUSTED.value)
        return CallbackStatus.EXHAUSTED.value

    attempt = (job.callback_attempts or 0) + 1

    try:
        await deliver_callback(job.results_endpoint, payload_from_job(job).model_dump())
    except CallbackRejectedError as exc:
        return await _record(job_id, CallbackStatus.REJECTED, attempt, str(exc))
    except CallbackDeliveryError as exc:
        step = next_step_after_failure(
            attempt,
            created_at=job.created_at,
            now=datetime.now(timezone.utc),
            retry_after=exc.retry_after,
            round_pause=timedelta(minutes=settings.CALLBACK_RETRY_INTERVAL_MINUTES),
            window=timedelta(hours=settings.CALLBACK_RETRY_WINDOW_HOURS),
        )
        return await _record(job_id, step.status, attempt, str(exc), step.next_attempt_at)

    return await _record(job_id, CallbackStatus.DELIVERED, attempt)


async def _record(
    job_id: str,
    status: CallbackStatus,
    attempt: int,
    error: str | None = None,
    next_attempt_at: datetime | None = None,
) -> str:
    """
    El desenlace va a la tabla, no solo al log: los logs de Render son
    efímeros, y esto es lo que hay que poder consultar días después para saber
    qué resultados nunca llegaron a la plataforma.
    """
    await update_job(
        job_id,
        callback_status=status.value,
        callback_attempts=attempt,
        callback_error=error,
        callback_next_attempt_at=next_attempt_at,
    )
    log_job_event(
        f"callback_{status.value}",
        job_id,
        attempts=attempt,
        error=error,
        next_attempt_at=next_attempt_at,
    )
    return status.value


async def dispatch_due_callbacks() -> list[str]:
    """
    Encola los avisos cuya hora de reintento ya llegó. Devuelve sus ids.

    La consulta los pasa a `pending` en la misma sentencia que los toma (ver
    `_move_callbacks` en db_connect.py), así que dos corridas solapadas no
    pueden encolar el mismo aviso dos veces.
    """
    now = datetime.now(timezone.utc)
    claimed = await claim_due_callbacks(now, settings.CALLBACK_RETRY_BATCH_SIZE)

    enqueued: list[str] = []
    for job_id in claimed:
        if enqueue_callback(job_id):
            enqueued.append(job_id)
        else:
            # Se devuelve a `retrying` con la hora vencida, en vez de dejarlo
            # en `pending`: así lo vuelve a tomar la próxima corrida, en
            # segundos, y no el barrido de avisos perdidos, en horas.
            await update_job(
                job_id,
                callback_status=CallbackStatus.RETRYING.value,
                callback_next_attempt_at=now,
            )

    return enqueued


async def recover_lost_callbacks() -> list[str]:
    """
    Devuelve al ciclo los avisos cuya tarea inmediata se perdió.

    Un aviso en `pending` debería resolverse en segundos. Si lleva más de
    `CALLBACK_STALE_PENDING_HOURS` así, su tarea desapareció. Se pasa a
    `retrying` con la hora vencida y el despachador lo toma en su próxima
    corrida, así que el rescate usa el mismo camino que un reintento normal.
    """
    now = datetime.now(timezone.utc)
    recovered = await requeue_stale_callbacks(
        idle_before=now - timedelta(hours=settings.CALLBACK_STALE_PENDING_HOURS),
        now=now,
        limit=settings.CALLBACK_RETRY_BATCH_SIZE,
    )

    for job_id in recovered:
        log_job_event("callback_recovered", job_id)
    if recovered:
        logger.warning("%d aviso(s) sin tarea vuelven al ciclo de reintentos", len(recovered))

    return recovered
