"""
Plataforma Render: Celery + Redis para la cola, Celery Beat para las tareas
periódicas, y para el video la misma base de Render (desde el 2026-10-08),
Supabase Storage o Firebase. Blueprint en deploy/render/render.yaml.

Implementa la interfaz descrita en app/platforms/__init__.py.

Los imports de Celery van adentro de cada función: la API arranca sin tener
que construir la app de Celery hasta el primer encolado, y quien importe este
paquete solo para `is_private_network_host` (la configuración de la base) no
arrastra nada de la cola. Lo mismo con el almacenamiento: elegir la base o
Supabase no carga firebase-admin.
"""

from types import ModuleType
from typing import Awaitable, Callable

from app.platforms.render.config import is_render_internal_host, render_settings


def enqueue_video_processing(job_id: str) -> None:
    from app.platforms.render.worker import process_video_task

    process_video_task.delay(job_id)


def enqueue_callback(job_id: str) -> None:
    from app.platforms.render.worker import send_callback_task

    send_callback_task.delay(job_id)


def is_private_network_host(hostname: str | None) -> bool:
    """La base de Render ("dpg-...", sin dominio) va por su red privada."""
    return is_render_internal_host(hostname)


def health_checks() -> dict[str, Callable[[], Awaitable[str]]]:
    """Además de la base: el broker, sin el cual la API no puede encolar nada."""
    from app.platforms.render.health import check_queue

    return {"queue": check_queue}


async def close() -> None:
    from app.platforms.render.health import close_probes

    await close_probes()


def video_storage() -> ModuleType:
    """La base de Render, Supabase Storage o Firebase, según STORAGE_BACKEND (ver config.py)."""
    if render_settings.STORAGE_BACKEND == "postgres":
        from app.platforms.render import postgres_storage

        return postgres_storage

    if render_settings.STORAGE_BACKEND == "supabase":
        from app.platforms.render import supabase_storage

        return supabase_storage

    from app.platforms.render import firebase_storage

    return firebase_storage
