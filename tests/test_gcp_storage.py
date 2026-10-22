"""
Cloud Storage como almacenamiento del video en Google Cloud
(app/platforms/gcp/cloud_storage.py).

Dos niveles:

  - Sin red: la configuración, qué cliente se arma (anónimo SOLO con el
    emulador) y cómo se interpreta cada respuesta.
  - Contra el emulador de Cloud Storage (fake-gcs-server, el mismo de la
    demo), que habla la misma API JSON que el servicio real: subir, bajar y
    borrar de verdad, incluida la subida reanudable de los videos de más de
    8 MB. Corren si GCS_EMULATOR_FOR_TESTS apunta a un emulador; el workflow
    de tests lo levanta, así que en cada PR corren siempre. En local:

        docker run -d --rm -p 4443:4443 fsouza/fake-gcs-server:1 `
            -scheme http -port 4443 -backend memory -external-url http://localhost:4443
        $env:GCS_EMULATOR_FOR_TESTS = "http://localhost:4443"

Que el bucket REAL acepte las mismas llamadas con la cuenta de servicio lo
comprueba `python -m app.tools.storage_check --plataforma gcp` (ver README).
"""

import asyncio
import json
import os
import uuid
from pathlib import Path

import pytest
from google.auth.credentials import AnonymousCredentials
from google.cloud.exceptions import NotFound

from app.core.config import settings
from app.platforms import gcp
from app.platforms.gcp import cloud_storage
from app.platforms.gcp.config import GcpSettings, gcp_settings
from app.utils.video_storage import new_video_path

ROOT = Path(__file__).resolve().parent.parent
EMULATOR = os.environ.get("GCS_EMULATOR_FOR_TESTS", "").strip()


@pytest.fixture(autouse=True)
def fresh_client(monkeypatch):
    """Cada test arma su propio cliente: el del módulo se cachea por proceso."""
    monkeypatch.setattr(cloud_storage, "_bucket", None)


def test_the_platform_storage_is_cloud_storage():
    assert gcp.video_storage() is cloud_storage


# --- Configuración ---


def test_it_does_not_start_without_a_bucket(monkeypatch):
    monkeypatch.setattr(gcp_settings, "GCS_BUCKET", " ")

    with pytest.raises(RuntimeError, match="GCS_BUCKET"):
        cloud_storage.ensure_configured()


def test_it_starts_with_a_bucket(monkeypatch):
    monkeypatch.setattr(gcp_settings, "GCS_BUCKET", "pose-videos")

    cloud_storage.ensure_configured()


def test_production_refuses_to_start_with_the_emulator():
    with pytest.raises(ValueError, match="STORAGE_EMULATOR_HOST"):
        GcpSettings(_env_file=None, ENVIRONMENT="production", STORAGE_EMULATOR_HOST="http://gcs:4443")


def test_development_accepts_the_emulator_and_production_works_without_it():
    GcpSettings(_env_file=None, ENVIRONMENT="development", STORAGE_EMULATOR_HOST="http://gcs:4443")
    GcpSettings(
        _env_file=None,
        ENVIRONMENT="production",
        GCS_BUCKET="pose-videos",
        # La cola también es obligatoria en producción (ver test_gcp_tasks.py).
        GCP_PROJECT="proyecto",
        WORKER_URL="https://pose-worker.run.app",
        TASKS_SERVICE_ACCOUNT="pose-tasks@proyecto.iam.gserviceaccount.com",
    )


# --- Qué cliente se arma ---


