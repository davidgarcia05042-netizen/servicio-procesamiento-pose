"""
Límite de tasa para el endpoint de recepción de video.

Vive en su propio módulo (y no en main.py) porque lo necesitan dos lados que
no se pueden importar entre sí: `app/api/video_processing.py` usa el decorador
y `app/main.py` registra el limiter y su manejador de excepción. Si el limiter
viviera en main.py, la API tendría que importar main y se cerraría el ciclo.

Por qué el valor por defecto: el cuello de botella real no es la CPU sino la
MEMORIA. El middleware de HMAC bufferiza el cuerpo completo del request para
poder firmarlo (ver app/main.py), así que cada solicitud en vuelo ocupa hasta
MAX_VIDEO_SIZE_MB en RAM antes de que el video llegue siquiera a validarse. En
el hardware de despliegue real (Render: la API en 1c-2g) eso es lo que
acota cuántas solicitudes simultáneas se aguantan, no el procesamiento en sí
-- que es asíncrono y se encola.

Nota sobre la identificación del cliente: `get_remote_address` usa la IP de la
conexión. Detrás del proxy de Render todas las solicitudes llegan con la IP
del proxy, así que en la práctica el límite es global del servicio, no por
cliente. Acá eso es aceptable porque hay UN solo cliente (la plataforma de
telefisioterapia), pero es la razón por la que no sirve como defensa contra un
atacante distribuido.

Nota sobre el almacenamiento: el conteo es en memoria del proceso. Si en
producción se corre uvicorn con varios workers, cada uno lleva su propia
cuenta y el límite efectivo se multiplica. Para un límite real compartido hay
que apuntar `storage_uri` al Redis que ya usa Celery.
"""

from slowapi import Limiter
from slowapi.util import get_remote_address

from app.core.config import settings

limiter = Limiter(key_func=get_remote_address)


def video_rate_limit() -> str:
    """
    Límite configurable en tiempo de ejecución (no se congela en el decorador).

    slowapi acepta un invocable, así que leer `settings` acá permite ajustar el
    valor por variable de entorno sin tocar el código del endpoint.
    """
    return settings.RATE_LIMIT_VIDEO
