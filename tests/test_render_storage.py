"""
Almacenamiento del video en Render: Supabase Storage
(app/platforms/render/supabase_storage.py) y la elección entre Supabase y
Firebase (`video_storage()` en app/platforms/render/__init__.py).

La API de Storage se simula con httpx.MockTransport: se comprueba qué
solicitud sale (método, ruta, headers) y cómo se interpreta cada respuesta.
Que la API real acepte esas solicitudes lo comprueba
`python -m app.tools.storage_check` contra el proyecto (ver README).
"""

import asyncio
import json

import httpx
import pytest

from app.platforms import render
from app.platforms.render import supabase_storage
from app.platforms.render.config import RenderSettings, render_settings

PROJECT = "https://abcdefghij.supabase.co"
PATH = "videos/0123456789abcdef0123456789abcdef.mp4"


class FakeStorage:
    """Un Storage en memoria que registra cada solicitud."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        upload_prefix = "/storage/v1/object/videos/"
        download_prefix = "/storage/v1/object/authenticated/videos/"

        if request.method == "POST" and path.startswith(upload_prefix):
            key = path[len(upload_prefix):]
            if key in self.objects:
                return httpx.Response(400, json={"error": "Duplicate"})
            self.objects[key] = request.content
            return httpx.Response(200, json={"Key": f"videos/{key}"})

        if request.method == "GET" and path.startswith(download_prefix):
            key = path[len(download_prefix):]
            if key not in self.objects:
                return httpx.Response(400, json={"statusCode": "404", "error": "not_found"})
            return httpx.Response(200, content=self.objects[key])

        if request.method == "DELETE" and path == "/storage/v1/object/videos":
            deleted = [{"name": key} for key in json.loads(request.content)["prefixes"]
                       if self.objects.pop(key, None) is not None]
            return httpx.Response(200, json=deleted)

        return httpx.Response(400, json={"error": "Bucket not found"})


@pytest.fixture
def storage(monkeypatch):
    fake = FakeStorage()
    monkeypatch.setattr(render_settings, "SUPABASE_URL", PROJECT)
    monkeypatch.setattr(render_settings, "SUPABASE_SECRET_KEY", "sb_secret_prueba")
    monkeypatch.setattr(render_settings, "SUPABASE_STORAGE_BUCKET", "videos")

    real_client = supabase_storage._client

    def client_with_fake_transport():
        client = real_client()
        client._transport = httpx.MockTransport(fake.handler)
        return client

    monkeypatch.setattr(supabase_storage, "_client", client_with_fake_transport)
    return fake


def test_a_video_survives_upload_download_and_delete(storage, tmp_path):
    content = b"\x00\x00\x00\x18ftypmp42" + b"x" * 1000

    asyncio.run(supabase_storage.upload_video(content, PATH, "video/mp4"))
    destination = tmp_path / "bajado.mp4"
    asyncio.run(supabase_storage.download_video(PATH, str(destination)))

    assert destination.read_bytes() == content
    assert asyncio.run(supabase_storage.delete_video(PATH)) is True
    assert storage.objects == {}


def test_the_video_is_stored_at_the_path_it_is_given(storage):
    asyncio.run(supabase_storage.upload_video(b"datos", PATH, "video/mp4"))

    assert storage.requests[0].url.path == f"/storage/v1/object/videos/{PATH}"


def test_deleting_a_video_that_is_gone_is_not_an_error(storage):
    """El barrido de retención puede pasar después del worker."""
    assert asyncio.run(supabase_storage.delete_video("videos/ya-no-esta.mp4")) is False


def test_the_upload_never_overwrites_and_declares_the_type(storage):
    asyncio.run(supabase_storage.upload_video(b"datos", PATH, "video/mp4"))
    upload = storage.requests[0]

    assert upload.headers["x-upsert"] == "false"
    assert upload.headers["content-type"] == "video/mp4"


def test_the_new_secret_key_goes_only_in_the_apikey_header(storage):
    """Las claves sb_secret_ no son JWT: mandarlas como Bearer no autentica."""
    asyncio.run(supabase_storage.upload_video(b"datos", PATH, "video/mp4"))
    request = storage.requests[0]

    assert request.headers["apikey"] == "sb_secret_prueba"
    assert "authorization" not in request.headers


def test_a_legacy_service_role_key_also_goes_as_bearer(storage, monkeypatch):
    monkeypatch.setattr(render_settings, "SUPABASE_SECRET_KEY", "eyJhbGciOi.legado.firma")
    asyncio.run(supabase_storage.upload_video(b"datos", PATH, "video/mp4"))
    request = storage.requests[0]

    assert request.headers["apikey"] == "eyJhbGciOi.legado.firma"
    assert request.headers["authorization"] == "Bearer eyJhbGciOi.legado.firma"


def test_a_storage_error_is_raised_with_the_reason_and_without_the_key(storage, monkeypatch):
    monkeypatch.setattr(render_settings, "SUPABASE_STORAGE_BUCKET", "no-existe")

    with pytest.raises(supabase_storage.StorageError) as error:
        asyncio.run(supabase_storage.upload_video(b"datos", PATH, "video/mp4"))

    assert "Bucket not found" in str(error.value)
    assert "sb_secret_prueba" not in str(error.value)


def test_downloading_a_missing_video_raises(storage, tmp_path):
    with pytest.raises(supabase_storage.StorageError, match="descargar"):
        asyncio.run(supabase_storage.download_video("videos/nada.mp4", str(tmp_path / "x.mp4")))


# --- Elección del almacenamiento ---


@pytest.mark.parametrize(
    "backend, module",
    [
        ("postgres", "postgres_storage"),
        ("supabase", "supabase_storage"),
        ("r2", "r2_storage"),
        ("firebase", "firebase_storage"),
    ],
)
def test_the_configured_backend_is_the_one_used(monkeypatch, backend, module):
    monkeypatch.setattr(render_settings, "STORAGE_BACKEND", backend)

    assert render.video_storage().__name__ == f"app.platforms.render.{module}"


# --- Configuración ---


def _settings(**overrides) -> RenderSettings:
    return RenderSettings(_env_file=None, **overrides)


def test_an_unknown_backend_is_rejected():
    with pytest.raises(ValueError, match="STORAGE_BACKEND"):
        _settings(STORAGE_BACKEND="s3")


def test_the_backend_name_is_normalized():
    assert _settings(STORAGE_BACKEND=" Supabase ").STORAGE_BACKEND == "supabase"


def test_the_default_is_the_database():
    """Desde el 2026-10-08 todo va en Render: el video espera en la misma base."""
    assert _settings().STORAGE_BACKEND == "postgres"


def test_supabase_without_url_or_key_does_not_start(monkeypatch):
    monkeypatch.setattr(render_settings, "SUPABASE_SECRET_KEY", "sb_secret_x")

    monkeypatch.setattr(render_settings, "SUPABASE_URL", "http://abc.supabase.co")
    with pytest.raises(RuntimeError, match="https://"):
        supabase_storage.ensure_configured()

    monkeypatch.setattr(render_settings, "SUPABASE_URL", "https://CAMBIAR-ref.supabase.co")
    with pytest.raises(RuntimeError, match="relleno"):
        supabase_storage.ensure_configured()

    monkeypatch.setattr(render_settings, "SUPABASE_URL", PROJECT)
    monkeypatch.setattr(render_settings, "SUPABASE_SECRET_KEY", " ")
    with pytest.raises(RuntimeError, match="SUPABASE_SECRET_KEY"):
        supabase_storage.ensure_configured()


def test_supabase_configured_starts(monkeypatch):
    monkeypatch.setattr(render_settings, "SUPABASE_URL", PROJECT)
    monkeypatch.setattr(render_settings, "SUPABASE_SECRET_KEY", "sb_secret_x")

    supabase_storage.ensure_configured()


def test_the_api_checks_the_configured_supabase_when_it_starts(monkeypatch):
    """El puerto común llega hasta la comprobación de Supabase."""
    from app.utils import video_storage

    monkeypatch.setattr(render_settings, "STORAGE_BACKEND", "supabase")
    monkeypatch.setattr(render_settings, "SUPABASE_URL", PROJECT)
    monkeypatch.setattr(render_settings, "SUPABASE_SECRET_KEY", "")

    with pytest.raises(RuntimeError, match="SUPABASE_SECRET_KEY"):
        video_storage.ensure_configured()


def test_beat_can_start_without_the_storage_key():
    """Beat construye la configuración con el mismo grupo de variables, sin la clave."""
    config = _settings(ENVIRONMENT="production", STORAGE_BACKEND="supabase", SUPABASE_URL=PROJECT)
    assert config.SUPABASE_SECRET_KEY == ""


# --- Herramienta de prueba de humo ---


def test_the_storage_check_passes_against_a_working_storage(storage, monkeypatch, capsys):
    from app.tools import storage_check

    monkeypatch.setattr(render_settings, "STORAGE_BACKEND", "supabase")

    assert storage_check.main(["--plataforma", "render"]) == 0
    output = capsys.readouterr().out
    assert "supabase_storage" in output and "OK" in output
    assert storage.objects == {}  # no deja basura en el bucket


def test_the_storage_check_fails_when_the_bucket_is_wrong(storage, monkeypatch, capsys):
    from app.tools import storage_check

    monkeypatch.setattr(render_settings, "STORAGE_BACKEND", "supabase")
    monkeypatch.setattr(render_settings, "SUPABASE_STORAGE_BUCKET", "no-existe")

    assert storage_check.main(["--plataforma", "render"]) == 1
    assert "Bucket not found" in capsys.readouterr().out


def test_the_storage_check_fails_before_uploading_if_unconfigured(storage, monkeypatch, capsys):
    from app.tools import storage_check

    monkeypatch.setattr(render_settings, "STORAGE_BACKEND", "supabase")
    monkeypatch.setattr(render_settings, "SUPABASE_SECRET_KEY", "")

    assert storage_check.main(["--plataforma", "render"]) == 1
    assert "SUPABASE_SECRET_KEY" in capsys.readouterr().out
    assert storage.requests == []
