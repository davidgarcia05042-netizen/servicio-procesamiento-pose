"""
Almacenamiento del video en Cloudflare R2 (app/platforms/render/r2_storage.py).

El cliente de S3 se reemplaza por uno en memoria que imita las respuestas de
R2 (incluidos los errores como los arma botocore). Ninguna prueba sale a la
red: la que arma el cliente real comprueba endpoint, región y firma sin hacer
ninguna solicitud. Que R2 de verdad acepte las solicitudes lo comprueba
`python -m app.tools.storage_check` contra el bucket (ver README).
"""

import asyncio
import io

import pytest
from botocore.exceptions import ClientError
from botocore.response import StreamingBody

from app.platforms.render import r2_storage
from app.platforms.render.config import RenderSettings, render_settings

ACCOUNT = "0123456789abcdef0123456789abcdef"
ACCESS = "a" * 32
SECRET = "s" * 64
PATH = "videos/0123456789abcdef0123456789abcdef.mp4"


def _client_error(code: str, status: int, operation: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": f"mensaje de {code}"}, "ResponseMetadata": {"HTTPStatusCode": status}},
        operation,
    )


class FakeS3:
    """Un bucket de R2 en memoria que registra cada llamada."""

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.calls: list[tuple[str, dict]] = []
        self.bucket = "pose-videos"

    def _check_bucket(self, bucket: str, operation: str) -> None:
        if bucket != self.bucket:
            raise _client_error("NoSuchBucket", 404, operation)

    def put_object(self, **kwargs):
        self.calls.append(("put_object", kwargs))
        self._check_bucket(kwargs["Bucket"], "PutObject")
        self.objects[kwargs["Key"]] = (kwargs["Body"], kwargs["ContentType"])
        return {}

    def get_object(self, **kwargs):
        self.calls.append(("get_object", kwargs))
        self._check_bucket(kwargs["Bucket"], "GetObject")
        if kwargs["Key"] not in self.objects:
            raise _client_error("NoSuchKey", 404, "GetObject")
        content = self.objects[kwargs["Key"]][0]
        return {"Body": StreamingBody(io.BytesIO(content), len(content))}

    def head_object(self, **kwargs):
        self.calls.append(("head_object", kwargs))
        self._check_bucket(kwargs["Bucket"], "HeadObject")
        if kwargs["Key"] not in self.objects:
            # HeadObject no trae cuerpo: botocore pone el código HTTP como Code.
            raise _client_error("404", 404, "HeadObject")
        return {}

    def delete_object(self, **kwargs):
        self.calls.append(("delete_object", kwargs))
        self._check_bucket(kwargs["Bucket"], "DeleteObject")
        self.objects.pop(kwargs["Key"], None)  # S3 responde 204 exista o no
        return {}


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(render_settings, "R2_ACCOUNT_ID", ACCOUNT)
    monkeypatch.setattr(render_settings, "R2_ACCESS_KEY_ID", ACCESS)
    monkeypatch.setattr(render_settings, "R2_SECRET_ACCESS_KEY", SECRET)
    monkeypatch.setattr(render_settings, "R2_BUCKET", "pose-videos")


@pytest.fixture
def s3(configured, monkeypatch):
    fake = FakeS3()
    monkeypatch.setattr(r2_storage, "_client", lambda: fake)
    return fake


# --- Contrato ---


def test_a_video_survives_upload_download_and_delete(s3, tmp_path):
    content = b"\x00\x00\x00\x18ftypmp42" + b"x" * (3 * 1024 * 1024)  # más de una parte

    asyncio.run(r2_storage.upload_video(content, PATH, "video/mp4"))
    destination = tmp_path / "bajado.mp4"
    asyncio.run(r2_storage.download_video(PATH, str(destination)))

    assert destination.read_bytes() == content
    assert asyncio.run(r2_storage.delete_video(PATH)) is True
    assert s3.objects == {}


def test_the_video_is_stored_at_the_path_it_is_given_with_its_type(s3):
    asyncio.run(r2_storage.upload_video(b"datos", PATH, "video/mp4"))

    name, call = s3.calls[0]
    assert name == "put_object"
    assert call["Bucket"] == "pose-videos" and call["Key"] == PATH
    assert call["ContentType"] == "video/mp4"


def test_deleting_a_video_that_is_gone_is_not_an_error(s3):
    """El barrido de retención puede pasar después del worker."""
    assert asyncio.run(r2_storage.delete_video("videos/ya-no-esta.mp4")) is False
    assert [name for name, _ in s3.calls] == ["head_object"]  # no intenta borrar


def test_downloading_a_missing_video_raises(s3, tmp_path):
    with pytest.raises(r2_storage.StorageError, match="descargar"):
        asyncio.run(r2_storage.download_video("videos/nada.mp4", str(tmp_path / "x.mp4")))


