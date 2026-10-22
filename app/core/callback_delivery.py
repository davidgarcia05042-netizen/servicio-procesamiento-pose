"""
Entrega del webhook de resultados hacia la plataforma, con reintentos.

POR QUÉ VIVE APARTE DE LA TAREA QUE PROCESA EL VIDEO: cuando llega el momento
de avisar, el resultado ya está calculado y guardado en PostgreSQL. Si el
reintento viviera dentro de `process_video_job`, reintentar significaría una de
dos cosas malas: volver a descargar el video y correr MediaPipe sobre un
resultado que ya existe, o dejar al worker esperando horas sin atender otros
videos. En su propia tarea, la entrega reintenta sola y el worker sigue
trabajando.

POLÍTICA (contrastada contra las guías de Svix y Hookdeck, sept. 2026):

  - Se reintenta: timeouts, fallos de conexión, 5xx y 429.

  - NO se reintenta ninguna otra respuesta que no sea 2xx (3xx y 4xx). Un 400
    o un 401 significan que la plataforma rechazó el contenido o la firma: el
    mismo payload fallaría igual las nueve veces. Pesa además que el esquema
    HMAC todavía no está acordado formalmente con la plataforma (bloqueador #2
    de docs/DECISIONES_TECNICAS.md), así que un 401 es el 4xx más probable que vamos a ver, y lo
    que necesita es que alguien lo mire, no insistencia.

    Acá las guías se contradicen: Svix recomienda reintentar TODO lo que no
    sea 2xx, porque sirve a miles de endpoints desconocidos cuyos dueños
    pueden arreglar una mala configuración en el camino. Hookdeck recomienda
    lo que hacemos acá, y advierte que insistir sobre fallos permanentes
    "puede parecer un ataque para el endpoint receptor". Se eligió lo segundo
    porque nuestro receptor es uno solo y conocido.

  - Si la respuesta trae `Retry-After` (RFC 9110, típico en 429 y 503), ese
    valor manda sobre el cálculo propio, acotado a `MAX_RETRY_AFTER`.

  - CADA INTENTO VUELVE A FIRMAR. La firma incluye el timestamp y hay ventana
    anti-replay (`HMAC_MAX_SKEW_SECONDS`, 5 minutos por defecto): reintentar a
    la media hora reusando la firma original sería un rechazo garantizado, y
    encima se vería como un problema de credenciales en vez de uno de reloj.

VENTANA: 9 intentos repartidos en unas 5,7 horas forman UNA RONDA. Terminada
una ronda sin éxito, la siguiente empieza tras `CALLBACK_RETRY_INTERVAL_MINUTES`,
y así hasta agotar `CALLBACK_RETRY_WINDOW_HOURS` contadas desde que entró el
job (3 días por defecto, la ventana de Stripe). Recién entonces el aviso queda
en `exhausted`. Repetir rondas cortas es mejor que una sola escalera de tres
días: cada ronda vuelve a empezar con esperas de segundos, así que una
plataforma que acaba de volver recibe su aviso enseguida en vez de esperar al
siguiente peldaño de horas.

Este módulo DECIDE cuándo toca el próximo intento (`next_step_after_failure`)
pero no lo programa: la hora se guarda en la tabla y la cumple el despachador
de app/core/callback_retry.py.
"""

import json
import random
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Optional

import httpx

from app.constants.job_constants import CallbackStatus
from app.utils.hmac_auth import compute_signature
from app.utils.request_validations import endpoint_origin, rejected_endpoint_reason
from app.utils.logger import get_logger

logger = get_logger("callback_delivery")

_TIMEOUT_SECONDS = 30

# Espera antes de cada reintento, en segundos. El primero es casi inmediato:
# cubre el parpadeo de red, que es el fallo más común y el que se resuelve
# solo. De ahí en adelante crece para darle aire a una caída de verdad.
RETRY_DELAYS = (5, 30, 120, 480, 1800, 3600, 7200, 7200)
ROUND_ATTEMPTS = len(RETRY_DELAYS) + 1

# Jitter proporcional (±20%), y no "full jitter" (un valor al azar entre 0 y
# el delay completo) como recomiendan las guías. El full jitter existe para
# descorrelacionar una estampida de reintentos simultáneos; acá hay UNA
# plataforma cliente y poco volumen, así que ese beneficio no aplica, y en
# cambio recortaría a la mitad la ventana real de reintentos -- justo lo que
# se quiere sostener.
_JITTER = 0.2

# Tope a lo que se honra de un `Retry-After`. El header lo elige el receptor, y
# sin tope una respuesta con "Retry-After: 604800" dejaría el aviso parado una
# semana: un receptor mal configurado podría frenar los avisos indefinidamente
# sin que nada lo hiciera evidente.
#
# El tope es la espera más larga de la escalera propia: por encima de eso, lo
# que pida el otro lado ya no aporta información que no tengamos.
MAX_RETRY_AFTER = max(RETRY_DELAYS)


@dataclass(frozen=True)
class NextStep:
    """Qué queda escrito en la fila después de un intento fallido."""

    status: CallbackStatus
    next_attempt_at: Optional[datetime]