class RecordingClient:
    """Registra cómo se construyó el cliente y qué llamadas hizo."""

    instances: list["RecordingClient"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.calls: list[tuple[str, str]] = []
        RecordingClient.instances.append(self)

    def bucket(self, name):
        self.calls.append(("bucket", name))
        return f"bucket:{name}"

    def lookup_bucket(self, name):
        self.calls.append(("lookup_bucket", name))
        return None

    def create_bucket(self, name):
        self.calls.append(("create_bucket", name))
        return f"creado:{name}"


@pytest.fixture
def recording_client(monkeypatch):
    RecordingClient.instances = []
    monkeypatch.setattr(cloud_storage.storage, "Client", RecordingClient)
    return RecordingClient


def test_in_production_the_client_uses_the_service_account(monkeypatch, recording_client):
    """
    Sin argumentos: credenciales y proyecto salen del entorno (la cuenta de
    servicio en Cloud Run). Y `bucket()` en vez de `get_bucket()`: no hace
    llamadas, así que la cuenta no necesita permiso para leer la
    configuración del bucket, solo sobre los objetos.
    """
    monkeypatch.setattr(gcp_settings, "STORAGE_EMULATOR_HOST", "")
    monkeypatch.setattr(gcp_settings, "GCS_BUCKET", "pose-videos")

    assert cloud_storage._get_bucket() == "bucket:pose-videos"
    (client,) = recording_client.instances
    assert client.kwargs == {}
    assert client.calls == [("bucket", "pose-videos")]


def test_with_the_emulator_the_client_is_anonymous_and_creates_the_bucket(monkeypatch, recording_client):
    monkeypatch.setattr(gcp_settings, "STORAGE_EMULATOR_HOST", "http://gcs:4443")
    monkeypatch.setattr(gcp_settings, "GCS_BUCKET", "pose-local")

    assert cloud_storage._get_bucket() == "creado:pose-local"
    (client,) = recording_client.instances
    assert isinstance(client.kwargs["credentials"], AnonymousCredentials)
    assert client.kwargs["client_options"] == {"api_endpoint": "http://gcs:4443"}


def test_the_client_is_built_once_per_process(monkeypatch, recording_client):
    monkeypatch.setattr(gcp_settings, "STORAGE_EMULATOR_HOST", "")
    monkeypatch.setattr(gcp_settings, "GCS_BUCKET", "pose-videos")

    cloud_storage._get_bucket()
    cloud_storage._get_bucket()

    assert len(recording_client.instances) == 1


# --- Cómo se interpreta cada respuesta ---


class FakeBlob:
    def __init__(self, missing: bool) -> None:
        self.missing = missing

    def delete(self, timeout=None):
        if self.missing:
            raise NotFound("no está")


@pytest.mark.parametrize("missing, expected", [(False, True), (True, False)])
def test_deleting_a_video_that_is_gone_is_not_an_error(monkeypatch, missing, expected):
    """El barrido de retención puede pasar después de que el worker lo borró."""
    bucket = type("Bucket", (), {"blob": lambda self, path: FakeBlob(missing)})()
    monkeypatch.setattr(cloud_storage, "_bucket", bucket)

    assert asyncio.run(cloud_storage.delete_video("videos/x.mp4")) is expected


# --- La regla de ciclo de vida del bucket (deploy/gcp/storage-lifecycle.json) ---


def _lifecycle_rule() -> dict:
    config = json.loads((ROOT / "deploy" / "gcp" / "storage-lifecycle.json").read_text(encoding="utf-8"))
    (rule,) = config["lifecycle"]["rule"]
    return rule


def test_the_lifecycle_rule_never_deletes_a_video_a_live_job_needs():
    """
    Un video solo hace falta mientras su job está en cola o procesándose.
    Pasados esos umbrales, el barrido de jobs atascados ya lo dio por perdido.
    Si la regla borrara antes, un job lento perdería su video a mitad de camino.
    """
    rule = _lifecycle_rule()
    age_minutes = rule["condition"]["age"] * 24 * 60

    assert rule["action"] == {"type": "Delete"}
    assert age_minutes > settings.STUCK_JOB_QUEUED_TIMEOUT_MINUTES + settings.STUCK_JOB_TIMEOUT_MINUTES


def test_the_lifecycle_rule_is_stricter_than_the_retention_policy():
    """Es la red de seguridad de la Ley 1581: no puede conservar más que la retención."""
    assert _lifecycle_rule()["condition"]["age"] <= settings.DATA_RETENTION_DAYS


def test_the_lifecycle_rule_covers_where_the_videos_are_stored():
    (prefix,) = _lifecycle_rule()["condition"]["matchesPrefix"]

    assert new_video_path("marcha.mp4").startswith(prefix)


# --- Contra el emulador ---


@pytest.fixture
def emulator(monkeypatch):
    if not EMULATOR:
        pytest.skip("GCS_EMULATOR_FOR_TESTS no está definido (ver el docstring del módulo)")
    monkeypatch.setattr(gcp_settings, "STORAGE_EMULATOR_HOST", EMULATOR)
    # Un bucket por test: el emulador es compartido y no se limpia entre tests.
    monkeypatch.setattr(gcp_settings, "GCS_BUCKET", f"pruebas-{uuid.uuid4().hex[:12]}")
    return cloud_storage


def _path(name: str = "marcha.mp4") -> str:
    return f"videos/{uuid.uuid4().hex}_{name}"


def test_a_video_survives_upload_download_and_delete(emulator, tmp_path):
    content = b"\x00\x00\x00\x18ftypmp42" + os.urandom(4096)
    path = _path()

    asyncio.run(emulator.upload_video(content, path, "video/mp4"))
    destination = tmp_path / "bajado.mp4"
    asyncio.run(emulator.download_video(path, str(destination)))

    assert destination.read_bytes() == content
    assert emulator._get_bucket().get_blob(path).content_type == "video/mp4"
    assert asyncio.run(emulator.delete_video(path)) is True
    assert asyncio.run(emulator.delete_video(path)) is False


def test_a_video_over_8_mb_goes_by_resumable_upload(emulator, tmp_path):
    """
    Desde 8 MB la librería sube en modo reanudable, que es otro camino (otra
    URL, varias solicitudes). Los videos reales pesan 28-43 MB.
    """
    content = os.urandom(9 * 1024 * 1024)
    path = _path("largo.mp4")

    asyncio.run(emulator.upload_video(content, path, "video/mp4"))
    destination = tmp_path / "largo.mp4"
    asyncio.run(emulator.download_video(path, str(destination)))

    assert destination.read_bytes() == content


def test_downloading_a_missing_video_raises_and_leaves_no_file(emulator, tmp_path):
    destination = tmp_path / "nada.mp4"

    with pytest.raises(NotFound):
        asyncio.run(emulator.download_video(_path("nada.mp4"), str(destination)))

    assert not destination.exists()


def test_the_storage_check_passes_against_the_emulator(emulator, capsys):
    from app.tools import storage_check

    assert storage_check.main(["--plataforma", "gcp"]) == 0
    output = capsys.readouterr().out
    assert "cloud_storage" in output and "OK" in output
    assert list(emulator._get_bucket().list_blobs()) == []  # no deja basura
