"""
Orquestación del pre-procesamiento (equivalente al video_processing_service.py
del repo de referencia, sin rate limiting por usuario ni consultas por usuario
-- acá no hay usuarios finales, solo la plataforma cliente).

Pasos del POST /video/:
  1. Valida el request (points, resultsEndpoint)
  2. Lee el video a memoria y lo valida AHÍ (tipo, tamaño, magic bytes)
     -- si algo falla, se rechaza sin tocar el almacenamiento
  3. Decide la ruta del video y crea el job en PostgreSQL (estado QUEUED)
     con la ruta y los puntos pedidos, en una sola escritura
  4. Sube el video a esa ruta en el almacenamiento de la plataforma
  5. Encola el procesamiento y devuelve el job_id

POR QUÉ LA RUTA SE ESCRIBE ANTES DE SUBIR: antes el orden era subir y después
anotar la ruta en el job. Si el proceso moría entre las dos cosas, el video
quedaba guardado sin ninguna fila que lo nombrara: ni el barrido de retención
ni ningún otro podía encontrarlo, y nada lo borraba nunca (Ley 1581). Con la
ruta escrita primero, todo video guardado tiene un job que lo nombra, y los
barridos lo alcanzan. Además se ahorra una escritura a la base.
"""

import asyncio
import os
import uuid
from typing import Any, Optional

from fastapi import UploadFile

from app.constants.job_constants import JobStatus
from app.core.config import settings
from app.core.job_logging import log_job_event
from app.core import task_queue
from app.dtos.error_success_code import ErrorAndSuccessCodes
from app.utils.db_connect import insert_job, update_job
from app.utils.video_storage import delete_video, new_video_path, upload_video
from app.utils.logger import get_logger
from app.utils.request_validations import (
    CONTRACT_V2,
    as_triplets,
    contract_version,
    validate_content_type,
    validate_points,
    validate_results_endpoint,
    validate_video_bytes,
    validate_video_is_decodable,
)

logger = get_logger("video_processing_service")


class TaskQueue:
    """Encapsula el encolado para poder sustituirlo en tests."""

    def process_video(self, job_id: str) -> None:
        task_queue.enqueue_video_processing(job_id)


async def process_video(
    video_file: UploadFile,
    points: list,
    exercise: str,
    results_endpoint: str,
    queue: Optional[TaskQueue] = None,
) -> dict[str, Any]:
    queue = queue or TaskQueue()
    job_id: Optional[str] = None
    blob_path: Optional[str] = None
    # Desde que se intenta subir: ver el comentario del borrado, abajo.
    upload_attempted = False

    try:
        for check in (
            validate_points(points),
            validate_results_endpoint(results_endpoint),
            validate_content_type(video_file.content_type),
        ):
            if check != ErrorAndSuccessCodes.SUCCESS:
                logger.info("Request rechazado en validación: %s", check.value)
                log_job_event("video_rejected", exercise=exercise, reason=check.value)
                return {"status": check}

        # Validación EN MEMORIA antes de gastar una subida al almacenamiento.
        content = await video_file.read()
        bytes_check = validate_video_bytes(content)
        if bytes_check != ErrorAndSuccessCodes.SUCCESS:
            logger.info("Video rechazado en validación de contenido: %s", bytes_check.value)
            log_job_event("video_rejected", exercise=exercise, reason=bytes_check.value)
            return {"status": bytes_check}

        # Recién ahora se paga el costo de escribir a disco y decodificar: las
        # validaciones baratas ya descartaron la basura evidente.
        # `to_thread` porque esto bloquea y corre en el event loop de uvicorn.
        suffix = os.path.splitext(video_file.filename or "video.mp4")[1] or ".mp4"
        decodable_check = await asyncio.to_thread(validate_video_is_decodable, content, suffix)
        if decodable_check != ErrorAndSuccessCodes.SUCCESS:
            logger.info("Video rechazado en validación de decodificación: %s", decodable_check.value)
            log_job_event("video_rejected", exercise=exercise, reason=decodable_check.value)
            return {"status": decodable_check}

        job_id = str(uuid.uuid4())
        blob_path = new_video_path(video_file.filename or "video.mp4")
        # `requested_points` viaja en el JSONB para que el worker sepa qué
        # articulación medir sin necesidad de otra columna dedicada.
        intake: dict[str, Any] = {"requested_points": points}
        if contract_version(points) == CONTRACT_V2:
            # Solo se marca v2: un job v1 guarda exactamente lo de siempre, y la
            # ausencia de la marca significa v1 (filas viejas incluidas). De esta
            # marca dependen el análisis del worker y la forma del callback.
            intake = {"requested_points": as_triplets(points), "contract_version": CONTRACT_V2}
        await insert_job(
            job_id=job_id,
            exercise=exercise,
            results_endpoint=results_endpoint,
            pose_model=settings.POSE_MODEL,
            video_url=blob_path,
            results=intake,
        )

        upload_attempted = True
        await upload_video(content, blob_path, video_file.content_type or "video/mp4")

        # `to_thread` por lo mismo que la decodificación: encolar es E/S de red
        # bloqueante (Celery hacia Redis en Render, el cliente de Cloud Tasks en
        # Google). Con la cola sana tarda milisegundos, pero con el broker caído
        # Celery reintenta la publicación durante segundos, y en el event loop
        # eso congelaba la API entera, /health incluido, no solo esta solicitud.
        await asyncio.to_thread(queue.process_video, job_id)
        logger.info("Job %s encolado (ejercicio: %s)", job_id, exercise)
        log_job_event(
            "video_queued",
            job_id,
            exercise=exercise,
            pose_model=settings.POSE_MODEL,
            size_bytes=len(content),
        )

        return {"job_id": job_id, "status": ErrorAndSuccessCodes.SUCCESS}

    except Exception:  # noqa: BLE001
        logger.exception("Error preparando el procesamiento del video")
        # El borrado va primero: si la base también está caída, el update de
        # abajo revienta, y el video no puede quedar dependiendo de eso.
        #
        # Se intenta también si la SUBIDA falló: una subida puede fallar del
        # lado de acá (se cortó la respuesta, venció el plazo) después de que
        # el almacenamiento ya guardó el video. Borrar algo que no está es
        # inocuo (devuelve False); dejar un video guardado sin finalidad, no.
        if upload_attempted and blob_path:
            await _discard_uploaded_video(blob_path)
        if job_id:
            await update_job(job_id, status=JobStatus.FAILED.value, error="error en la recepción del video")
        log_job_event("video_intake_failed", job_id, exercise=exercise)
        return {"status": ErrorAndSuccessCodes.PROCESSING_ERROR}


async def _discard_uploaded_video(blob_path: str) -> None:
    """
    Borra un video que se subió (o pudo haberse subido) pero cuyo job no llegó
    a encolarse.

    Pasa si falla la subida misma o lo que viene después (típicamente, el
    broker no responde al encolar). El job queda en FAILED y nadie va a procesar ese
    video, así que conservarlo no tiene finalidad (Ley 1581, principio de
    temporalidad). Sin esto, quedaba hasta que el barrido de retención lo
    borrara, 30 días después.

    Un fallo acá no cambia nada de lo anterior: el barrido de retención sigue
    siendo la red, porque la fila conserva el `video_url`.
    """
    try:
        await delete_video(blob_path)
    except Exception:  # noqa: BLE001
        logger.exception("No se pudo borrar el video de una recepción fallida: %s", blob_path)
