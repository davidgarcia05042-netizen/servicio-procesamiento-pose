"""
Health check (app/api/health.py, común) con el sondeo propio de Render
(app/platforms/render/health.py: el broker de Redis).

Lo que se protege acá: Render decide con este endpoint si un despliegue tuvo
éxito y, con el servicio ya corriendo, si deja de enrutarle tráfico o reinicia
la instancia. Un `ok` incondicional deja pasar a producción una versión con la
base mal configurada; un 503 de más provoca reinicios. Las dos direcciones
importan, y por eso hay tests de las dos.

Todo corre en proceso vía ASGITransport de httpx: sin sockets, sin PostgreSQL
y sin Redis reales.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.api import health
from app.core.config import settings
from app.platforms.render import health as render_health

# Una URL con contraseña, de la forma exacta que arma `broker_url`. Sirve para
# comprobar que nunca se filtra en la respuesta.
SECRET_PASSWORD = "contrasena-que-no-debe-salir"


class _FakeSession:
    """Sesión que responde al SELECT 1, o revienta si se le pide."""

    def __init__(self, error: Exception | None = None, delay: float = 0.0) -> None:
        self._error = error
        self._delay = delay

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False

    async def execute(self, statement: object) -> object:
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._error:
            raise self._error
        return SimpleNamespace()


def _database(error: Exception | None = None, delay: float = 0.0):
    """Doble de `database` cuyo .session() devuelve la sesión falsa."""
    if error is not None and delay == 0.0:
        # Un fallo al PEDIR la sesión (ej. la base no está conectada en este
        # proceso) tiene que contar igual que un fallo en la consulta.
        return SimpleNamespace(session=lambda: _FakeSession(error=error))
    return SimpleNamespace(session=lambda: _FakeSession(error=error, delay=delay))


def _queue(error: Exception | None = None, delay: float = 0.0) -> AsyncMock:
    async def ping() -> bool:
        if delay:
            await asyncio.sleep(delay)
        if error:
            raise error
        return True

    return SimpleNamespace(ping=ping)


@pytest.fixture
def client():
    from app.main import app

    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def _get_health(client, *, database, queue):
    with (
        patch.object(health, "database", database),
        patch.object(render_health, "_get_queue_client", lambda: queue),
    ):
        async with client:
            return await client.get("/health")


# --- El caso sano ---


async def test_everything_up_reports_ok_with_the_breakdown(client):
    response = await _get_health(client, database=_database(), queue=_queue())

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "checks": {"database": "ok", "queue": "ok"}}


# --- Las dependencias caídas ---


async def test_a_dead_database_reports_503(client):
    response = await _get_health(
        client, database=_database(error=OSError("conexión rechazada")), queue=_queue()
    )

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["checks"]["database"] == "error"
    # La cola sigue sana y hay que poder verlo: es lo que dice dónde mirar.
    assert body["checks"]["queue"] == "ok"


async def test_a_dead_queue_reports_503(client):
    response = await _get_health(
        client, database=_database(), queue=_queue(error=OSError("NOAUTH"))
    )

    assert response.status_code == 503
    assert response.json()["checks"] == {"database": "ok", "queue": "error"}


async def test_the_second_probe_still_runs_when_the_first_one_fails(client):
    """
    Discriminante de que los sondeos NO se cortocircuitan. Si el endpoint
    devolviera al primer fallo, un incidente con las dos dependencias caídas se
    vería como un problema de una sola, y se buscaría en el lugar equivocado.
    """
    response = await _get_health(
        client,
        database=_database(error=OSError("caída")),
        queue=_queue(error=OSError("caída")),
    )

    assert response.status_code == 503
    assert response.json()["checks"] == {"database": "error", "queue": "error"}


async def test_a_database_not_connected_in_this_process_counts_as_error(client):
    """`database.session()` lanza RuntimeError si nadie llamó a connect()."""
    broken = SimpleNamespace(
        session=lambda: (_ for _ in ()).throw(RuntimeError("no está conectada"))
    )

    with (
        patch.object(health, "database", broken),
        patch.object(render_health, "_get_queue_client", lambda: _queue()),
    ):
        async with client:
            response = await client.get("/health")

    assert response.status_code == 503
    assert response.json()["checks"]["database"] == "error"


# --- Una dependencia colgada, que es distinto de una caída ---


async def test_a_hanging_dependency_does_not_hang_the_endpoint(client, monkeypatch):
    """
    Una dependencia que no responde NI falla es el caso peor: sin tope, este
    endpoint se quedaría esperando con la conexión ocupada hasta que venciera
    el tiempo de espera de Render.
    """
    monkeypatch.setattr(health, "_PROBE_TIMEOUT_SECONDS", 0.1)

    started = asyncio.get_running_loop().time()
    response = await _get_health(client, database=_database(delay=5.0), queue=_queue())
    elapsed = asyncio.get_running_loop().time() - started

    assert response.status_code == 503
    assert response.json()["checks"]["database"] == "error"
    assert elapsed < 2.0, "el sondeo no respetó su tope de tiempo"


# --- Que no se filtre nada ---


async def test_the_response_never_leaks_connection_details(client):
    """
    Los mensajes de SQLAlchemy y de redis-py pueden incluir la URL de conexión,
    y ahí viaja la contraseña. /health es público (no lleva firma HMAC), así
    que el detalle del fallo va al log del servicio, nunca al cuerpo.
    """
    leaky = OSError(
        f"could not connect to postgresql+asyncpg://postgres:{SECRET_PASSWORD}@db:5432/pose"
    )

    response = await _get_health(
        client, database=_database(error=leaky), queue=_queue(error=leaky)
    )

    body = response.text
    assert SECRET_PASSWORD not in body
    assert "postgresql" not in body
    assert "@" not in body


# --- El cableado con el resto de la app ---


async def test_health_stays_exempt_from_the_hmac_signature(client, monkeypatch):
    """
    Render sondea sin firmar. Si /health dejara de estar exento, el health
    check fallaría siempre con HMAC_ENFORCE activo y Render reiniciaría la
    instancia en bucle.
    """
    monkeypatch.setattr(settings, "HMAC_ENFORCE", True)

    response = await _get_health(client, database=_database(), queue=_queue())

    assert response.status_code == 200


async def test_the_queue_client_is_reused_between_probes():
    """
    Se sondea cada pocos segundos, para siempre: crear un cliente por llamada
    abriría y cerraría una conexión a Redis sin parar.
    """
    await render_health.close_probes()
    try:
        first = render_health._get_queue_client()
        second = render_health._get_queue_client()
        assert first is second
    finally:
        await render_health.close_probes()
        assert render_health._queue_client is None
