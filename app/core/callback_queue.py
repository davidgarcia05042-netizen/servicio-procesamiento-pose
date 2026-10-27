"""
Encolado del aviso a la plataforma, y el payload que lleva.

Es el ÚNICO lugar que publica `send_callback_task`: al terminar un job
(video_job), al cerrar uno colgado (stuck_jobs) y al despachar un reintento
cuya hora llegó (callback_retry).

LA TAREA LLEVA SOLO EL `job_id`. El payload se arma desde la fila en cada
intento (`payload_from_job`). Así hay una sola fuente de verdad -- la tabla --
y un reintento no depende de que un mensaje con el payload haya sobrevivido
en Redis.

POR QUÉ UN FALLO AL ENCOLAR NO SE PROPAGA: en todos los casos, quien encola ya
dejó la fila en el estado correcto. Dejar que la excepción subiera marcaría
como fallido un job que no falló, o cortaría un barrido a la mitad por el solo
hecho de que Redis esté caído. Se registra y quien llama decide qué hacer con
el `False`. Si no hace nada, la fila queda en `pending` y el barrido de avisos
perdidos la recupera (ver CALLBACK_STALE_PENDING_HOURS).

Cómo viaja la tarea la decide la plataforma (ver app/core/task_queue.py).
"""

from typing import Any, Optional, Protocol, Union

from app.core import task_queue
from app.dtos.video_processing_dtos import (
    CallbackPayload,
    CallbackPayloadV2,
    CallbackResults,
    CallbackResultsV2,
)
from app.utils.request_validations import CONTRACT_V2
from app.utils.logger import get_logger

logger = get_logger("callback_queue")


class _JobResult(Protocol):
    """Lo que hace falta de una fila de `jobs` para armar el aviso."""

    error: Optional[str]
    results: Optional[dict[str, Any]]
    max_angle: Optional[float]
    min_angle: Optional[float]


def payload_from_job(job: _JobResult) -> Union[CallbackPayload, CallbackPayloadV2]:
    """
    El aviso que se le envía a la plataforma, armado desde la fila.

    Sirve para los dos desenlaces sin ramificar. Un job que falló tiene `error`
    lleno y ni `points` ni ángulos, así que sale exactamente el payload de
    fallo; uno que terminó bien tiene `error` en NULL y sale el de éxito. La
    forma la fija el contrato de la plataforma (ver docs/DECISIONES_TECNICAS.md).

    La forma depende de cómo llegó la solicitud: la marca `contract_version`
    del JSONB la pone la recepción solo para v2. Sin marca (v1, o filas
    anteriores al contrato v2) sale exactamente el payload de siempre.

    `requested_points` NO viaja: es un dato interno que el worker guarda en el
    mismo JSONB para saber qué ángulo medir, y la plataforma ya sabe lo que
    pidió.
    """
    results = job.results or {}
    if results.get("contract_version") == CONTRACT_V2:
        return CallbackPayloadV2(
            error=job.error,
            results=CallbackResultsV2(
                fps=results.get("fps"),
                duration_s=results.get("duration_s"),
                joints=results.get("joints", []),
                frames=results.get("frames", []),
            ),
        )
    return CallbackPayload(
        error=job.error,
        results=CallbackResults(points=results.get("points", [])),
        max_angle=job.max_angle,
        min_angle=job.min_angle,
    )


def enqueue_callback(job_id: str) -> bool:
    """Encola un intento inmediato de entrega. Devuelve si se pudo."""
    try:
        task_queue.enqueue_callback(job_id)
    except Exception:  # noqa: BLE001 -- broker caído: ver el docstring del módulo
        logger.exception("No se pudo encolar el aviso del job %s", job_id)
        return False
    return True
