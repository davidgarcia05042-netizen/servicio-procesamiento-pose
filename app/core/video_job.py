"""
Procesamiento de un video: el trabajo que el worker hace por cada job.

Es común a todas las plataformas: en Render lo ejecuta una tarea de Celery
(app/platforms/render/worker.py). Antes se llamaba celery_core.py, como el
módulo equivalente del repo de referencia (sin la parte de miniaturas).

Flujo:
  1. Lee el job de PostgreSQL y lo marca PROCESSING
  2. Descarga el video del almacenamiento a un archivo temporal
  3. Corre MediaPipe y calcula los ángulos
  4. Guarda resultados (JSONB) + max/min y marca DONE
  5. Encola el webhook de vuelta a `resultsEndpoint` como tarea aparte, que
     reintenta por su cuenta (ver app/core/callback_delivery.py). Va en una
     tarea aparte porque si el envío viviera acá, un reintento volvería a
     descargar el video y a correr MediaPipe sobre un resultado ya guardado
  6. Limpia el archivo temporal pase lo que pase

TOMAR Y CERRAR EL JOB VAN CONDICIONADOS A QUE SIGA ABIERTO (`update_open_job`
en app/utils/db_connect.py). Mientras el worker procesa, otro puede cerrar el
mismo job: el barrido de jobs colgados (que además ya le avisó del fallo a la
plataforma) u otra entrega de la misma tarea. Si eso pasó, este worker no
escribe su desenlace ni encola otro aviso: la plataforma recibiría dos
mensajes, y quizá contradictorios, por el mismo job.

En Render, esta función NUNCA se llama con asyncio.run() desde la tarea:
corre sobre el loop persistente del worker (ver app/platforms/render/worker.py).
"""

import os
import shutil
import tempfile
import time
from typing import Any, Optional

from app.constants.job_constants import CallbackStatus, JobStatus
from app.core.callback_queue import enqueue_callback
from app.core.config import settings
from app.core.job_logging import log_job_event
from app.core.pose_analysis import analyze_video, analyze_video_v2
from app.utils.db_connect import get_job, update_open_job
from app.utils.request_validations import CONTRACT_V2
from app.utils.video_storage import delete_video, download_video
from app.utils.logger import get_logger

logger = get_logger("video_job")


