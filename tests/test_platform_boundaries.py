"""
Separación entre la parte común y las plataformas de despliegue
(app/platforms/__init__.py).

La regla: lo común (validar, analizar, ángulos, callbacks, retención) es uno
solo, y lo de cada plataforma vive en su paquete sin cruzarse con las demás.
Estos tests la hacen cumplir: un import de Celery o de una librería de Google
en app/core, o una plataforma a la que le falta una función, rompen la suite
en vez de descubrirse al desplegar.
"""

import ast
import subprocess
import sys
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import platforms
from app.core import task_queue
from app.core.config import PLATFORMS_UNDER_CONSTRUCTION, SUPPORTED_PLATFORMS, Settings, settings

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"

# Lo común: todo app/ salvo los paquetes de plataforma y las herramientas de
# desarrollo (app/tools miden o demuestran una plataforma concreta).
COMMON = [
    path
    for path in APP.rglob("*.py")
    if "__pycache__" not in path.parts
    and not path.is_relative_to(APP / "platforms")
    and not path.is_relative_to(APP / "tools")
]

# Librerías que solo puede importar una plataforma. "google" cubre
# google.cloud.* y google.auth (Firebase y Cloud Storage por igual).
PLATFORM_ONLY_LIBRARIES = {"celery", "kombu", "redis", "firebase_admin", "google", "boto3", "botocore"}

# Los mismos nombres en requirements/ (los de pip, no los de import).
PLATFORM_ONLY_DISTRIBUTIONS = {"celery", "kombu", "redis", "firebase-admin", "boto3", "botocore"}
PLATFORM_ONLY_DISTRIBUTION_PREFIXES = ("google-cloud-",)

INTERFACE = (
    "enqueue_video_processing",
    "enqueue_callback",
    "is_private_network_host",
    "health_checks",
    "close",
    "video_storage",
)

# Todos los paquetes de app/platforms/, también los que están en construcción
# (que todavía no se pueden elegir con PLATFORM).
PLATFORM_PACKAGES = sorted(
    path.name for path in (APP / "platforms").iterdir() if (path / "__init__.py").exists()
)


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def test_common_code_does_not_import_platform_libraries():
    offenders = {
        str(path.relative_to(ROOT)): sorted(
            name for name in _imported_modules(path)
            if name.split(".")[0] in PLATFORM_ONLY_LIBRARIES
        )
        for path in COMMON
    }
    assert {k: v for k, v in offenders.items() if v} == {}


def test_common_code_reaches_platforms_only_through_the_selector():
    """`from app import platforms` sí; `app.platforms.render...` directo, no."""
    offenders = {
        str(path.relative_to(ROOT)): sorted(
            name for name in _imported_modules(path)
            for package in PLATFORM_PACKAGES
            if name == f"app.platforms.{package}" or name.startswith(f"app.platforms.{package}.")
        )
        for path in COMMON
    }
    assert {k: v for k, v in offenders.items() if v} == {}


def test_platforms_do_not_import_each_other():
    for platform in PLATFORM_PACKAGES:
        others = set(PLATFORM_PACKAGES) - {platform}
        for path in (APP / "platforms" / platform).rglob("*.py"):
            imported = _imported_modules(path)
            crossing = {
                name for name in imported
                for other in others if name.startswith(f"app.platforms.{other}")
            }
            assert not crossing, (path, crossing)


@pytest.mark.parametrize("platform", PLATFORM_PACKAGES)
def test_every_platform_implements_the_whole_interface(platform):
    """También las que están en construcción: en desarrollo se pueden elegir."""
    module = import_module(f"app.platforms.{platform}")

    missing = [name for name in INTERFACE if not callable(getattr(module, name, None))]
    assert missing == []


def test_every_platform_package_is_either_supported_or_under_construction():
    assert set(PLATFORM_PACKAGES) == SUPPORTED_PLATFORMS | PLATFORMS_UNDER_CONSTRUCTION
    assert not (SUPPORTED_PLATFORMS & PLATFORMS_UNDER_CONSTRUCTION)


@pytest.mark.parametrize("platform", sorted(PLATFORMS_UNDER_CONSTRUCTION))
def test_a_platform_under_construction_cannot_go_to_production(platform):
    """
    Una plataforma a medias desplegada en serio se vería sana y le faltaría algo
    que nadie nota a tiempo. Se habilita pasándola a SUPPORTED_PLATFORMS.
    """
    with pytest.raises(ValueError, match="en construcción"):
        Settings(_env_file=None, PLATFORM=platform, ENVIRONMENT="production")