def test_an_error_names_the_reason_and_never_the_credentials(s3, monkeypatch):
    monkeypatch.setattr(render_settings, "R2_BUCKET", "no-existe")

    with pytest.raises(r2_storage.StorageError) as error:
        asyncio.run(r2_storage.upload_video(b"datos", PATH, "video/mp4"))

    message = str(error.value)
    assert "NoSuchBucket" in message
    assert ACCESS not in message and SECRET not in message


def test_a_failure_checking_before_delete_is_an_error_not_a_missing_video(s3, monkeypatch):
    """Un 403 (token sin permiso) no puede leerse como "ya no estaba"."""

    def forbidden(**kwargs):
        raise _client_error("403", 403, "HeadObject")

    monkeypatch.setattr(s3, "head_object", forbidden)
    with pytest.raises(r2_storage.StorageError, match="consultar"):
        asyncio.run(r2_storage.delete_video(PATH))


# --- El cliente real, sin red ---


def test_the_client_points_to_the_account_endpoint_with_region_auto(configured):
    client = r2_storage._client()

    assert client.meta.endpoint_url == f"https://{ACCOUNT}.r2.cloudflarestorage.com"
    assert client.meta.region_name == "auto"
    assert client.meta.config.signature_version == "s3v4"


# --- Configuración ---


def test_r2_configured_starts(configured):
    r2_storage.ensure_configured()


@pytest.mark.parametrize(
    "variable, value, message",
    [
        ("R2_ACCOUNT_ID", "", "R2_ACCOUNT_ID"),
        ("R2_ACCOUNT_ID", "no-es-un-id", "R2_ACCOUNT_ID"),
        ("R2_BUCKET", " ", "R2_BUCKET"),
        ("R2_ACCESS_KEY_ID", "", "R2_ACCESS_KEY_ID"),
        ("R2_SECRET_ACCESS_KEY", "", "R2_SECRET_ACCESS_KEY"),
    ],
)
def test_r2_without_its_values_does_not_start(configured, monkeypatch, variable, value, message):
    monkeypatch.setattr(render_settings, variable, value)

    with pytest.raises(RuntimeError, match=message):
        r2_storage.ensure_configured()


def test_swapped_keys_are_caught_at_startup_without_showing_them(configured, monkeypatch):
    """El error real del 2026-10-22: el Secret pegado donde va el Access Key ID."""
    monkeypatch.setattr(render_settings, "R2_ACCESS_KEY_ID", SECRET)

    with pytest.raises(RuntimeError) as error:
        r2_storage.ensure_configured()

    assert "R2_ACCESS_KEY_ID" in str(error.value) and "64" in str(error.value)
    assert SECRET not in str(error.value)


def test_surrounding_spaces_in_the_keys_are_tolerated(configured, monkeypatch):
    monkeypatch.setattr(render_settings, "R2_ACCESS_KEY_ID", f" {ACCESS}\n")
    monkeypatch.setattr(render_settings, "R2_SECRET_ACCESS_KEY", f"{SECRET} ")

    r2_storage.ensure_configured()


def test_r2_is_an_accepted_backend():
    assert RenderSettings(_env_file=None, STORAGE_BACKEND=" R2 ").STORAGE_BACKEND == "r2"


def test_beat_can_start_without_the_r2_keys():
    """Beat construye la configuración con el mismo grupo de variables, sin las claves."""
    config = RenderSettings(_env_file=None, ENVIRONMENT="production", STORAGE_BACKEND="r2", R2_ACCOUNT_ID=ACCOUNT)
    assert config.R2_ACCESS_KEY_ID == "" and config.R2_SECRET_ACCESS_KEY == ""


def test_the_api_checks_r2_when_it_starts(configured, monkeypatch):
    """El puerto común llega hasta la comprobación de R2."""
    from app.utils import video_storage

    monkeypatch.setattr(render_settings, "STORAGE_BACKEND", "r2")
    monkeypatch.setattr(render_settings, "R2_SECRET_ACCESS_KEY", "")

    with pytest.raises(RuntimeError, match="R2_SECRET_ACCESS_KEY"):
        video_storage.ensure_configured()


# --- Herramienta de prueba de humo ---


def test_the_storage_check_passes_against_a_working_r2(s3, monkeypatch, capsys):
    from app.tools import storage_check

    monkeypatch.setattr(render_settings, "STORAGE_BACKEND", "r2")

    assert storage_check.main(["--plataforma", "render"]) == 0
    output = capsys.readouterr().out
    assert "r2_storage" in output and "OK" in output
    assert s3.objects == {}  # no deja basura en el bucket
