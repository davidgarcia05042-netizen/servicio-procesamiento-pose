"""
Plataforma Google Cloud: Cloud Run (API y worker), Cloud Storage, Cloud SQL,
Cloud Tasks y Cloud Scheduler.

Implementa la interfaz descrita en app/platforms/__init__.py:

  - video_storage(): Cloud Storage (etapa 1).
  - is_private_network_host(): la base en Cloud SQL (etapa 2).
  - enqueue_*(): Cloud Tasks hacia el worker en Cloud Run (etapa 3; el worker
    está en worker.py).
  - Las tareas periódicas (barridos, reintentos de avisos, retención y
    resumen de estado): Cloud Scheduler llama al worker (etapa 4; ver
    schedule.py).

Lo que falta para desplegarla (etapa 5) es infraestructura, no código: la
imagen en Artifact Registry, los servicios de Cloud Run y el CI.

Los imports de Google van adentro de cada función, como en Render: quien
importe este paquete solo para la regla de la base no carga el cliente de
Cloud Tasks.
"""

from types import ModuleType
from typing import Awaitable, Callable, Optional


def enqueue_video_processing(job_id: str) -> None:
    from app.platforms.gcp import cloud_tasks

    cloud_tasks.enqueue_video_processing(job_id)


def enqueue_callback(job_id: str) -> None:
    from app.platforms.gcp import cloud_tasks

    cloud_tasks.enqueue_callback(job_id)


def is_private_network_host(hostname: Optional[str]) -> bool:
    """
    Ningún host de red se exime de TLS en Google Cloud.

    La base (Cloud SQL) llega a Cloud Run como un socket Unix en
    /cloudsql/<proyecto>:<región>:<instancia>, y eso ya lo acepta la regla
    común sin pasar por acá (ver `database_url` en app/core/config.py): el
    tramo hasta la base lo cifra el proxy de Cloud SQL. Este servicio no usa
    IP privada ni VPC, así que cualquier URL con un host de red, incluida una
    IP interna de Google, tiene que pedir TLS.
    """
    return False


def health_checks() -> dict[str, Callable[[], Awaitable[str]]]:
    """
    Ninguno además de la base. Cloud Tasks es un servicio administrado: no hay
    una instancia nuestra que sondear, como el Redis de Render. Si no está
    disponible, se nota al encolar: el job queda FAILED y el error, en el log.
    """
    return {}


async def close() -> None:
    from app.platforms.gcp import cloud_tasks

    cloud_tasks.close()


def video_storage() -> ModuleType:
    """Cloud Storage. El import va adentro, igual que en Render."""
    from app.platforms.gcp import cloud_storage

    return cloud_storage