async def process_video_job(job_id: str) -> str:
    job = None
    work_dir: Optional[str] = None
    results_endpoint: Optional[str] = None
    started_at: Optional[float] = None
    # Se llena recién cuando el job queda cerrado: hasta entonces el video
    # todavía puede hacer falta.
    video_to_discard: Optional[str] = None

    try:
        job = await get_job(job_id)
        if job is None or not job.video_url:
            logger.error("Job no encontrado o sin video en almacenamiento: %s", job_id)
            return JobStatus.FAILED.value

        # NO reprocesar un job ya cerrado.
        #
        # Con `task_acks_late` (ver app/platforms/render/worker.py), si el worker muere sin
        # confirmar la tarea, Celery la vuelve a entregar cuando vence el
        # visibility_timeout de Redis. Eso es deseable cuando el job sigue en
        # PROCESSING: es justo la recuperación que esa opción busca.
        #
        # Pero si mientras tanto el barrido ya lo cerró (app/core/stuck_jobs.py
        # actúa a los 15 minutos y el visibility_timeout es de una hora, así
        # que el barrido gana siempre), reprocesarlo terminaría
        # enviándole a la plataforma un callback de éxito DESPUÉS de haberle
        # enviado uno de fallo por el mismo job. Dos mensajes contradictorios
        # con horas de diferencia.
        #
        # También cubre el caso de un job en DONE cuya confirmación se perdió:
        # sin esta guarda se volvería a procesar el video entero y la
        # plataforma recibiría el resultado dos veces.
        if job.status in JobStatus.terminal():
            return _discard_delivery(job_id, job.status)

        results_endpoint = job.results_endpoint
        points: list = list((job.results or {}).get("requested_points", []))
        # Sin la marca es v1 (también las filas creadas antes del contrato v2).
        is_v2 = (job.results or {}).get("contract_version") == CONTRACT_V2

        # Si alguien lo cerró entre la lectura de arriba y acá, no se toma.
        if not await update_open_job(job_id, status=JobStatus.PROCESSING.value):
            return _discard_delivery(job_id, _CLOSED_ELSEWHERE)

        # El cronómetro arranca acá: mide el trabajo real (descarga + análisis)
        # y deja afuera la espera en cola, que es lo que contaminaría la resta
        # de updated_at - created_at.
        started_at = time.perf_counter()

        work_dir = tempfile.mkdtemp(prefix=f"job_{job_id}_")
        local_path = os.path.join(work_dir, os.path.basename(job.video_url))
        await download_video(job.video_url, local_path)

        if is_v2:
            # Una sola pasada para todas las ternas. Max/min van por
            # articulación dentro de `joints`; las columnas de primer nivel
            # quedan en NULL, como el callback v2 (nadie las confunde con una
            # articulación concreta).
            analysis: dict[str, Any] = analyze_video_v2(local_path, points)
            results: dict[str, Any] = {
                "requested_points": points,
                "contract_version": CONTRACT_V2,
                **analysis,
            }
            max_angle = min_angle = None
            frame_count = len(analysis["frames"])
        else:
            analysis = analyze_video(local_path, points)
            results = {"requested_points": points, "points": analysis["points"]}
            max_angle, min_angle = analysis["max_angle"], analysis["min_angle"]
            frame_count = len(analysis["points"])

        processing_seconds = round(time.perf_counter() - started_at, 3)

        closed = await update_open_job(
            job_id,
            status=JobStatus.DONE.value,
            results=results,
            max_angle=max_angle,
            min_angle=min_angle,
            error=None,
            processing_seconds=processing_seconds,
            # Viaja en la MISMA escritura que el cambio de estado, igual que
            # processing_seconds: dejar marcado que hay un aviso pendiente no
            # debe costar una consulta extra.
            callback_status=CallbackStatus.PENDING.value if results_endpoint else None,
        )
        # El job quedó cerrado (por este worker o por otro): el video ya
        # cumplió su finalidad.
        video_to_discard = job.video_url
        if not closed:
            return _discard_result(job_id, outcome="success")

        log_job_event(
            "job_processed",
            job_id,
            outcome="success",
            pose_model=settings.POSE_MODEL,
            processing_seconds=processing_seconds,
            frames=frame_count,
        )

        if results_endpoint:
            enqueue_callback(job_id)

        return JobStatus.DONE.value

    except Exception as exc:  # noqa: BLE001 -- la tarea no debe tumbar al worker
        if job is None:
            # Falló la lectura misma del job (típicamente la base de datos no
            # respondió), así que no se sabe a qué endpoint avisar. Marcarlo
            # FAILED acá lo dejaría cerrado SIN aviso, y el barrido de jobs
            # colgados solo mira los que siguen abiertos: la plataforma no se
            # enteraría nunca. Se deja en QUEUED para que ese barrido lo
            # cierre con QUEUED_TIMEOUT y le avise.
            logger.exception("No se pudo leer el job %s: queda para el barrido", job_id)
            return JobStatus.QUEUED.value

        logger.exception("Error procesando el job %s", job_id)

        # Cuánto se alcanzó a trabajar antes de fallar: es justo el dato que se
        # quiere para distinguir un fallo inmediato de uno tras minutos de
        # cómputo. Puede no haber arrancado si el fallo fue antes del análisis.
        failed_after = round(time.perf_counter() - started_at, 3) if started_at else None

        closed = await update_open_job(
            job_id,
            status=JobStatus.FAILED.value,
            error=str(exc),
            processing_seconds=failed_after,
            callback_status=CallbackStatus.PENDING.value if results_endpoint else None,
        )
        # También cuando falla: con la guarda de estado terminal, la tarea no
        # se va a reprocesar, así que el video tampoco sirve ya. Se pierde la
        # posibilidad de inspeccionarlo para diagnosticar, y es un costo
        # aceptado: es el video de un paciente, la plataforma lo conserva, y
        # guardarlo "por si acaso" es justo lo que el principio de
        # temporalidad no permite.
        video_to_discard = job.video_url
        if not closed:
            return _discard_result(job_id, outcome="error")

        log_job_event(
            "job_processed",
            job_id,
            outcome="error",
            pose_model=settings.POSE_MODEL,
            processing_seconds=failed_after,
            error=str(exc),
        )

        # La plataforma queda esperando el callback: se le avisa del error en
        # vez de dejarla colgada. La entrega (y sus reintentos) corre en su
        # propia tarea, así que el job ya quedó cerrado acá pase lo que pase.
        if results_endpoint:
            enqueue_callback(job_id)

        return JobStatus.FAILED.value

    finally:
        if work_dir and os.path.isdir(work_dir):
            shutil.rmtree(work_dir, ignore_errors=True)
            logger.info("Archivos temporales eliminados: %s", work_dir)

        if video_to_discard:
            # Un fallo borrando NO puede cambiar el desenlace del job: los
            # ángulos ya están calculados y guardados, y la plataforma ya tiene
            # su aviso encolado. Queda el barrido de retención como red
            # (ver app/core/data_retention.py), que vuelve a intentarlo.
            try:
                await delete_video(video_to_discard)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "No se pudo borrar el video del job %s: %s", job_id, video_to_discard
                )


# Lo que devuelve la tarea cuando el job lo cerró otro: no es un estado de
# JobStatus, porque este worker no sabe cuál quedó (FAILED si fue el barrido,
# DONE o FAILED si fue otra entrega).
_CLOSED_ELSEWHERE = "closed_elsewhere"


def _discard_delivery(job_id: str, status: str) -> str:
    """
    Descarta una entrega de la tarea para un job que ya está cerrado: no se
    descarga el video, no se procesa, no se toca la fila.
    """
    logger.warning(
        "Job %s ya está cerrado (%s): se descarta la re-entrega de la tarea", job_id, status
    )
    log_job_event("job_already_closed", job_id, status=status)
    return status


def _discard_result(job_id: str, outcome: str) -> str:
    """
    El worker terminó, pero otro cerró el job mientras tanto (el barrido de
    jobs colgados u otra entrega de la tarea). Su desenlace no se escribe y no
    se encola otro aviso: la plataforma ya tiene el de quien cerró primero.
    """
    logger.warning(
        "Job %s cerrado por otro mientras se procesaba: se descarta este resultado (%s)",
        job_id,
        outcome,
    )
    log_job_event("job_result_discarded", job_id, outcome=outcome)
    return _CLOSED_ELSEWHERE
