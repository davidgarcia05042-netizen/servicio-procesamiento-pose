"""
Endpoints del servicio.

`POST /video/` es el contrato con la plataforma de telefisioterapia: recibe el
video y responde 200 de inmediato con un job_id; el resultado real se entrega
después por webhook a `resultsEndpoint`.
"""

import json

from fastapi import APIRouter, File, Form, HTTPException, Path, Request, UploadFile

from app.constants.job_constants import CallbackStatus, JobStatus
from app.core.rate_limit import limiter, video_rate_limit
from app.core.video_processing_service import process_video
from app.dtos.error_success_code import ErrorAndSuccessCodes
from app.dtos.video_processing_dtos import JobDetailResponse, VideoProcessingResponse
from app.utils.db_connect import get_job
from app.utils.logger import get_logger

logger = get_logger("api")

router = APIRouter(tags=["VideoProcessing"])


@router.post("/video/", response_model=VideoProcessingResponse)
@limiter.limit(video_rate_limit)
async def receive_video(
    request: Request,  # noqa: ARG001 -- lo exige slowapi para identificar al cliente
    video: UploadFile = File(...),
    points: str = Form(...),
    exercise: str = Form(...),
    resultsEndpoint: str = Form(...),  # noqa: N803 -- nombre fijado por el contrato de la plataforma
) -> VideoProcessingResponse:
    """
    Recibe un video para análisis de pose y lo encola.

    `points`: JSON con los índices de landmark de MediaPipe a medir.
    v1: una terna, ej. "[24, 26, 28]" (cadera, rodilla, tobillo derechos).
    v2: lista de 1 a 12 ternas, ej. "[[23, 25, 27], [24, 26, 28]]"; recibe el
    callback v2 (ver docs/DECISIONES_TECNICAS.md, "Contrato v2").
    """
    # Sin el nombre del archivo: puede identificar al paciente (ver
    # `new_video_path` en app/utils/video_storage.py).
    logger.info("Video recibido (ejercicio: %s, tipo: %s)", exercise, video.content_type)

    try:
        parsed_points = json.loads(points)
    except json.JSONDecodeError:
        return VideoProcessingResponse(
            status="error", internal_status_code=ErrorAndSuccessCodes.INVALID_POINTS
        )

    if not isinstance(parsed_points, list):
        return VideoProcessingResponse(
            status="error", internal_status_code=ErrorAndSuccessCodes.INVALID_POINTS
        )

    result = await process_video(
        video_file=video,
        points=parsed_points,
        exercise=exercise,
        results_endpoint=resultsEndpoint,
    )

    if result.get("job_id") is None:
        return VideoProcessingResponse(status="error", internal_status_code=result["status"])

    return VideoProcessingResponse(
        status="ok",
        internal_status_code=result["status"],
        job_id=result["job_id"],
    )


@router.get("/jobs/{job_id}", response_model=JobDetailResponse)
async def get_job_detail(job_id: str = Path(...)) -> JobDetailResponse:
    """
    Estado de un job. No es parte del contrato con la plataforma (que recibe
    todo por webhook); existe para monitoreo y depuración.
    """
    job = await get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job_id no encontrado")

    return JobDetailResponse(
        job_id=job.id,
        status=JobStatus(job.status),
        exercise=job.exercise,
        error=job.error,
        max_angle=job.max_angle,
        min_angle=job.min_angle,
        pose_model=job.pose_model,
        processing_seconds=job.processing_seconds,
        callback_status=CallbackStatus(job.callback_status) if job.callback_status else None,
        callback_attempts=job.callback_attempts,
        callback_error=job.callback_error,
        callback_next_attempt_at=job.callback_next_attempt_at,
    )
