"""
Entrega del webhook de resultados y su política de reintentos
(app/core/callback_delivery.py).

Lo que se protege acá es la distinción entre un fallo que vale la pena
reintentar y uno que no. Si esa clasificación se rompe, el servicio o bien
abandona un resultado que sí se podía entregar, o bien machaca a la plataforma
durante horas con un payload que nunca va a aceptar.
"""

import time
from email.utils import formatdate
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

from app.constants.job_constants import CallbackStatus
from app.core import callback_delivery
from app.core.callback_delivery import (
    MAX_RETRY_AFTER,
    ROUND_ATTEMPTS,
    RETRY_DELAYS,
    CallbackDeliveryError,
    CallbackRejectedError,
    deliver_callback,
    next_delay,
    next_step_after_failure,
    parse_retry_after,
)

ENDPOINT = "https://platform.example.com/results"
PAYLOAD = {"error": None, "results": {"points": []}, "max_angle": 90.0, "min_angle": 10.0}


class _FakeClient:
    """Cliente httpx de mentira: devuelve una respuesta fija o lanza un error."""

    response: httpx.Response | None = None
    error: Exception | None = None
    calls: list = []

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False

    async def post(self, url, content=None, headers=None):
        _FakeClient.calls.append(SimpleNamespace(url=url, content=content, headers=headers))
        if _FakeClient.error is not None:
            raise _FakeClient.error
        return _FakeClient.response


def _respond(status_code: int, headers: dict | None = None) -> None:
    _FakeClient.calls = []
    _FakeClient.error = None
    _FakeClient.response = httpx.Response(
        status_code, headers=headers or {}, request=httpx.Request("POST", ENDPOINT)
    )


def _fail_with(error: Exception) -> None:
    _FakeClient.calls = []
    _FakeClient.error = error
    _FakeClient.response = None


# --- Qué se reintenta y qué no ---


async def test_a_successful_response_does_not_raise():
    _respond(200)
    with patch.object(callback_delivery.httpx, "AsyncClient", _FakeClient):
        await deliver_callback(ENDPOINT, PAYLOAD)


@pytest.mark.parametrize("status", [500, 502, 503, 504, 429])
async def test_server_errors_and_rate_limiting_are_worth_retrying(status):
    _respond(status)
    with patch.object(callback_delivery.httpx, "AsyncClient", _FakeClient):
        with pytest.raises(CallbackDeliveryError):
            await deliver_callback(ENDPOINT, PAYLOAD)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422, 301])
async def test_rejections_are_not_retried(status):
    """
    Un 401 es el caso más probable acá: significaría que la plataforma no
    acepta nuestro esquema de firma (bloqueador #2 de docs/DECISIONES_TECNICAS.md). Insistir
    nueve veces no lo arregla y entierra la señal.
    """
    _respond(status)
    with patch.object(callback_delivery.httpx, "AsyncClient", _FakeClient):
        with pytest.raises(CallbackRejectedError):
            await deliver_callback(ENDPOINT, PAYLOAD)


@pytest.mark.parametrize(
    "error",
    [
        httpx.ConnectTimeout("se agotó el tiempo"),
        httpx.ReadTimeout("la plataforma no respondió"),
        httpx.ConnectError("conexión rechazada"),
    ],
)
async def test_network_failures_are_worth_retrying(error):
    _fail_with(error)
    with patch.object(callback_delivery.httpx, "AsyncClient", _FakeClient):
        with pytest.raises(CallbackDeliveryError):
            await deliver_callback(ENDPOINT, PAYLOAD)


# --- Retry-After: lo que diga la plataforma manda ---


async def test_retry_after_is_read_from_the_response():
    _respond(429, headers={"Retry-After": "120"})
    with patch.object(callback_delivery.httpx, "AsyncClient", _FakeClient):
        with pytest.raises(CallbackDeliveryError) as raised:
            await deliver_callback(ENDPOINT, PAYLOAD)

    assert raised.value.retry_after == 120


def test_retry_after_accepts_seconds_and_http_dates():
    assert parse_retry_after("120") == 120

    in_a_minute = parse_retry_after(formatdate(time.time() + 60, usegmt=True))
    assert in_a_minute is not None and 50 <= in_a_minute <= 70


