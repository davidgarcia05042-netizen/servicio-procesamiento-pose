"""
Demo local (docker-compose.demo.yml, app/tools/demo_*.py y el emulador de
almacenamiento en app/platforms/render/firebase_storage.py).

La demo es lo que el equipo va a ver del servicio, así que tiene que mostrar el
comportamiento REAL: la misma firma, la misma validación de endpoints y el
mismo borrado del video que en producción. Y el atajo que la hace posible sin
Firebase -- el emulador -- no puede colarse nunca a producción.
"""

import time
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

import httpx
import pytest
import yaml

from app.core.config import settings
from app.platforms.render.config import RenderSettings, render_settings
from app.utils.hmac_auth import compute_signature

ROOT = Path(__file__).resolve().parent.parent
SECRET = "secreto-de-prueba"


# --- 1. El emulador no puede llegar a producción ---


def test_production_refuses_to_start_with_the_emulator():
    """
    Con el emulador, los videos quedarían en un proceso sin persistencia ni
    control de acceso, y el servicio se vería sano mientras tanto.
    """
    with pytest.raises(ValueError, match="STORAGE_EMULATOR_HOST"):
        RenderSettings(
            ENVIRONMENT="production",
            STORAGE_EMULATOR_HOST="http://gcs:4443",
            _env_file=None,
        )


def test_development_accepts_the_emulator_and_production_works_without_it():
    """El discriminante: la guarda no puede romper ninguno de los dos casos legítimos."""
    RenderSettings(ENVIRONMENT="development", STORAGE_EMULATOR_HOST="http://gcs:4443", _env_file=None)
    RenderSettings(ENVIRONMENT="production", _env_file=None)


def test_the_emulator_bucket_uses_anonymous_credentials_and_skips_firebase():
    """
    `STORAGE_EMULATOR_HOST` por sí solo no alcanza: firebase-admin le pasa sus
    credenciales al cliente, y el cliente pediría un token a Google antes de
    cada llamada. Por eso el camino del emulador no puede pasar por
    firebase-admin.
    """
    from google.auth.credentials import AnonymousCredentials

    from app.platforms.render import firebase_storage

    created = {}

    class _Client:
        def __init__(self, **kwargs):
            created.update(kwargs)

        def lookup_bucket(self, name):
            return None

        def create_bucket(self, name):
            return f"bucket:{name}"

    with (
        patch.object(firebase_storage, "_emulator_bucket", None),
        patch.object(render_settings, "STORAGE_EMULATOR_HOST", "http://gcs:4443"),
        patch.object(render_settings, "FIREBASE_STORAGE_BUCKET", "pose-demo"),
        patch("google.cloud.storage.Client", _Client),
        patch.object(
            firebase_storage.firebase_admin,
            "initialize_app",
            side_effect=AssertionError("no debería inicializar firebase-admin"),
        ),
    ):
        bucket = firebase_storage._bucket()

    assert bucket == "bucket:pose-demo"
    assert isinstance(created["credentials"], AnonymousCredentials)
    assert created["client_options"] == {"api_endpoint": "http://gcs:4443"}


# --- 2. El receptor se comporta como la plataforma real ---


@pytest.fixture
def receiver(monkeypatch):
    from app.tools import demo_receiver

    monkeypatch.setattr(settings, "HMAC_SECRET", SECRET)
    demo_receiver.state.attempts.clear()
    demo_receiver.state.down = False
    yield demo_receiver
    demo_receiver.state.attempts.clear()
    demo_receiver.state.down = False


def _signed(body: bytes, secret: str = SECRET) -> dict[str, str]:
    timestamp = str(int(time.time()))
    return {"X-Timestamp": timestamp, "X-Signature": compute_signature(timestamp, body, secret)}


