"""
Retención de datos personales (Ley 1581 de 2012).

QUÉ DATO SE MANEJA ACÁ Y POR QUÉ IMPORTA: un video de una persona haciendo un
ejercicio de rehabilitación, y los ángulos articulares que salen de él, son
datos sensibles bajo el artículo 5 de la Ley 1581 -- de salud y biométricos a
la vez. El principio de temporalidad prohíbe conservarlos más allá de la
finalidad que justificó recogerlos, y esa finalidad acá es calcular los ángulos
y entregárselos a la plataforma.

EL SERVICIO MINIMIZA EN DOS MOMENTOS:

  1. **Al cerrar cada job**, el worker borra el video del almacenamiento
     externo (ver app/core/video_job.py). Es el dato más sensible y el que
     antes deja de hacer falta: una vez calculados los ángulos no vuelve a
     usarse, y este servicio ni siquiera es su sistema de registro -- la
     plataforma de telefisioterapia lo envió y lo conserva.
  2. **Pasados `DATA_RETENTION_DAYS`**, este barrido minimiza la fila: borra
     los ángulos, la ruta del video y el endpoint de la plataforma, y conserva
     lo que no identifica a nadie.

Este módulo es además la RED del primer momento: si el borrado del video falló
(Firebase caído, credenciales rotadas), la fila conserva su `video_url` y acá
se vuelve a intentar.

QUÉ NO SE BORRA, Y POR QUÉ: la fila entera no. Lo que identifica a una persona
son los ángulos, la ruta del video y el `resultsEndpoint`, que suele llevar
identificadores del paciente o de la sesión. El id, el estado, el ejercicio, el
modelo y los tiempos no identifican a nadie y son lo que permite seguir
midiendo el servicio. Minimizar es quitar lo que identifica, no destruir el
registro operativo.

EL PLAZO NO ES UNA DECISIÓN TÉCNICA. Los 30 días por defecto son un punto de
partida conservador: el número correcto depende de la política de tratamiento
de datos de la plataforma y de cuánto tiempo necesite el equipo clínico poder
consultar un resultado. Hay que confirmarlo con quien responda por el
tratamiento.
"""

from datetime import datetime, timedelta, timezone

from app.constants.job_constants import CallbackStatus
from app.core.config import settings
from app.core.job_logging import log_job_event
from app.utils.db_connect import anonymize_jobs, find_expired_jobs
from app.utils.video_storage import delete_video
from app.utils.logger import get_logger

logger = get_logger("data_retention")


async def purge_expired_data() -> int:
    """
    Minimiza los jobs cerrados que superaron el plazo. Devuelve cuántos.

    Se procesa de a `RETENTION_BATCH_SIZE`: sin tope, la primera corrida sobre
    una base con historia acumulada intentaría borrar miles de videos de
    Firebase dentro de una sola tarea, dejando al worker sin atender videos
    nuevos. Lo que sobre se toma en la corrida siguiente.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.DATA_RETENTION_DAYS)
    expired = await find_expired_jobs(cutoff, settings.RETENTION_BATCH_SIZE)

    if not expired:
        return 0

    ready: list = []
    for job in expired:
        if job.video_url:
            try:
                await delete_video(job.video_url)
            except Exception:  # noqa: BLE001
                # La fila NO se minimiza: si se le borrara el `video_url`, el
                # blob quedaría en Firebase sin ningún registro de su ruta, o
                # sea imposible de borrar después. Dejándola intacta, la
                # próxima corrida lo vuelve a intentar. Un video problemático
                # no frena la minimización de los demás.
                logger.exception(
                    "No se pudo borrar el video del job %s: la fila queda para la "
                    "próxima corrida (%s)",
                    job.id,
                    job.video_url,
                )
                continue
        ready.append(job)

    if not ready:
        logger.warning(
            "Retención: ninguno de los %d job(s) vencidos pudo minimizarse", len(expired)
        )
        return 0

    _warn_about_results_nobody_received(ready)
    minimized = await anonymize_jobs([job.id for job in ready])

    log_job_event(
        "data_retention_sweep",
        minimized=minimized,
        retention_days=settings.DATA_RETENTION_DAYS,
    )
    logger.info(
        "Retención: %d job(s) minimizados por superar %d días",
        minimized,
        settings.DATA_RETENTION_DAYS,
    )

    postponed = len(expired) - len(ready)
    if postponed:
        logger.warning(
            "Retención: %d job(s) quedaron para la próxima corrida porque no se pudo "
            "borrar su video",
            postponed,
        )

    if len(expired) == settings.RETENTION_BATCH_SIZE:
        logger.info(
            "El lote se llenó (%d): quedan más jobs por minimizar para la próxima corrida",
            settings.RETENTION_BATCH_SIZE,
        )

    return minimized


def _warn_about_results_nobody_received(expired: list) -> None:
    """
    Avisa antes de destruir un resultado que la plataforma nunca recibió.

    Sin esto, un callback agotado se borraría en silencio al vencer el plazo y
    nadie sabría nunca que ese procesamiento se perdió. El aviso no detiene la
    minimización -- el plazo legal no se suspende porque haya una entrega
    pendiente -- pero deja constancia de qué se fue.
    """
    attention = {s.value for s in CallbackStatus.needs_attention()}
    never_received = [job.id for job in expired if job.callback_status in attention]

    if never_received:
        logger.warning(
            "Se van a minimizar %d job(s) cuyo resultado la plataforma nunca recibió: %s",
            len(never_received),
            ", ".join(never_received[:20]),
        )