def test_an_unreadable_retry_after_falls_back_instead_of_breaking():
    """Un header mal formado del otro lado no debe romper la entrega."""
    assert parse_retry_after(None) is None
    assert parse_retry_after("") is None
    assert parse_retry_after("cuando quieras") is None


def test_retry_after_wins_over_the_computed_delay():
    assert next_delay(0, retry_after=45.0) == 45.0


# --- La ventana de reintentos ---


def test_the_delay_grows_and_stays_near_the_schedule():
    """
    El jitter es proporcional (±20%), no "full jitter": con un solo cliente no
    hay estampida que descorrelacionar, y un valor al azar entre 0 y el delay
    completo recortaría a la mitad la ventana que se quiere sostener.
    """
    for index, base in enumerate(RETRY_DELAYS):
        delays = [next_delay(index) for _ in range(50)]
        assert all(base * 0.8 <= delay <= base * 1.2 for delay in delays)

    # Y crece: el primer reintento es casi inmediato, el último no.
    assert RETRY_DELAYS[0] < RETRY_DELAYS[-1]


def test_the_window_spans_hours_not_minutes():
    """
    Una ventana de minutos no sobrevive ni una ventana de mantenimiento
    nocturno de la plataforma. Cota inferior deliberada: si alguien recorta la
    tabla de esperas, este test lo señala.
    """
    total_hours = sum(RETRY_DELAYS) / 3600
    assert ROUND_ATTEMPTS == len(RETRY_DELAYS) + 1
    assert 4 <= total_hours <= 8


# --- Cada intento firma de nuevo ---


async def test_every_attempt_signs_with_a_fresh_timestamp():
    """
    La firma lleva timestamp y hay ventana anti-replay de 5 minutos. Reusar la
    firma del primer intento haría que todo reintento posterior a esa ventana
    fuera rechazado, y parecería un problema de credenciales.
    """
    _respond(200)
    with patch.object(callback_delivery.httpx, "AsyncClient", _FakeClient):
        await deliver_callback(ENDPOINT, PAYLOAD)
        with patch.object(callback_delivery.time, "time", return_value=time.time() + 600):
            await deliver_callback(ENDPOINT, PAYLOAD)

    first, second = _FakeClient.calls
    assert first.headers["X-Timestamp"] != second.headers["X-Timestamp"]
    assert first.headers["X-Signature"] != second.headers["X-Signature"]
    # El cuerpo es idéntico: lo único que cambió fue el momento de firmar.
    assert first.content == second.content


# --- La política: cuándo toca el próximo intento ---

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
PAUSE = timedelta(hours=1)
WINDOW = timedelta(hours=72)


def _step(attempt, retry_after=None, created_at=NOW):
    return next_step_after_failure(
        attempt, created_at=created_at, now=NOW, retry_after=retry_after,
        round_pause=PAUSE, window=WINDOW,
    )


def test_inside_a_round_the_wait_follows_the_ladder():
    """El intento 1 falló: el siguiente va en ~5 s; el 2 falló: en ~30 s."""
    for attempt, base in ((1, RETRY_DELAYS[0]), (2, RETRY_DELAYS[1])):
        step = _step(attempt)
        assert step.status == CallbackStatus.RETRYING
        wait = (step.next_attempt_at - NOW).total_seconds()
        assert base * 0.8 <= wait <= base * 1.2


def test_retry_after_from_the_platform_wins_but_stays_capped():
    assert _step(1, retry_after=90.0).next_attempt_at == NOW + timedelta(seconds=90)
    capped = _step(1, retry_after=604800.0).next_attempt_at
    assert capped == NOW + timedelta(seconds=MAX_RETRY_AFTER)


def test_a_finished_round_pauses_before_the_next_one():
    """Tras el último intento de la ronda no se sigue la escalera: se espera la pausa."""
    step = _step(ROUND_ATTEMPTS)
    assert step.status == CallbackStatus.RETRYING
    assert step.next_attempt_at == NOW + PAUSE


def test_a_new_round_starts_again_with_seconds():
    """
    Es lo que hace útiles a las rondas: una plataforma que acaba de volver
    recibe su aviso en segundos, no en el siguiente peldaño de horas.
    """
    wait = (_step(ROUND_ATTEMPTS + 1).next_attempt_at - NOW).total_seconds()
    assert RETRY_DELAYS[0] * 0.8 <= wait <= RETRY_DELAYS[0] * 1.2


