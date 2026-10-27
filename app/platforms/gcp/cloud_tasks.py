"""
La cola en Google Cloud: Cloud Tasks le entrega cada tarea al worker en Cloud
Run por HTTP (ver worker.py, acá al lado).

Hace el papel que en Render cumplen Redis y Celery. Cada tarea es un POST con
`{"job_id": ...}` al worker, firmado con un token OIDC de la cuenta de servicio
de las tareas (el worker verifica que sea ese y no otro: ver task_auth.py).

Si el worker no responde 2xx dentro del plazo, Cloud Tasks reintenta según la
configuración de la cola. Es el equivalente de `task_acks_late` en Render: un
worker que muere a mitad de un video no pierde la tarea, y la parte común
descarta la re-entrega si el job ya se cerró (ver app/core/video_job.py).

EL CLIENTE ES SÍNCRONO (gRPC). Encolar es una llamada corta y la hace la API
justo después de aceptar un video, igual que `.delay()` de Celery en Render,
que también es síncrono.
"""

import json
import threading
from datetime import timedelta
from typing import Optional

from google.api_core import exceptions
from google.api_core import retry as retries
from google.cloud import tasks_v2
from google.protobuf import duration_pb2

from app.platforms.gcp.config import gcp_settings
from app.utils.logger import get_logger

logger = get_logger("cloud_tasks")

VIDEO_TASK_PATH = "/tasks/process-video"
CALLBACK_TASK_PATH = "/tasks/send-callback"

# Cuánto espera Cloud Tasks la respuesta del worker antes de dar el intento por
# fallido y reintentar. Para un video es el máximo que admite (30 minutos), y
# tiene que superar al barrido de jobs atascados (STUCK_JOB_TIMEOUT_MINUTES):
# si venciera antes, Cloud Tasks reintentaría un video que el worker todavía
# está procesando, y habría dos copias trabajando sobre el mismo job. Hay un
# test que protege esa relación.
VIDEO_DISPATCH_DEADLINE = timedelta(minutes=30)

# Un intento de aviso tarda como mucho el timeout del POST a la plataforma (30
# s, ver app/core/callback_delivery.py) más el arranque del worker si estaba
# apagado.
CALLBACK_DISPATCH_DEADLINE = timedelta(minutes=2)

# Reintenta crear la tarea solo si Cloud Tasks no estaba disponible: ahí la
# solicitud no llegó. Para un video es seguro aunque hubiera llegado, porque la
# tarea lleva nombre (ver `enqueue_video_processing`).
_CREATE_RETRY = retries.Retry(
    predicate=retries.if_exception_type(exceptions.ServiceUnavailable),
    initial=0.5,
    maximum=4.0,
    timeout=15.0,
)

_lock = threading.Lock()
_client: Optional[tasks_v2.CloudTasksClient] = None


def _get_client() -> tasks_v2.CloudTasksClient:
    """Un cliente por proceso (se arma al primer encolado)."""
    global _client
    with _lock:
        if _client is None:
            _client = _emulator_client() if gcp_settings.CLOUD_TASKS_EMULATOR_HOST else tasks_v2.CloudTasksClient()
    return _client


def _emulator_client() -> tasks_v2.CloudTasksClient:
    """Contra el emulador local: gRPC sin TLS ni credenciales. Solo desarrollo."""
    import grpc
    from google.cloud.tasks_v2.services.cloud_tasks.transports import CloudTasksGrpcTransport

    logger.warning(
        "Cola en EMULADOR LOCAL de Cloud Tasks (%s): solo para tests y desarrollo",
        gcp_settings.CLOUD_TASKS_EMULATOR_HOST,
    )
    channel = grpc.insecure_channel(gcp_settings.CLOUD_TASKS_EMULATOR_HOST)
    return tasks_v2.CloudTasksClient(transport=CloudTasksGrpcTransport(channel=channel))


def queue_path(queue: str) -> str:
    return tasks_v2.CloudTasksClient.queue_path(gcp_settings.GCP_PROJECT, gcp_settings.GCP_REGION, queue)


def build_task(path: str, job_id: str, deadline: timedelta, name: Optional[str] = None) -> tasks_v2.Task:
    """La tarea HTTP hacia el worker, firmada con el token OIDC de la cuenta de las tareas."""
    worker_url = gcp_settings.worker_url
    if not worker_url or not gcp_settings.GCP_PROJECT.strip():
        raise RuntimeError("La cola de gcp exige GCP_PROJECT y WORKER_URL.")

    task = tasks_v2.Task(
        http_request=tasks_v2.HttpRequest(
            http_method=tasks_v2.HttpMethod.POST,
            url=f"{worker_url}{path}",
            headers={"Content-Type": "application/json"},
            body=json.dumps({"job_id": job_id}).encode(),
            oidc_token=tasks_v2.OidcToken(
                service_account_email=gcp_settings.TASKS_SERVICE_ACCOUNT,
                audience=worker_url,
            ),
        ),
        dispatch_deadline=duration_pb2.Duration(seconds=int(deadline.total_seconds())),
    )
    if name:
        task.name = name
    return task


def _create(queue: str, task: tasks_v2.Task) -> None:
    try:
        _get_client().create_task(parent=queue_path(queue), task=task, retry=_CREATE_RETRY, timeout=10.0)
    except exceptions.AlreadyExists:
        # Solo pasa con tareas con nombre: un intento anterior de crearla sí
        # llegó. Ya está encolada, que es lo que se quería.
        logger.info("La tarea ya estaba encolada: %s", task.name)


def enqueue_video_processing(job_id: str) -> None:
    """
    La tarea lleva nombre (`video-<job_id>`): Cloud Tasks rechaza un segundo
    intento de crear la misma, así que reintentar la creación no puede
    terminar procesando el video dos veces.
    """
    queue = gcp_settings.TASKS_VIDEO_QUEUE
    name = f"{queue_path(queue)}/tasks/video-{job_id}"
    _create(queue, build_task(VIDEO_TASK_PATH, job_id, VIDEO_DISPATCH_DEADLINE, name=name))


def enqueue_callback(job_id: str) -> None:
    """
    Sin nombre: un job puede necesitar varios intentos de aviso. Una copia de
    más no envía dos veces, porque el intento solo actúa sobre un aviso en
    `pending` (ver `attempt_callback` en app/core/callback_retry.py).
    """
    queue = gcp_settings.TASKS_CALLBACK_QUEUE
    _create(queue, build_task(CALLBACK_TASK_PATH, job_id, CALLBACK_DISPATCH_DEADLINE))


def close() -> None:
    global _client
    with _lock:
        if _client is not None:
            _client.transport.close()
            _client = None
