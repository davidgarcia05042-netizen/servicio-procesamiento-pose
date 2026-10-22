"""
Límite de tasa del POST /video/ (app/core/rate_limit.py).

Se ejercita la app completa vía ASGITransport de httpx: llama al ASGI en
proceso, sin abrir sockets ni levantar uvicorn. El servicio de orquestación se
mockea, así que no se toca PostgreSQL, Firebase ni Celery.

Ojo con el orden: el middleware de HMAC corre ANTES del enrutamiento, así que
el límite de tasa solo se aplica a solicitudes ya firmadas correctamente; una
firma inválida se corta antes con 401 y ni siquiera consume cupo.
"""

import time
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.core.config import settings
from app.core.rate_limit import limiter
from app.dtos.error_success_code import ErrorAndSuccessCodes
from app.utils.hmac_auth import compute_signature

SECRET = "secreto-de-prueba"
MP4_HEADER = b"\x00\x00\x00\x20ftypisom" + b"\x00" * 16


@pytest.fixture
def signed_client(monkeypatch):
    """App real + firma HMAC válida + servicio mockeado, con el limiter en limpio."""
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


async def _post_video(client: httpx.AsyncClient) -> httpx.Response:
    request = client.build_request(
        "POST",
        "/video/",
        files={"video": ("video.mp4", MP4_HEADER, "video/mp4")},
        data={
            "points": "[24, 26, 28]",
            "exercise": "knee_flexion",
            "resultsEndpoint": "https://platform.example.com/results",
        },
    )
    body = request.read()
    timestamp = str(int(time.time()))
    request.headers["X-Timestamp"] = timestamp
    request.headers["X-Signature"] = compute_signature(timestamp, body, secret=SECRET)
    return await client.send(request)


async def test_video_endpoint_allows_requests_under_the_limit(signed_client, monkeypatch):
    monkeypatch.setattr(settings, "RATE_LIMIT_VIDEO", "3/minute")
    client, fake_service = signed_client

    async with client:
        responses = [await _post_video(client) for _ in range(3)]

    assert [r.status_code for r in responses] == [200, 200, 200]
    assert fake_service.await_count == 3


async def test_video_endpoint_returns_429_past_the_limit(signed_client, monkeypatch):
    """La solicitud excedente se corta con 429 y NO llega al servicio."""
    monkeypatch.setattr(settings, "RATE_LIMIT_VIDEO", "3/minute")
    client, fake_service = signed_client

    async with client:
        for _ in range(3):
            await _post_video(client)
        blocked = await _post_video(client)

    assert blocked.status_code == 429
    assert fake_service.await_count == 3  # la cuarta nunca se orquestó


async def test_rate_limit_value_is_read_from_settings_at_request_time(signed_client, monkeypatch):
    """El límite se lee por solicitud, así que se puede ajustar por entorno sin tocar el endpoint."""
    monkeypatch.setattr(settings, "RATE_LIMIT_VIDEO", "1/minute")
    client, _ = signed_client

    async with client:
        first = await _post_video(client)
        second = await _post_video(client)

    assert first.status_code == 200
    assert second.status_code == 429
