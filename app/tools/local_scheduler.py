"""
Cloud Scheduler para el camino de Google en local (docker-compose.gcp.yml).

Cloud Scheduler no tiene emulador. Esto cumple su papel con lo que sí hay:
cada vez que vence una de las tareas periódicas (app/platforms/gcp/schedule.py)
crea una tarea en el emulador de Cloud Tasks hacia POST /cron/<nombre> del
worker, y el emulador se la entrega firmada con su token. El worker la recibe
y la verifica igual que si viniera de Cloud Scheduler.

Igual que Celery Beat, dispara todas las tareas al arrancar y después cada
una a su frecuencia.

SOLO PARA DESARROLLO: no arranca sin CLOUD_TASKS_EMULATOR_HOST.

    python -m app.tools.local_scheduler
"""

import sys
import time
from typing import Callable

from google.cloud import tasks_v2
from google.protobuf import duration_pb2

from app.platforms.gcp import cloud_tasks
from app.platforms.gcp.config import gcp_settings
from app.platforms.gcp.schedule import ScheduledJob, scheduled_jobs
from app.utils.logger import get_logger

logger = get_logger("local_scheduler")

# Cola del emulador por la que pasan las llamadas (ver docker-compose.gcp.yml).
QUEUE = "pose-cron"

# Cada cuánto mira si venció alguna, como máximo.
_TICK_SECONDS = 5.0


def enqueue(job: ScheduledJob) -> None:
    worker_url = gcp_settings.worker_url
    task = tasks_v2.Task(
        http_request=tasks_v2.HttpRequest(
            http_method=tasks_v2.HttpMethod.POST,
            url=f"{worker_url}{job.path}",
            oidc_token=tasks_v2.OidcToken(
                service_account_email=gcp_settings.TASKS_SERVICE_ACCOUNT,
                audience=worker_url,
            ),
        ),
        dispatch_deadline=duration_pb2.Duration(seconds=job.attempt_deadline_seconds),
    )
    cloud_tasks._get_client().create_task(parent=cloud_tasks.queue_path(QUEUE), task=task)
    logger.info("Tarea periódica disparada: %s", job.name)


def run(
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    rounds: int | None = None,
) -> None:
    """Dispara las tareas que vencen. `rounds` limita las vueltas (para tests)."""
    jobs = scheduled_jobs()
    due = {job.name: clock() for job in jobs}
    done = 0
    while rounds is None or done < rounds:
        now = clock()
        for job in jobs:
            if due[job.name] <= now:
                try:
                    enqueue(job)
                except Exception:  # noqa: BLE001 -- un fallo no detiene las demás
                    logger.exception("No se pudo disparar %s", job.name)
                due[job.name] = now + job.every_seconds
        done += 1
        sleep(max(0.0, min(_TICK_SECONDS, min(due.values()) - clock())))


def main() -> int:
    if not gcp_settings.CLOUD_TASKS_EMULATOR_HOST:
        print("Solo para desarrollo: define CLOUD_TASKS_EMULATOR_HOST (ver docker-compose.gcp.yml)")
        return 1
    for job in scheduled_jobs():
        logger.info("%s: cada %d s (%s)", job.name, job.every_seconds, job.cron)
    run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
