"""
Medidas de seguridad de la capa de entrada y del webhook de salida.

Cubre tres cosas independientes:
  1. El chequeo de tamaño corre ANTES del middleware de HMAC (y por lo tanto
     antes de que el cuerpo se bufferice en memoria).
  2. HMAC_ENFORCE alterna entre modo advertencia (deja pasar) y estricto (401).
  3. El callback saliente va firmado con el mismo esquema que se verifica del
     lado entrante.

Todo corre en proceso vía ASGITransport de httpx: sin sockets, sin uvicorn,
sin PostgreSQL, sin Firebase y sin Celery.
"""

import logging
import time
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.core.config import settings
from app.core.rate_limit import limiter
from app.dtos.error_success_code import ErrorAndSuccessCodes
from app.utils.hmac_auth import compute_signature, is_valid_signature

SECRET = "secreto-de-prueba"
MP4_HEADER = b"\x00\x00\x00\x20ftypisom" + b"\x00" * 16


@pytest.fixture
def client(monkeypatch):
    """App real con el servicio de orquestación mockeado y el limiter en limpio."""
    monkeypatch.setattr(settings, "HMAC_SECRET", SECRET)
    monkeypatch.setattr(settings, "HMAC_MAX_SKEW_SECONDS", 300)

    from app.main import app

    limiter.reset()
    fake_service = AsyncMock(
        return_value={"job_id": "job-123", "status": ErrorAndSuccessCodes.SUCCESS}
    )

    with patch("app.api.video_processing.process_video", new=fake_service):
        transport = httpx.ASGITransport(app=app)
        yield httpx.AsyncClient(transport=transport, base_url="http://test"), fake_service

    limiter.reset()


def _build_video_request(client: httpx.AsyncClient) -> httpx.Request:
    return client.build_request(
        "POST",
        "/video/",
        files={"video": ("video.mp4", MP4_HEADER, "video/mp4")},
        data={
            "points": "[24, 26, 28]",
            "exercise": "knee_flexion",
            "resultsEndpoint": "https://platform.example.com/results",
        },
    )


def _sign(request: httpx.Request) -> httpx.Request:
    body = request.read()
    timestamp = str(int(time.time()))
    request.headers["X-Timestamp"] = timestamp
    request.headers["X-Signature"] = compute_signature(timestamp, body, secret=SECRET)
    return request


# --- 1. Chequeo de tamaño antes del middleware de HMAC ---


async def test_oversized_payload_is_rejected_before_hmac_middleware_runs(client, monkeypatch):
    """
    Discriminante del ORDEN de ejecución, no solo del rechazo.

    La solicitud excede el tamaño Y va sin firma, con HMAC_ENFORCE activo. Si
    el middleware de tamaño corre primero -> 413. Si corriera primero el de
    HMAC -> sería 401, y el cuerpo entero ya habría pasado por memoria. El
    código de estado es la prueba de cuál de los dos se ejecutó.
    """
    monkeypatch.setattr(settings, "MAX_VIDEO_SIZE_MB", 1)
    monkeypatch.setattr(settings, "HMAC_ENFORCE", True)
    http_client, fake_service = client

    async with http_client:
        response = await http_client.post("/video/", content=b"x" * (2 * 1024 * 1024))

    assert response.status_code == 413
    fake_service.assert_not_awaited()


async def test_payload_within_size_limit_passes_the_size_middleware(client, monkeypatch):
    monkeypatch.setattr(settings, "MAX_VIDEO_SIZE_MB", 200)
    monkeypatch.setattr(settings, "HMAC_ENFORCE", True)
    http_client, fake_service = client

    async with http_client:
        response = await http_client.send(_sign(_build_video_request(http_client)))

    assert response.status_code == 200
    fake_service.assert_awaited_once()


async def _in_chunks(body: bytes, size: int = 256 * 1024):
    """Envía el cuerpo por trozos: httpx lo manda sin Content-Length."""
    for start in range(0, len(body), size):
        yield body[start : start + size]


async def test_a_body_without_content_length_is_still_capped(client, monkeypatch):
    """
    El chequeo de tamaño por header no ve una solicitud enviada por trozos
    (Transfer-Encoding: chunked), porque no trae Content-Length. Antes, el
    middleware de HMAC la leía ENTERA a memoria con `request.body()`: un cuerpo
    de varios GB tumbaba la instancia de la API.

    Va con HMAC_ENFORCE=false porque es como está hoy en producción: con la
    firma en modo advertencia, nada más frenaba la lectura.
    """
    monkeypatch.setattr(settings, "MAX_VIDEO_SIZE_MB", 1)
    monkeypatch.setattr(settings, "HMAC_ENFORCE", False)
    http_client, fake_service = client

    async with http_client:
        response = await http_client.post(
            "/video/", content=_in_chunks(b"x" * (3 * 1024 * 1024))
        )

    assert response.status_code == 413
    fake_service.assert_not_awaited()


