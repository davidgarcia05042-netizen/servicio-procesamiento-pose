"""
Worker de la plataforma Google Cloud: el servicio de Cloud Run al que Cloud
Tasks le entrega las tareas por HTTP (ver cloud_tasks.py, acá al lado), y al
que Cloud Scheduler llama para las tareas periódicas (ver schedule.py).

Comando (la misma imagen que la API, etapa `gcp` del Dockerfile):
  uvicorn app.platforms.gcp.worker:app --host 0.0.0.0 --port $PORT

Cada endpoint es un envoltorio de una línea sobre una función async de la parte
común, igual que las tareas de Celery en Render.

QUÉ RESPONDE, Y POR QUÉ: Cloud Tasks reintenta toda tarea que no reciba un 2xx
dentro del plazo. Por eso:

  - Un video que FALLÓ (corrupto, sin una persona) responde 200: el desenlace
    ya quedó en la base y el aviso a la plataforma, encolado. Reintentarlo
    daría el mismo resultado.
  - Solo un error que la parte común no alcanzó a manejar responde 500, y ahí
    reintentar es lo correcto. Si el worker muere a mitad de un video, Cloud
    Tasks no recibe respuesta y también reintenta: es el equivalente de
    `task_acks_late` en Render, y la parte común descarta la re-entrega si el
    job ya se cerró (ver app/core/video_job.py).

SIN CELERY NO HACE FALTA EL LOOP PERSISTENTE de Render: uvicorn ya corre un
único event loop por proceso durante toda la vida del servicio, y la base se
conecta dentro de él al arrancar.

UN VIDEO POR INSTANCIA: el análisis con MediaPipe bloquea el event loop
mientras corre (en Render pasa igual, y ahí es inocuo). Por eso el worker se
despliega con concurrencia 1 en Cloud Run: cada instancia procesa un video a
la vez, y el paralelismo viene de tener varias instancias, que es lo que
limita la cola de videos (ver README, "La cola en Cloud Tasks").
"""

from contextlib import asynccontextmanager
from importlib import import_module
from typing import Any, Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from app.core.job_logging import log_job_event
from app.platforms.gcp import cloud_tasks, schedule
from app.platforms.gcp.task_auth import require_cloud_tasks
from app.utils import video_storage
from app.utils.db_connect import database
from app.utils.logger import get_logger

logger = get_logger("worker")


@asynccontextmanager
async def lifespan(app: FastAPI):
    video_storage.ensure_configured()
    await database.connect()
    logger.info("Worker de Google Cloud listo: PostgreSQL conectado")
    yield
    await database.disconnect()
    # El worker también encola (los avisos tras procesar un video).
    cloud_tasks.close()


# Sin documentación interactiva: el worker no lo llama nadie más que Cloud Tasks.
app = FastAPI(
    title="Worker del servicio de pose (Google Cloud)",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    dependencies=[Depends(require_cloud_tasks)],
)


class TaskRequest(BaseModel):
    job_id: str = Field(min_length=1, max_length=64)


def _log_delivery(kind: str, job_id: str, retry_count: Optional[str], queue: Optional[str]) -> None:
    """
    Deja constancia de cada entrega. Un `retry_count` mayor que 0 es una
    re-entrega: el worker anterior murió o no respondió a tiempo, que es justo
    lo que conviene poder buscar en el log.
    """
    log_job_event("task_received", job_id, task=kind, queue=queue, retry_count=retry_count)


@app.post(cloud_tasks.VIDEO_TASK_PATH)
async def process_video(
    body: TaskRequest,
    x_cloudtasks_taskretrycount: Optional[str] = Header(default=None),
    x_cloudtasks_queuename: Optional[str] = Header(default=None),
) -> dict[str, str]:
    """Ver app/core/video_job.py."""
    from app.core.video_job import process_video_job

    _log_delivery("process_video", body.job_id, x_cloudtasks_taskretrycount, x_cloudtasks_queuename)
    return {"job_id": body.job_id, "status": await process_video_job(body.job_id)}


@app.post(cloud_tasks.CALLBACK_TASK_PATH)
async def send_callback(
    body: TaskRequest,
    x_cloudtasks_taskretrycount: Optional[str] = Header(default=None),
    x_cloudtasks_queuename: Optional[str] = Header(default=None),
) -> dict[str, str]:
    """
    UN intento de entregar el resultado a la plataforma. No reintenta por su
    cuenta: si falla, deja en la tabla la hora del próximo intento (ver
    app/core/callback_retry.py). Por eso responde 200 aunque la plataforma no
    haya recibido el aviso.
    """
    from app.core.callback_retry import attempt_callback

    _log_delivery("send_callback", body.job_id, x_cloudtasks_taskretrycount, x_cloudtasks_queuename)
    return {"job_id": body.job_id, "status": await attempt_callback(body.job_id)}


@app.post("/cron/{name}")
async def run_scheduled_job(name: str) -> dict[str, Any]:
    """
    Una tarea periódica, disparada por Cloud Scheduler (ver schedule.py).

    Responde con un resumen y no con los ids que devuelven algunas tareas: la
    respuesta queda en el log de Cloud Scheduler, y ahí no hace falta más que
    el conteo. Si falla responde 500 y Cloud Scheduler no reintenta: la
    corrida siguiente vuelve a hacer el trabajo, porque todas las tareas
    periódicas toman lo que encuentran pendiente en la base.
    """
    job = schedule.find(name)
    if job is None:
        raise HTTPException(status_code=404, detail="tarea periódica desconocida")

    module_name, function_name = job.target.split(":")
    function = getattr(import_module(module_name), function_name)
    result = await function()
    summary = len(result) if isinstance(result, list) else result
    logger.info("Tarea periódica %s: %s", name, summary)
    return {"task": name, "result": summary}
