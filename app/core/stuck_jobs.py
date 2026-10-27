"""
Barrido periódico de jobs colgados.

Hay DOS formas de quedarse colgado, con causas distintas, y el barrido cierra
las dos:

  - **En PROCESSING**: el worker tomó el trabajo y desapareció a mitad. El
    OOM-kill es el caso realista (videos de hasta 200 MB y varios procesos
    compartiendo la RAM de la instancia). Nadie alcanza a marcar la fila
    FAILED, así que queda en PROCESSING para siempre. Umbral:
    `STUCK_JOB_TIMEOUT_MINUTES`.
  - **En QUEUED**: nadie tomó el trabajo nunca. Pasa si la tarea encolada se
    pierde -- un reinicio del broker vacía la cola -- o si no queda ningún
    worker vivo para consumirla. Umbral:
    `STUCK_JOB_QUEUED_TIMEOUT_MINUTES`, más alto porque esperar en la cola es
    normal (ver el comentario en app/core/config.py).

Los dos fallos son silenciosos: no hay excepción, no hay log de error, solo una
fila que dejó de avanzar mientras la plataforma espera un callback que ya no va
a llegar.

QUÉ HACE ESTE MÓDULO: cierra la fila como FAILED con el código que corresponde
a cada caso Y le avisa a la plataforma, encolando el mismo callback de fallo
que usaría un job que falló normalmente. Sin ese aviso, cerrar la fila sacaría
del engaño a quien mire la base de datos, pero la plataforma seguiría esperando
indefinidamente — que es el problema que había que resolver.

Y BORRA EL VIDEO, como cualquier otro cierre de un job (ver
app/core/video_job.py). Antes no lo hacía: el video de un job cerrado por el
barrido quedaba guardado hasta la retención, 30 días después, sin ninguna
finalidad (Ley 1581). Borrarlo es seguro: si el worker sigue vivo con ese job,
ya tiene su copia local y su cierre ya no se escribe (el job está cerrado); si
la tarea llega después, se descarta sin tocar el video.

OJO CON `task_acks_late` (ver app/platforms/render/worker.py): esa opción hace que Celery
re-encole la tarea si el worker muere, así que hay un segundo mecanismo de
recuperación en juego. Con Redis como broker, la re-entrega ocurre recién
cuando vence el `visibility_timeout`, que es de una hora (ver
VISIBILITY_TIMEOUT_SECONDS en app/platforms/render/worker.py). O sea: este barrido es el
que recupera un job colgado, mucho antes que Celery. Si después la re-entrega
llega, el worker la descarta porque la fila ya está cerrada (ver la guarda de
estado terminal en app/core/video_job.py).

LÍMITE DEL MECANISMO: este barrido corre COMO UNA TAREA MÁS del worker, y su
trabajo es detectar que un worker murió. O sea que solo funciona mientras
quede algún worker vivo para ejecutarlo. Cubre el caso que motivó escribirlo
(un hijo del pool prefork muere por OOM y sus hermanos siguen consumiendo la
cola), pero si el servicio worker cae entero, Beat sigue publicando barridos
que nadie ejecuta: se acumulan en la cola y corren recién cuando vuelva un
worker. Esa caída total la tiene que detectar el monitoreo del proveedor.
"""

from datetime import datetime, timedelta, timezone

from app.constants.job_constants import JobStatus
from app.core.callback_queue import enqueue_callback
from app.core.config import settings
from app.core.job_logging import log_job_event
from app.dtos.error_success_code import ErrorAndSuccessCodes
from app.utils.db_connect import StuckJob, fail_stuck_jobs
from app.utils.video_storage import delete_video
from app.utils.logger import get_logger

logger = get_logger("stuck_jobs")


async def sweep_stuck_jobs() -> list[str]:
    """
    Cierra los jobs colgados en cualquiera de los dos estados y devuelve sus ids.

    El orden importa poco, pero se barre PROCESSING primero porque es el caso
    más frecuente y el de umbral más corto.
    """
    closed = await _sweep(
        status=JobStatus.PROCESSING,
        timeout_minutes=settings.STUCK_JOB_TIMEOUT_MINUTES,
        error=ErrorAndSuccessCodes.PROCESSING_TIMEOUT.value,
        event="job_timed_out",
    )
    closed += await _sweep(
        status=JobStatus.QUEUED,
        timeout_minutes=settings.STUCK_JOB_QUEUED_TIMEOUT_MINUTES,
        error=ErrorAndSuccessCodes.QUEUED_TIMEOUT.value,
        event="job_never_started",
    )
    return closed


async def _sweep(status: JobStatus, timeout_minutes: int, error: str, event: str) -> list[str]:
    """
    Un barrido sobre un estado. Emite un evento por cada job cerrado: es el
    único rastro que queda, porque del lado del worker no hubo excepción que
    registrar.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=timeout_minutes)
    jobs = await fail_stuck_jobs(status.value, cutoff, error)

    for job in jobs:
        log_job_event(
            event,
            job.id,
            outcome="error",
            error=error,
            timeout_minutes=timeout_minutes,
        )
        _notify_platform(job)
        await _discard_video(job)

    if jobs:
        logger.warning(
            "Barrido: %d job(s) cerrados en %s por superar %d minutos",
            len(jobs),
            status.value,
            timeout_minutes,
        )

    return [job.id for job in jobs]


def _notify_platform(job: StuckJob) -> None:
    """
    Encola el aviso de fallo hacia `resultsEndpoint`.

    Va por la misma tarea de entrega que usa un job normal
    (app/platforms/render/worker.py::send_callback_task), así que hereda su política de
    reintentos y deja registrado el desenlace en las columnas `callback_*`. El
    payload sale de la fila, que `fail_stuck_jobs` ya dejó con el código de
    error y el aviso en `pending`. No se envía desde acá directamente porque
    el barrido tiene que seguir cerrando los demás jobs aunque la plataforma
    esté caída.

    Un fallo al encolar se registra pero no se propaga (ver
    app/core/callback_queue.py): la fila YA quedó cerrada correctamente, y el
    barrido tiene que seguir con los jobs que faltan.
    """
    if not job.results_endpoint:
        # Un job sin endpoint no tiene a quién avisarle. No debería pasar (la
        # API lo exige al recibir el video), pero la columna admite NULL.
        logger.warning("Job %s cerrado sin results_endpoint: no hay a quién avisar", job.id)
        return

    enqueue_callback(job.id)


async def _discard_video(job: StuckJob) -> None:
    """
    Borra el video del job recién cerrado.

    Un fallo se registra y no se propaga, por la misma razón que el aviso: el
    barrido tiene que seguir con los jobs que faltan. La fila conserva el
    `video_url`, así que el barrido de retención lo vuelve a intentar.
    """
    if not job.video_url:
        return
    try:
        await delete_video(job.video_url)
    except Exception:  # noqa: BLE001
        logger.exception("No se pudo borrar el video del job %s cerrado por el barrido", job.id)
