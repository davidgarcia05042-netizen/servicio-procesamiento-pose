"""
Esquemas de request/response del servicio.

El contrato con la plataforma de telefisioterapia está documentado en
docs/DECISIONES_TECNICAS.md: la plataforma hace POST /video/ (multipart: video + points +
exercise + resultsEndpoint), espera 200 inmediato, y este servicio llama de
vuelta a `resultsEndpoint` cuando termina con
`{error, results: {points}, max_angle, min_angle}`.
"""

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from app.constants.job_constants import CallbackStatus, JobStatus
from app.dtos.error_success_code import ErrorAndSuccessCodes


class VideoProcessingResponse(BaseModel):
    """Respuesta inmediata (200) al POST /video/ -- el procesamiento sigue en background."""

    status: str
    internal_status_code: ErrorAndSuccessCodes
    job_id: Optional[str] = None


class JobDetailResponse(BaseModel):
    """Consulta puntual del estado de un job (para depuración/monitoreo, no parte del contrato)."""

    job_id: str
    status: JobStatus
    exercise: Optional[str] = None
    error: Optional[str] = None
    max_angle: Optional[float] = None
    min_angle: Optional[float] = None
    pose_model: Optional[str] = None
    processing_seconds: Optional[float] = None

    # Desenlace del aviso a la plataforma, aparte de `status`: es lo que
    # permite recuperar a mano un resultado que se calculó bien pero que la
    # plataforma nunca llegó a recibir.
    callback_status: Optional[CallbackStatus] = None
    callback_attempts: Optional[int] = None
    callback_error: Optional[str] = None
    callback_next_attempt_at: Optional[datetime] = None


class CallbackResults(BaseModel):
    """Campo `results` del payload de vuelta hacia la plataforma."""

    points: list[dict[str, Any]] = Field(default_factory=list)


class CallbackPayload(BaseModel):
    """
    Payload exacto que se envía a `resultsEndpoint` al terminar.

    Forma fijada por el contrato de la plataforma (ver docs/DECISIONES_TECNICAS.md):
    `{error, results: {points}, max_angle, min_angle}`.
    """

    error: Optional[str] = None
    results: CallbackResults = Field(default_factory=CallbackResults)
    max_angle: Optional[float] = None
    min_angle: Optional[float] = None


class CallbackResultsV2(BaseModel):
    """
    Campo `results` del callback v2 (docs/DECISIONES_TECNICAS.md de la plataforma, 3.2).

    `points` va siempre vacío: es el campo de frames de v1 y se conserva para
    que un consumidor de v1 no reviente al leerlo. Lo de v2 está en `joints`
    (una entrada por terna) y `frames` (33 landmarks + ángulos por terna).
    """

    points: list[dict[str, Any]] = Field(default_factory=list)
    fps: Optional[float] = None
    duration_s: Optional[float] = None
    joints: list[dict[str, Any]] = Field(default_factory=list)
    frames: list[dict[str, Any]] = Field(default_factory=list)


class CallbackPayloadV2(BaseModel):
    """
    Callback para una solicitud v2 (lista de ternas). Un DTO aparte y no campos
    nuevos en `CallbackPayload`: una solicitud v1 debe recibir EXACTAMENTE el
    callback de siempre, sin `contract_version` ni claves extra.

    `max_angle`/`min_angle` de primer nivel van siempre en null: los valores
    están por articulación en `results.joints`. En error: `error` lleno y
    `joints`/`frames` vacíos, como en v1.
    """

    error: Optional[str] = None
    contract_version: Literal[2] = 2
    max_angle: Optional[float] = None
    min_angle: Optional[float] = None
    results: CallbackResultsV2 = Field(default_factory=CallbackResultsV2)