async def _post(receiver, body: bytes, headers: dict) -> httpx.Response:
    transport = httpx.ASGITransport(app=receiver.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://receptor") as client:
        return await client.post("/resultados/marcha-1", content=body, headers=headers)


PAYLOAD = (
    b'{"error":null,"results":{"points":[{"frame":0,"timestamp_ms":0,"angle":120.0},'
    b'{"frame":1,"timestamp_ms":33,"angle":null},{"frame":2,"timestamp_ms":66,"angle":140.0},'
    b'{"frame":3,"timestamp_ms":99,"angle":150.0}]},"max_angle":150.0,"min_angle":120.0}'
)


async def test_a_correctly_signed_result_is_accepted_and_kept(receiver):
    response = await _post(receiver, PAYLOAD, _signed(PAYLOAD))

    assert response.status_code == 200
    attempt = receiver.state.attempts[0]
    assert attempt.label == "marcha-1"
    assert attempt.signature_valid
    assert attempt.payload["max_angle"] == 150.0


async def test_a_bad_signature_gets_401_so_the_service_does_not_retry(receiver):
    """
    Es lo que tendría que hacer la plataforma real. Un 401 lo clasifica el
    servicio como `rejected` y no insiste, que es parte de lo que la demo
    enseña.
    """
    response = await _post(receiver, PAYLOAD, _signed(PAYLOAD, secret="otro"))

    assert response.status_code == 401
    assert receiver.state.attempts[0].payload is None


async def test_a_simulated_outage_answers_503_so_the_service_retries(receiver):
    receiver.state.down = True

    response = await _post(receiver, PAYLOAD, _signed(PAYLOAD))

    assert response.status_code == 503
    assert receiver.state.attempts[0].payload is None


def test_frames_without_a_detected_pose_leave_a_gap_in_the_curve():
    """
    Unir el punto anterior con el siguiente inventaría un movimiento que nadie
    midió: el frame sin pose tiene que verse como un hueco.
    """
    import json

    from app.tools.demo_receiver import angle_chart_svg

    svg = angle_chart_svg(json.loads(PAYLOAD)["results"]["points"])

    assert svg.count("<polyline") == 2


def test_a_video_with_no_angles_does_not_break_the_page():
    from app.tools.demo_receiver import angle_chart_svg

    assert "<svg" not in angle_chart_svg([{"frame": 0, "timestamp_ms": 0, "angle": None}])


async def test_the_page_escapes_what_comes_from_outside(receiver):
    """La etiqueta viene de la URL y el error del servicio: ninguno puede inyectar HTML."""
    body = b'{"error":"<script>alert(1)</script>","results":{"points":[]}}'
    transport = httpx.ASGITransport(app=receiver.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://receptor") as client:
        # Sin barra en la etiqueta: con "</b>" la ruta no coincide, da 404 y la
        # comprobación de abajo pasaría sin haber probado nada.
        sent = await client.post('/resultados/<img src=x onerror=1>', content=body, headers=_signed(body))
        page = (await client.get("/")).text

    assert sent.status_code == 200, "el intento no llegó al receptor: el test no probaría nada"
    assert "<script>alert(1)</script>" not in page
    assert "<img src=x" not in page
    assert "&lt;script&gt;" in page
    assert "&lt;img src=x onerror=1&gt;" in page


# --- 3. El envío firma exactamente lo que viaja ---


def _capture_send(tmp_path, secret):
    from app.tools.demo_send import send_video

    video = tmp_path / "v.mp4"
    video.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"x" * 100)
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(200, json={"job_id": "j"})

    with httpx.Client(transport=httpx.MockTransport(handler), base_url="http://api") as client:
        send_video(client, video, [24, 26, 28], "marcha", "http://receptor:9099/r/x", secret)
    return captured["request"]


def test_the_signature_covers_the_exact_multipart_bytes(tmp_path):
    """
    El límite del multipart lo genera httpx al armar la solicitud: firmar antes
    de armarla daría una firma que el servicio rechazaría con 401.
    """
    request = _capture_send(tmp_path, SECRET)

    timestamp = request.headers["X-Timestamp"]
    assert request.headers["X-Signature"] == compute_signature(timestamp, request.content, SECRET)
    assert b'name="resultsEndpoint"' in request.content
    assert b"[24, 26, 28]" in request.content


def test_without_a_secret_nothing_is_signed(tmp_path):
    request = _capture_send(tmp_path, None)

    assert "X-Signature" not in request.headers


# --- 4. Los compose locales: la demo de Render y el camino de Google Cloud ---

# Archivo -> los servicios internos, que no tienen por qué publicarse.
COMPOSE_FILES = {
    "docker-compose.demo.yml": ("postgres", "redis"),
    "docker-compose.gcp.yml": ("postgres", "gcs", "tareas"),
}


def _compose(name: str = "docker-compose.demo.yml") -> dict:
    return yaml.safe_load((ROOT / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("compose_file", COMPOSE_FILES)
def test_the_compose_secret_matches_the_sender_default(compose_file):
    """Si se desalinean, el primer envío de la demo recibe 401 frente al equipo."""
    from app.tools.demo_send import DEMO_SECRET

    for name in ("api", "worker", "receptor"):
        assert _compose(compose_file)["services"][name]["environment"]["HMAC_SECRET"] == DEMO_SECRET


@pytest.mark.parametrize("compose_file", COMPOSE_FILES)
def test_the_receiver_host_is_explicitly_allowed(compose_file):
    """
    `receptor` no tiene punto, y el servicio rechaza esos hosts por defecto.
    Sin esto, todo callback de la demo se rechazaría.
    """
    from app.tools.demo_send import DEMO_RECEIVER

    env = _compose(compose_file)["services"]["worker"]["environment"]
    assert urlsplit(DEMO_RECEIVER).hostname in env["CALLBACK_ALLOWED_HOSTS"].split(",")


@pytest.mark.parametrize("compose_file", COMPOSE_FILES)
def test_the_demo_runs_as_development_with_strict_signatures(compose_file):
    env = _compose(compose_file)["services"]["api"]["environment"]

    assert env["ENVIRONMENT"] == "development"
    assert env["HMAC_ENFORCE"] == "true"


def test_the_render_demo_keeps_the_videos_in_its_database_like_production():
    """Como en deploy/render/render.yaml: el video nunca sale de la máquina."""
    env = _compose("docker-compose.demo.yml")["services"]["api"]["environment"]

    assert env["STORAGE_BACKEND"] == "postgres"
    assert "STORAGE_EMULATOR_HOST" not in env


def test_the_google_path_stores_videos_in_the_emulator():
    env = _compose("docker-compose.gcp.yml")["services"]["api"]["environment"]

    assert env["STORAGE_EMULATOR_HOST"]


@pytest.mark.parametrize("compose_file, internal", COMPOSE_FILES.items())
def test_nothing_is_exposed_beyond_this_machine(compose_file, internal):
    """
    El video de la demo puede ser el de una persona real. Lo que se publica
    queda en 127.0.0.1, y la base, la cola y los emuladores ni siquiera se
    publican.
    """
    services = _compose(compose_file)["services"]

    for name, service in services.items():
        for port in service.get("ports", []):
            assert str(port).startswith("127.0.0.1:"), f"{name} publica {port} hacia afuera"

    for name in internal:
        assert "ports" not in services[name], f"{name} no necesita publicarse"


def test_the_google_path_runs_the_gcp_image_with_two_services():
    """API y worker separados, como en Cloud Run, y el worker en el módulo que existe."""
    services = _compose("docker-compose.gcp.yml")["services"]

    for name in ("migraciones", "api", "worker"):
        assert services[name]["build"]["target"] == "gcp"
        assert services[name]["environment"]["PLATFORM"] == "gcp"
    command = " ".join(services["worker"]["command"])
    module = command.split("uvicorn ", 1)[1].split(":", 1)[0]
    assert (ROOT / (module.replace(".", "/") + ".py")).exists(), module


def test_the_google_path_uses_the_cloud_sql_socket_and_the_emulators():
    env = _compose("docker-compose.gcp.yml")["services"]["api"]["environment"]

    assert "?host=/cloudsql/" in env["DATABASE_URL"]
    assert env["CLOUD_TASKS_EMULATOR_HOST"] and env["TASKS_OIDC_JWKS_URL"].startswith("http://tareas")