async def test_a_legitimate_chunked_body_still_reaches_the_endpoint_intact(client, monkeypatch):
    """
    El discriminante de lo anterior. La lectura acotada deja el cuerpo en
    `request._body`, que es de donde Starlette se lo reenvía a FastAPI: si
    ese detalle interno cambiara en una actualización, el endpoint recibiría
    un cuerpo vacío y esto fallaría.
    """
    monkeypatch.setattr(settings, "HMAC_ENFORCE", True)
    http_client, fake_service = client

    async with http_client:
        request = _sign(_build_video_request(http_client))
        body = request.read()
        response = await http_client.post(
            "/video/",
            content=_in_chunks(body, size=7),
            headers={
                "Content-Type": request.headers["Content-Type"],
                "X-Timestamp": request.headers["X-Timestamp"],
                "X-Signature": request.headers["X-Signature"],
            },
        )

    assert response.status_code == 200
    kwargs = fake_service.await_args.kwargs
    assert kwargs["points"] == [24, 26, 28]
    assert kwargs["results_endpoint"] == "https://platform.example.com/results"


# --- 2. HMAC_ENFORCE: modo advertencia vs. estricto ---


async def test_unsigned_request_is_allowed_and_logged_in_warning_mode(client, monkeypatch, caplog):
    monkeypatch.setattr(settings, "HMAC_ENFORCE", False)
    http_client, fake_service = client

    with caplog.at_level(logging.WARNING):
        async with http_client:
            response = await http_client.send(_build_video_request(http_client))

    assert response.status_code == 200
    fake_service.assert_awaited_once()  # la solicitud se procesó igual
    assert "SIN firma HMAC válida" in caplog.text


async def test_tampered_signature_is_allowed_and_logged_in_warning_mode(client, monkeypatch, caplog):
    monkeypatch.setattr(settings, "HMAC_ENFORCE", False)
    http_client, fake_service = client

    request = _build_video_request(http_client)
    request.read()
    request.headers["X-Timestamp"] = str(int(time.time()))
    request.headers["X-Signature"] = "0" * 64  # firma con forma válida pero incorrecta

    with caplog.at_level(logging.WARNING):
        async with http_client:
            response = await http_client.send(request)

    assert response.status_code == 200
    fake_service.assert_awaited_once()
    assert "SIN firma HMAC válida" in caplog.text


async def test_unsigned_request_is_rejected_with_401_in_enforce_mode(client, monkeypatch):
    monkeypatch.setattr(settings, "HMAC_ENFORCE", True)
    http_client, fake_service = client

    async with http_client:
        response = await http_client.send(_build_video_request(http_client))

    assert response.status_code == 401
    fake_service.assert_not_awaited()


async def test_correctly_signed_request_passes_in_enforce_mode(client, monkeypatch):
    monkeypatch.setattr(settings, "HMAC_ENFORCE", True)
    http_client, fake_service = client

    async with http_client:
        response = await http_client.send(_sign(_build_video_request(http_client)))

    assert response.status_code == 200
    fake_service.assert_awaited_once()


# --- 3. Firma del callback saliente ---


class _RecordingClient:
    """AsyncClient falso que captura el POST del webhook en vez de enviarlo."""

    captured: dict = {}

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def post(self, url, content=None, headers=None):
        _RecordingClient.captured = {"url": url, "content": content, "headers": headers}
        return httpx.Response(200, request=httpx.Request("POST", url))


async def test_outgoing_callback_signature_verifies_with_the_incoming_verifier(monkeypatch):
    """
    Garantiza que ambos lados usan EXACTAMENTE el mismo algoritmo: la firma que
    produce el worker se valida con `is_valid_signature`, la misma función que
    corre sobre las solicitudes entrantes.
    """
    monkeypatch.setattr(settings, "HMAC_SECRET", SECRET)
    monkeypatch.setattr(settings, "HMAC_MAX_SKEW_SECONDS", 300)

    from app.core import callback_delivery
    from app.dtos.video_processing_dtos import CallbackPayload, CallbackResults

    payload = CallbackPayload(
        error=None,
        results=CallbackResults(points=[{"frame": 0, "angle": 91.5}]),
        max_angle=150.0,
        min_angle=32.5,
    )

    with patch.object(callback_delivery.httpx, "AsyncClient", _RecordingClient):
        await callback_delivery.deliver_callback(
            "https://platform.example.com/results", payload.model_dump()
        )

    captured = _RecordingClient.captured
    headers = captured["headers"]
    body = captured["content"]

    assert headers["Content-Type"] == "application/json"
    # La firma se valida sobre los MISMOS bytes que se enviaron.
    assert is_valid_signature(headers["X-Timestamp"], headers["X-Signature"], body) is True


async def test_outgoing_callback_signature_breaks_if_body_is_altered(monkeypatch):
    """Si alguien altera el cuerpo en tránsito, la verificación debe fallar."""
    monkeypatch.setattr(settings, "HMAC_SECRET", SECRET)
    monkeypatch.setattr(settings, "HMAC_MAX_SKEW_SECONDS", 300)

    from app.core import callback_delivery
    from app.dtos.video_processing_dtos import CallbackPayload

    with patch.object(callback_delivery.httpx, "AsyncClient", _RecordingClient):
        await callback_delivery.deliver_callback(
            "https://platform.example.com/results",
            CallbackPayload(error="algo falló").model_dump(),
        )

    headers = _RecordingClient.captured["headers"]
    tampered = _RecordingClient.captured["content"].replace(b"algo", b"nada")

    assert is_valid_signature(headers["X-Timestamp"], headers["X-Signature"], tampered) is False
