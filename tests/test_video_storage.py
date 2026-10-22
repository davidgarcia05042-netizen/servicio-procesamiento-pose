"""
El puerto del almacenamiento (app/utils/video_storage.py): decide la ruta de
cada video y delega el resto en el almacenamiento de la plataforma activa.

Los adaptadores de cada plataforma se prueban en su propio archivo
(test_render_storage.py, test_gcp_storage.py). Acá se comprueba lo común: el
nombre, la delegación y que todos los adaptadores cumplan el mismo contrato.
Que nadie de la parte común llegue a un adaptador saltándose este puerto lo
comprueba test_platform_boundaries.py (lo común no importa paquetes de
plataforma).
"""

import asyncio
import inspect
import re
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import platforms
from app.utils import video_storage

ROOT = Path(__file__).resolve().parent.parent

# Todos los adaptadores que existen, de todas las plataformas (también las que
# están en construcción).
ADAPTERS = [
    "app.platforms.render.postgres_storage",
    "app.platforms.render.firebase_storage",
    "app.platforms.render.supabase_storage",
    "app.platforms.gcp.cloud_storage",
]


class FakeBackend:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.configured_checks = 0

    def ensure_configured(self) -> None:
        self.configured_checks += 1

    async def upload_video(self, content, path, content_type):
        self.objects[path] = (content, content_type)

    async def download_video(self, path, destination):
        Path(destination).write_bytes(self.objects[path][0])

    async def delete_video(self, path):
        return self.objects.pop(path, None) is not None


@pytest.fixture
def backend(monkeypatch):
    fake = FakeBackend()
    monkeypatch.setattr(platforms, "current", lambda: SimpleNamespace(video_storage=lambda: fake))
    return fake


# --- La ruta de cada video ---


def test_the_path_is_unique():
    first = video_storage.new_video_path("marcha.mp4")
    second = video_storage.new_video_path("marcha.mp4")

    assert re.fullmatch(r"videos/[0-9a-f]{32}\.mp4", first)
    assert first != second


@pytest.mark.parametrize(
    "filename",
    ["marcha_ana_perez.mp4", "../Sesión 3 rodilla de Juan.MP4", "paciente 1234567.mov"],
)
def test_nothing_of_the_client_filename_but_the_extension_reaches_the_path(filename):
    """
    El nombre puede identificar al paciente, y por la ruta terminaba en los
    logs, en `jobs.video_url` y en el archivo temporal del worker.
    """
    path = video_storage.new_video_path(filename)

    assert re.fullmatch(r"videos/[0-9a-f]{32}\.(mp4|mov)", path), path


def test_the_extension_is_kept_in_lowercase():
    """OpenCV elige el demuxer en parte por la extensión."""
    assert video_storage.new_video_path("Sesión.MOV").endswith(".mov")


@pytest.mark.parametrize("filename", ["", "...", "Ñ", "video", "video.mp4;rm", "x.muylarga"])
def test_without_a_plain_extension_the_path_ends_in_mp4(filename):
    assert re.fullmatch(r"videos/[0-9a-f]{32}\.mp4", video_storage.new_video_path(filename))


# --- Delegación en la plataforma ---


def test_the_upload_stores_at_the_path_it_is_given(backend, tmp_path):
    path = video_storage.new_video_path("marcha.mp4")
    asyncio.run(video_storage.upload_video(b"datos", path, "video/mp4"))

    assert backend.objects == {path: (b"datos", "video/mp4")}

    destination = tmp_path / "bajado.mp4"
    asyncio.run(video_storage.download_video(path, str(destination)))
    assert destination.read_bytes() == b"datos"

    assert asyncio.run(video_storage.delete_video(path)) is True
    assert asyncio.run(video_storage.delete_video(path)) is False


def test_the_startup_check_is_the_platform_one(backend):
    video_storage.ensure_configured()

    assert backend.configured_checks == 1


def test_the_port_upload_takes_the_path_like_the_adapters():
    """La ruta la decide quien sube, antes de subir (ver video_processing_service.py)."""
    assert list(inspect.signature(video_storage.upload_video).parameters) == ["content", "path", "content_type"]


def test_the_api_and_the_worker_check_the_storage_when_they_start():
    for relative in ("app/main.py", "app/platforms/render/worker.py"):
        assert "video_storage.ensure_configured()" in (ROOT / relative).read_text(encoding="utf-8"), relative


# --- Todos los adaptadores cumplen el mismo contrato ---


@pytest.mark.parametrize("module_name", ADAPTERS)
def test_every_adapter_implements_the_storage_contract(module_name):
    module = import_module(module_name)

    assert callable(module.ensure_configured)
    assert not inspect.iscoroutinefunction(module.ensure_configured)
    for name in ("upload_video", "download_video", "delete_video"):
        assert inspect.iscoroutinefunction(getattr(module, name)), name
    assert list(inspect.signature(module.upload_video).parameters)[1] in {"path", "blob_path"}


def test_the_adapter_list_covers_every_platform():
    """Un adaptador nuevo que no esté en ADAPTERS quedaría sin la prueba de arriba."""
    found = {
        import_module(f"app.platforms.{package.name}").video_storage().__name__
        for package in (ROOT / "app" / "platforms").iterdir()
        if (package / "__init__.py").exists()
    }
    assert found <= set(ADAPTERS)
