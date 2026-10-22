"""
Health check para monitoreo (queda fuera de la verificación HMAC).

QUÉ COMPRUEBA Y POR QUÉ ESO IMPORTA: no basta con responder "ok" porque el
proceso esté vivo. Render usa este endpoint para decidir si un despliegue tuvo
éxito, así que un `ok` incondicional deja pasar a producción una versión con la
base mal configurada. No es hipotético: la URL de PostgreSQL que entrega Render
viene sin driver, y `create_async_engine` no abre ninguna conexión al
construirse -- la primera consulta real es la que descubre si las credenciales
y el host sirven. Un `SELECT 1` acá es justamente esa primera consulta.

QUÉ NO COMPRUEBA: Firebase Storage. Es un servicio de terceros al otro lado de
internet, y este endpoint lo consulta Render cada pocos segundos. Incluirlo
significaría que una caída de Google reinicia nuestras instancias -- cosa que
no arregla nada -- y que cada sondeo paga una ida y vuelta hasta Google. Una
falla de Firebase se manifiesta donde corresponde: en el job que la sufre, con
su error y su callback.

CONSECUENCIAS DE DEVOLVER 503 (verificado en la documentación de Render):
  - Durante un despliegue: el despliegue se cancela y la versión anterior sigue
    recibiendo tráfico. Esto es lo que se busca.
  - Con el servicio ya corriendo: a los 15 segundos de fallas consecutivas
    Render deja de enrutarle tráfico, y a los 60 segundos reinicia la
    instancia. O sea que una caída larga de PostgreSQL provoca reinicios. Se
    acepta a conciencia: sin base de datos este servicio no puede aceptar un
    solo video, así que no hay nada que preservar manteniéndolo "sano".
"""

import asyncio

from fastapi import APIRouter, Response
from sqlalchemy import text

from app import platforms
from app.utils.db_connect import database
from app.utils.logger import get_logger

router = APIRouter(tags=["Health"])
logger = get_logger("health")

# Tope por sondeo. Corto a propósito: si una dependencia se cuelga, este
# endpoint tiene que responder igual. Colgarse esperando sería la peor
# respuesta posible, porque Render lo interpreta como falla de todas formas
# pero recién cuando vence SU tiempo de espera, y mientras tanto la conexión
# queda ocupada.
_PROBE_TIMEOUT_SECONDS = 3.0

_OK = "ok"
_ERROR = "error"

async def _check_database() -> str:
    """
    Una consulta trivial contra PostgreSQL.

    El detalle del error NO se devuelve al cliente: los mensajes de SQLAlchemy
    pueden incluir la URL de conexión, y ahí viaja la contraseña. El error
    completo va al log del servicio, que es donde hay que mirarlo.
    """
    try:
        async with asyncio.timeout(_PROBE_TIMEOUT_SECONDS):
            async with database.session() as session:
                await session.execute(text("SELECT 1"))
        return _OK
    except Exception:  # noqa: BLE001 -- cualquier fallo es "no está sana"
        logger.exception("Health check: PostgreSQL no responde")
        return _ERROR


@router.get("/health")
async def health_check(response: Response) -> dict[str, object]:
    """
    200 si todo responde, 503 si algo no.

    La base se comprueba en todas las plataformas; cada plataforma agrega los
    suyos (en Render, el broker de la cola: ver app/platforms/render/health.py).
    Los sondeos van en paralelo: son independientes y el endpoint no tiene
    por qué tardar la suma. El desglose se devuelve siempre, también en el
    caso sano, para que quien mire sepa qué se comprobó y no tenga que leer
    este archivo para averiguarlo.
    """
    probes = {"database": _check_database, **platforms.current().health_checks()}
    results = await asyncio.gather(*(probe() for probe in probes.values()))
    checks = dict(zip(probes, results))

    healthy = all(status == _OK for status in checks.values())
    if not healthy:
        response.status_code = 503
        logger.warning("Health check degradado: %s", checks)

    return {"status": _OK if healthy else "degraded", "checks": checks}