@pytest.mark.parametrize("platform", sorted(PLATFORMS_UNDER_CONSTRUCTION))
def test_a_platform_under_construction_can_be_tried_in_development(platform):
    """Para probarla de punta a punta con los emuladores (ver README)."""
    assert Settings(_env_file=None, PLATFORM=platform).PLATFORM == platform


@pytest.mark.parametrize("platform", sorted(SUPPORTED_PLATFORMS))
def test_a_supported_platform_goes_to_production(platform):
    assert Settings(_env_file=None, PLATFORM=platform, ENVIRONMENT="production").PLATFORM == platform


def test_starting_the_api_does_not_load_the_platform_libraries():
    """
    La API solo construye la app de Celery al encolar el primer video, y solo
    carga la librería del almacenamiento al usarlo. Se comprueba en un proceso
    aparte porque en este la suite ya importó todo.
    """
    libraries = ("celery", "kombu", "redis", "firebase_admin", "google.cloud.storage")
    code = (
        "import sys, app.main; "
        f"print(sorted(m for m in {libraries!r} if m in sys.modules))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "[]"


# --- El selector y el puerto de la cola ---


def test_an_unknown_platform_is_rejected():
    with pytest.raises(ValueError, match="PLATFORM"):
        Settings(_env_file=None, PLATFORM="heroku")


def test_the_platform_name_is_normalized():
    assert Settings(_env_file=None, PLATFORM=" Render ").PLATFORM == "render"


def test_the_queue_port_delegates_to_the_active_platform(monkeypatch):
    calls = []
    fake = SimpleNamespace(
        enqueue_video_processing=lambda job_id: calls.append(("video", job_id)),
        enqueue_callback=lambda job_id: calls.append(("callback", job_id)),
    )
    monkeypatch.setattr(platforms, "current", lambda: fake)

    task_queue.enqueue_video_processing("job-1")
    task_queue.enqueue_callback("job-2")

    assert calls == [("video", "job-1"), ("callback", "job-2")]


def test_the_selector_returns_the_configured_package(monkeypatch):
    monkeypatch.setattr(settings, "PLATFORM", "render")

    assert platforms.current().__name__ == "app.platforms.render"


# --- Imagen y dependencias ---


def _requirement_names(path: Path) -> set[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return {
        line.split("==")[0].split("[")[0].strip().lower()
        for line in lines
        if line.strip() and not line.lstrip().startswith(("#", "-r"))
    }


def test_the_common_requirements_carry_no_platform_library():
    packages = _requirement_names(ROOT / "requirements" / "base.txt")

    assert not (packages & PLATFORM_ONLY_DISTRIBUTIONS)
    assert not [p for p in packages if p.startswith(PLATFORM_ONLY_DISTRIBUTION_PREFIXES)]


@pytest.mark.parametrize("platform", PLATFORM_PACKAGES)
def test_each_platform_package_has_its_own_requirements(platform):
    requirements = ROOT / "requirements" / f"{platform}.txt"

    assert "-r base.txt" in requirements.read_text(encoding="utf-8")


def test_the_dev_requirements_install_every_platform():
    """La suite prueba el código de todas las plataformas: necesita sus librerías."""
    dev = (ROOT / "requirements-dev.txt").read_text(encoding="utf-8")

    for platform in PLATFORM_PACKAGES:
        assert f"-r requirements/{platform}.txt" in dev, platform


def test_the_ci_cache_follows_every_requirements_file():
    """
    requirements-dev.txt solo incluye a los de requirements/ con `-r`: si la
    clave del caché de la CI mirara solo ese archivo, un cambio en
    requirements/base.txt dejaría instaladas las versiones viejas.
    """
    from fnmatch import fnmatch

    import yaml

    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8"))
    patterns = [
        line.strip()
        for job in workflow["jobs"].values()
        for step in job["steps"]
        for line in str(step.get("with", {}).get("cache-dependency-path", "")).splitlines()
        if line.strip()
    ]
    files = ["requirements-dev.txt"] + [
        path.relative_to(ROOT).as_posix() for path in (ROOT / "requirements").glob("*.txt")
    ]

    uncovered = [name for name in files if not any(fnmatch(name, pattern) for pattern in patterns)]
    assert uncovered == []


@pytest.mark.parametrize("platform", PLATFORM_PACKAGES)
def test_each_platform_has_its_own_image_stage(platform):
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert f"FROM base AS {platform}" in dockerfile
    assert f"pip install -r requirements/{platform}.txt" in dockerfile
    assert f"ENV PLATFORM={platform}" in dockerfile


def test_the_base_image_stage_installs_only_the_common_requirements():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    base_stage = dockerfile.split("FROM base AS", 1)[0]

    assert "pip install -r requirements/base.txt" in base_stage
    for platform in PLATFORM_PACKAGES:
        assert f"requirements/{platform}.txt" not in base_stage