def next_step_after_failure(
    attempt: int,
    created_at: datetime,
    now: datetime,
    retry_after: Optional[float],
    round_pause: timedelta,
    window: timedelta,
) -> NextStep:
    """
    Cuándo toca el próximo intento, o si ya no toca ninguno.

    `attempt` es el número TOTAL de intentos hechos, contando todas las rondas.
    Dentro de una ronda, la espera sale de `RETRY_DELAYS`; al cerrarse una
    ronda (cada `ROUND_ATTEMPTS` intentos), la siguiente empieza tras
    `round_pause`.

    Se deja de insistir cuando el próximo intento caería fuera de la ventana,
    contada desde que ENTRÓ el job. Sin ese tope, un endpoint mal configurado
    recibiría rondas de nueve intentos para siempre: eso deja de ser
    recuperación y pasa a ser hostigamiento.

    Es una función pura a propósito: la política completa de reintentos se
    puede probar sin broker, sin base y sin reloj real.
    """
    position = attempt % ROUND_ATTEMPTS
    if position:
        wait = timedelta(seconds=next_delay(position - 1, retry_after))
    else:
        wait = round_pause

    next_attempt_at = now + wait
    if next_attempt_at >= created_at + window:
        return NextStep(CallbackStatus.EXHAUSTED, None)
    return NextStep(CallbackStatus.RETRYING, next_attempt_at)


class CallbackDeliveryError(Exception):
    """Fallo transitorio: vale la pena reintentar."""

    def __init__(self, message: str, retry_after: Optional[float] = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class CallbackRejectedError(Exception):
    """La plataforma rechazó el envío. Reintentar el mismo payload no cambia nada."""


def parse_retry_after(value: Optional[str]) -> Optional[float]:
    """
    Lee el header `Retry-After` (RFC 9110): segundos ("120") o fecha HTTP.

    Devuelve None si viene ausente o ilegible, para caer en el cálculo propio
    en vez de romper la entrega por un header mal formado del otro lado.
    """
    if not value:
        return None

    text = value.strip()

    try:
        return max(0.0, float(int(text)))
    except ValueError:
        pass

    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None

    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)

    return max(0.0, when.timestamp() - time.time())


def next_delay(retry_index: int, retry_after: Optional[float] = None) -> float:
    """
    Cuánto esperar antes del reintento número `retry_index` (0 = el primero).

    Si la plataforma dijo cuánto esperar, se respeta: insistir antes solo gasta
    intentos contra alguien que ya avisó que no está listo.
    """
    if retry_after is not None:
        # Acotado: ver MAX_RETRY_AFTER.
        return min(retry_after, MAX_RETRY_AFTER)

    base = RETRY_DELAYS[min(retry_index, len(RETRY_DELAYS) - 1)]
    return base * random.uniform(1 - _JITTER, 1 + _JITTER)  # noqa: S311 -- no es criptografía


async def deliver_callback(results_endpoint: str, payload: dict[str, Any]) -> None:
    """
    Un intento de entrega: firma, envía y clasifica el resultado.

    No reintenta por su cuenta. Traduce lo que pasó a una de dos excepciones
    para que quien la llame decida: `CallbackDeliveryError` si vale la pena
    volver a intentar, `CallbackRejectedError` si no.

    El cuerpo se serializa acá y viaja con `content=` en vez de `json=` porque
    hay que firmar EXACTAMENTE los bytes que se envían: si httpx serializara
    por su cuenta, cualquier diferencia de separadores invalidaría la firma
    del otro lado.
    """
    # Segunda barrera, en el punto donde de verdad sale la petición.
    #
    # La primera está en la entrada (`validate_results_endpoint`), y con eso
    # bastaría si la fila se hubiera creado hoy. Pero una fila pudo guardarse
    # antes de que se restringiera la lista de hosts, y el barrido de jobs
    # colgados encola callbacks leyendo la tabla, sin volver a pasar por la
    # validación de entrada. Comprobar acá cuesta nada y cubre los dos casos.
    #
    # Se levanta `CallbackRejectedError` y no `CallbackDeliveryError`: es
    # definitivo. Reintentar una dirección no permitida durante horas es
    # justamente el abuso que esto evita.
    reason = rejected_endpoint_reason(results_endpoint)
    if reason is not None:
        logger.warning("Entrega cancelada, endpoint no permitido (%s)", reason)
        raise CallbackRejectedError(f"endpoint no permitido: {reason}")

    # Lo que va a logs y a `callback_error`: sin la ruta, que puede llevar
    # identificadores del paciente (ver `endpoint_origin`).
    target = endpoint_origin(results_endpoint)

    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    timestamp = str(int(time.time()))

    headers = {
        "Content-Type": "application/json",
        "X-Timestamp": timestamp,
        "X-Signature": compute_signature(timestamp, body),
    }

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            response = await client.post(results_endpoint, content=body, headers=headers)
    except httpx.HTTPError as exc:
        # Timeout, DNS, conexión rechazada: la plataforma no llegó a responder.
        raise CallbackDeliveryError(f"no se pudo contactar a {target}: {exc}") from exc

    if response.is_success:
        logger.info("Callback entregado a %s (%d)", target, response.status_code)
        return

    if response.status_code == 429 or response.status_code >= 500:
        raise CallbackDeliveryError(
            f"{target} respondió {response.status_code}",
            retry_after=parse_retry_after(response.headers.get("Retry-After")),
        )

    raise CallbackRejectedError(f"{target} respondió {response.status_code}")
