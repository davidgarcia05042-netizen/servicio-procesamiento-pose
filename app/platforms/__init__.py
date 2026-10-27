"""
Plataformas de despliegue: lo que cambia según dónde corre el servicio.

El servicio tiene una parte COMÚN (app/core, app/api, app/utils: validar,
analizar el video, calcular ángulos, avisar a la plataforma, retención) y una
parte que depende de dónde se despliega: dónde espera el video, cómo viaja el
trabajo de la API al worker, quién dispara las tareas periódicas y qué se
sondea en /health.

Cada plataforma vive en su propio paquete y NO se cruzan:

  - app/platforms/render/  Celery + Redis + Celery Beat, y el video en la
                           misma base de Render (o Supabase Storage, o
                           Firebase). Blueprint en deploy/render/render.yaml.
  - app/platforms/gcp/     Google Cloud. En construcción: por ahora solo Cloud
                           Storage, y todavía no se puede elegir con PLATFORM.

La parte común nunca importa un paquete de plataforma directamente: pide la
activa con `current()`, que la elige según PLATFORM. Un test lo comprueba
(tests/test_platform_boundaries.py).

LO QUE CADA PLATAFORMA EXPONE (las funciones de su `__init__.py`):

  enqueue_video_processing(job_id) -> None
      Encola el procesamiento de un video. Lanza si no pudo.
  enqueue_callback(job_id) -> None
      Encola UN intento de entrega del aviso a la plataforma. Lanza si no pudo.
  is_private_network_host(hostname) -> bool
      Si una base en ese host se alcanza por la red privada del proveedor (y
      por eso no se le exige TLS en producción).
  health_checks() -> dict[str, Callable[[], Awaitable[str]]]
      Sondeos propios de la plataforma para /health, además de la base.
  close() -> Awaitable[None]
      Libera lo que la plataforma haya abierto (al apagar la API).
  video_storage() -> ModuleType
      El almacenamiento del video. La parte común lo usa por
      app/utils/video_storage.py, que decide la ruta de cada video; el módulo
      devuelto solo guarda, baja y borra bytes:

        ensure_configured() -> None
            Lanza si falta configuración (lo llaman la API y el worker al
            arrancar). No hace llamadas de red.
        async upload_video(content, path, content_type) -> None
            Guarda en `path`. La ruta llega única (lleva un uuid), así que
            reintentar una subida es seguro: a lo sumo reescribe los mismos
            bytes.
        async download_video(path, destination) -> None
            Lo baja al archivo `destination`. Lanza si no existe.
        async delete_video(path) -> bool
            False si ya no estaba: no es un error (el barrido de retención
            puede pasar después de que el worker lo borró).
"""

from importlib import import_module
from types import ModuleType

from app.core.config import settings


def current() -> ModuleType:
    """El paquete de la plataforma configurada en PLATFORM."""
    return import_module(f"app.platforms.{settings.PLATFORM}")