def test_it_gives_up_when_the_next_attempt_would_fall_outside_the_window():
    """Sin este tope, un endpoint mal configurado recibiría rondas para siempre."""
    almost_expired = NOW - WINDOW + timedelta(seconds=2)
    step = _step(1, created_at=almost_expired)
    assert step.status == CallbackStatus.EXHAUSTED
    assert step.next_attempt_at is None


def test_a_platform_down_for_three_days_gets_a_bounded_number_of_attempts():
    """
    La política completa, de punta a punta, con la plataforma caída todo el
    tiempo: hay que insistir durante la ventana entera, pero sin hostigar. Con
    el jitter fijo en su valor central son ~10 rondas de 9 intentos.
    """
    created, now, attempt = NOW, NOW, 0
    with patch.object(callback_delivery.random, "uniform", return_value=1.0):
        while True:
            attempt += 1
            step = next_step_after_failure(
                attempt, created_at=created, now=now, retry_after=None,
                round_pause=PAUSE, window=WINDOW,
            )
            if step.status == CallbackStatus.EXHAUSTED:
                break
            assert step.next_attempt_at < created + WINDOW
            now = step.next_attempt_at

    assert now - created > WINDOW - timedelta(hours=8), "dejó de insistir demasiado pronto"
    assert 80 <= attempt <= 110


def test_the_statuses_that_need_a_human_are_the_undelivered_ones():
    """`retrying` no está: todavía se está intentando, no es deuda."""
    assert CallbackStatus.needs_attention() == {
        CallbackStatus.REJECTED,
        CallbackStatus.EXHAUSTED,
    }
    assert CallbackStatus.RETRYING not in CallbackStatus.final()


# --- Ninguna tarea espera dentro del broker ---


def test_no_task_is_ever_scheduled_with_a_delay():
    """
    Una tarea programada con countdown o eta queda "sin confirmar" en Redis
    desde que se publica: si la espera supera el visibility_timeout se
    ejecutan varias copias, y si el worker se reinicia queda varada. Pasó con
    los reintentos de callback (verificado contra Redis real el 2026-09-24).
    Las esperas viven en PostgreSQL; este test impide reintroducirlas en la cola.
    """
    import re
    from pathlib import Path

    app_dir = Path(__file__).resolve().parent.parent / "app"
    offenders = [
        f"{path.relative_to(app_dir.parent)}:{number}"
        for path in app_dir.rglob("*.py")
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if re.search(r"countdown\s*=|\beta\s*=|\.retry\(|apply_async\(", line)
        and not line.lstrip().startswith("#")
    ]
    assert offenders == []


def test_the_visibility_timeout_is_set_explicitly():
    """
    Coincide con el de Celery, pero va explícito porque el umbral de avisos
    perdidos (CALLBACK_STALE_PENDING_HOURS) se calcula contra él.
    """
    from app.platforms.render.worker import VISIBILITY_TIMEOUT_SECONDS, celery

    assert celery.conf.broker_transport_options["visibility_timeout"] == VISIBILITY_TIMEOUT_SECONDS


async def test_logs_and_stored_errors_leave_out_the_endpoint_path(caplog):
    """
    La ruta del resultsEndpoint suele llevar identificadores del paciente, y
    el mensaje de error se guarda en `callback_error`. Retención borra el
    endpoint de la tabla; no puede quedar copiado en otro lado.
    """
    import logging

    endpoint = "https://platform.example.com/pacientes/123/sesion/9"

    _respond(200)
    with patch.object(callback_delivery.httpx, "AsyncClient", _FakeClient), caplog.at_level(logging.INFO):
        await deliver_callback(endpoint, {"error": None})
    assert "platform.example.com" in caplog.text
    assert "pacientes" not in caplog.text

    _respond(503)
    with patch.object(callback_delivery.httpx, "AsyncClient", _FakeClient):
        with pytest.raises(CallbackDeliveryError) as transient:
            await deliver_callback(endpoint, {"error": None})
    _respond(401)
    with patch.object(callback_delivery.httpx, "AsyncClient", _FakeClient):
        with pytest.raises(CallbackRejectedError) as rejected:
            await deliver_callback(endpoint, {"error": None})

    for error in (transient.value, rejected.value):
        assert "platform.example.com" in str(error)
        assert "pacientes" not in str(error)
