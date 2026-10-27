"""
Valores del blueprint de Render (render.yaml) que dependen de otros archivos o
de mediciones, y que nada más comprueba antes del despliegue.
"""

import asyncio
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent


def _service(name: str) -> dict:
    blueprint = yaml.safe_load((ROOT / "deploy" / "render" / "render.yaml").read_text(encoding="utf-8"))
    return next(service for service in blueprint["services"] if service["name"] == name)


def _env(service: dict) -> dict[str, str]:
    return {var["key"]: var.get("value") for var in service["envVars"] if "key" in var}


def test_the_api_port_matches_the_one_uvicorn_listens_on():
    """
    Render espera el 10000 si PORT no está definido, y solo "usualmente"
    detecta otro. Si el CMD del Dockerfile cambia de puerto, esto lo avisa.
    """
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    cmd_port = re.search(r'^CMD \[.*"--port", "(\d+)"', dockerfile, re.MULTILINE).group(1)

    assert _env(_service("pose-api"))["PORT"] == cmd_port


def _plan_cpus(plan: str) -> float:
    """"0.5c-512mb" -> 0.5, "4c-8g" -> 4."""
    return float(re.fullmatch(r"([\d.]+)c-[\d.]+(?:mb|g)", plan).group(1))


def _plan_memory_mb(plan: str) -> float:
    """"0.5c-512mb" -> 512, "1c-2g" -> 2048."""
    amount, unit = re.fullmatch(r"[\d.]+c-([\d.]+)(mb|g)", plan).groups()
    return float(amount) * (1024 if unit == "g" else 1)


def test_the_worker_runs_one_process_per_cpu_and_they_fit_in_memory():
    """
    El procesamiento de video satura un núcleo: menos procesos que núcleos
    deja CPU pagada sin usar, y más solo se estorban. MediaPipe Heavy llegó a
    649 MB de pico por proceso (pose-benchmark, Render 2c-8g, 2026-10-01); se
    pide el doble por proceso para cubrir la deriva de memoria entre jobs.
    """
    worker = _service("pose-worker")
    processes = int(re.search(r"--concurrency=(\d+)", worker["dockerCommand"]).group(1))
    mediapipe_heavy_peak_mb = 649

    assert processes == _plan_cpus(worker["plan"])
    assert _plan_memory_mb(worker["plan"]) >= 2 * mediapipe_heavy_peak_mb * processes


def test_the_api_has_room_for_two_videos_at_once():
    """
    Con dos videos de 47 MB llegando a la vez, la API llegó a 662 MiB (demo
    local, 2026-10-08). En 0.5c-512mb se reiniciaría por falta de memoria y las
    dos subidas fallarían. Se pide al menos un 50 % de margen sobre lo medido.
    """
    measured_two_videos_mb = 662

    assert _plan_memory_mb(_service("pose-api")["plan"]) >= 1.5 * measured_two_videos_mb


def test_the_worker_limits_glibc_memory_arenas():
    """Sin esto la RAM del worker sube job a job (ver el comentario en render.yaml)."""
    assert _env(_service("pose-worker"))["MALLOC_ARENA_MAX"] == "2"


def _blueprint() -> dict:
    return yaml.safe_load((ROOT / "deploy" / "render" / "render.yaml").read_text(encoding="utf-8"))


def _group() -> dict[str, str]:
    group = next(g for g in _blueprint()["envVarGroups"] if g["name"] == "pose-config")
    return {var["key"]: var.get("value") for var in group["envVars"]}


def _database_var(service: dict):
    return next((var for var in service["envVars"] if var.get("key") == "DATABASE_URL"), None)


def _database() -> dict:
    databases = _blueprint()["databases"]
    assert [db["name"] for db in databases] == ["pose-db"]
    return databases[0]


def test_the_database_is_render_postgres_in_the_services_region():
    """
    Todo en Render desde el 2026-10-08. La red privada no cruza regiones: en
    otra región, la URL interna de la base no resolvería desde los servicios.
    """
    database = _database()

    assert database["plan"] != "free"  # vence a los 30 días y se borra
    for name in APP_SERVICES:
        assert _service(name)["region"] == database["region"], name


def test_the_database_is_closed_to_the_internet():
    """Solo los servicios, por la red privada: ahí viajan resultados y videos de pacientes."""
    assert _database()["ipAllowList"] == []


def test_the_database_disk_grows_before_it_fills_up():
    """
    Si el worker se cae, los videos se acumulan en la base. Con el disco lleno
    la base deja de aceptar escrituras y se cae todo el servicio.
    """
    assert _database()["storageAutoscalingEnabled"] is True


def test_the_database_url_comes_from_the_blueprint_only_where_it_is_used():
    """
    La URL interna de pose-db, sin pegar nada a mano. Beat no la lleva: no
    abre conexiones.
    """
    expected = {"key": "DATABASE_URL", "fromDatabase": {"name": "pose-db", "property": "connectionString"}}

    assert _database_var(_service("pose-api")) == expected
    assert _database_var(_service("pose-worker")) == expected
    assert _database_var(_service("pose-beat")) is None


def test_the_internal_database_host_skips_tls_and_nothing_else_does():
    """
    La URL interna de Render tiene un host "dpg-...-a" sin dominio. La regla de
    app/core/config.py no le exige TLS por eso; si Render cambiara la forma, la
    API no arrancaría en producción.
    """
    from app.platforms import render

    assert render.is_private_network_host("dpg-cl4bq5ij1k6c73a1b2c0-a")
    assert not render.is_private_network_host("dpg-cl4bq5ij1k6c73a1b2c0-a.oregon-postgres.render.com")


def test_only_the_r2_token_is_pasted_by_hand():
    """
    Los únicos secretos que no salen del blueprint son el par de claves del
    token de R2, el del bucket privado de videos (2026-10-22: los videos
    dejaron Supabase porque su plan gratuito pausa el proyecto; ver
    test_the_videos_wait_in_r2). La URL de la base la entrega Render y la
    clave HMAC la genera.

    Por qué así y no en otro lado:
    - Van con `sync: false`, nunca con `value`: el secreto no entra al repo.
    - Van en pose-api y pose-worker, que suben, leen y borran videos y las
      exigen al arrancar. No en pose-beat, que no toca el almacenamiento.
    - No van en el grupo pose-config: Render ignora en silencio una variable
      `sync: false` de un grupo, y la API y el worker no arrancarían.
    Cualquier otro `sync: false` es un secreto nuevo a mano: tiene que
    justificarse acá.
    """
    blueprint = _blueprint()
    asked = [
        (service["name"], var["key"])
        for service in blueprint["services"]
        for var in service.get("envVars", [])
        if var.get("sync") is False
    ]
    assert asked == [
        ("pose-api", "R2_ACCESS_KEY_ID"),
        ("pose-api", "R2_SECRET_ACCESS_KEY"),
        ("pose-worker", "R2_ACCESS_KEY_ID"),
        ("pose-worker", "R2_SECRET_ACCESS_KEY"),
    ]

    group = next(g for g in blueprint["envVarGroups"] if g["name"] == "pose-config")
    assert not [var for var in group["envVars"] if "sync" in var]
    for secret in ("R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "SUPABASE_SECRET_KEY"):
        assert secret not in _group()

    # Ningún valor escrito de las claves en ningún lado del blueprint.
    for service in blueprint["services"]:
        for var in service.get("envVars", []):
            if var.get("key") in {"R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "SUPABASE_SECRET_KEY"}:
                assert set(var) == {"key", "sync"}


def test_connections_fit_render_postgres():
    """
    Render Postgres admite 100 conexiones en los planes de menos de 8 GB de
    RAM. En el peor momento de un despliegue conviven la API vieja y la
    nueva, los procesos del worker y el alembic del pre-despliegue
    (NullPool: una).
    """
    assert _database()["plan"] in {"basic-256mb", "basic-1gb", "basic-4gb"}
    group = _group()
    per_process = int(group["DB_POOL_SIZE"]) + int(group["DB_MAX_OVERFLOW"])

    command = _service("pose-worker")["dockerCommand"]
    worker_processes = int(re.search(r"--concurrency=(\d+)", command).group(1))
    api_processes_during_deploy = 2
    alembic = 1

    total = per_process * (api_processes_during_deploy + worker_processes) + alembic
    assert total <= 100, total


def test_the_engine_uses_the_configured_pool(monkeypatch):
    from app.core.config import settings
    from app.utils import db_connect

    captured = {}

    def fake_engine(url, **kwargs):
        captured.update(kwargs)
        raise RuntimeError("basta con los argumentos")

    monkeypatch.setattr(db_connect, "create_async_engine", fake_engine)
    monkeypatch.setattr(settings, "DB_POOL_SIZE", 2)
    monkeypatch.setattr(settings, "DB_MAX_OVERFLOW", 1)

    with pytest.raises(RuntimeError):
        asyncio.run(db_connect.Database().connect())

    assert captured["pool_size"] == 2
    assert captured["max_overflow"] == 1


# --- Render construye la imagen desde GitHub ---

APP_SERVICES = ("pose-api", "pose-worker", "pose-beat")
_BUILD_KEYS = ("runtime", "repo", "branch", "dockerfilePath", "dockerContext", "autoDeployTrigger", "buildFilter")


def test_the_three_processes_build_the_same_commit():
    """
    Si un servicio construyera otra rama u otro Dockerfile, la API y el worker
    podrían correr versiones distintas del código.
    """
    builds = [{key: _service(name).get(key) for key in _BUILD_KEYS} for name in APP_SERVICES]

    assert builds[0]["runtime"] == "docker"
    assert builds[0]["branch"] == "main"
    assert all(build == builds[0] for build in builds), builds


def test_render_only_deploys_what_passed_the_checks():
    """Con `commit`, Render desplegaría un commit de main aunque la suite falle."""
    for name in APP_SERVICES:
        assert _service(name)["autoDeployTrigger"] == "checksPass", name


def test_render_builds_the_render_stage_of_the_dockerfile():
    """
    El blueprint no permite elegir la etapa: Render construye la última. Si
    fuera `gcp`, Render desplegaría la imagen de Google y nada arrancaría.
    """
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    stages = re.findall(r"^FROM \S+ AS (\S+)", dockerfile, re.MULTILINE)

    assert stages[-1] == "render", stages


def test_the_build_filter_covers_everything_the_image_copies():
    """
    El filtro evita reconstruir por un cambio en el README, pero si dejara
    afuera algo que el Dockerfile copia, un cambio ahí no se desplegaría nunca.
    """
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    sources = {source.rstrip("/") for source in re.findall(r"^COPY (\S+) ", dockerfile, re.MULTILINE)}
    paths = _service("pose-api")["buildFilter"]["paths"]

    assert "Dockerfile" in paths
    missing = [source for source in sorted(sources) if source not in paths and f"{source}/**" not in paths]
    assert missing == []


def test_render_builds_this_repository():
    """Un fork o un cambio de nombre del repositorio dejaría a Render construyendo el viejo."""
    import subprocess

    try:
        origin = subprocess.run(
            ["git", "remote", "get-url", "origin"], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("sin git o sin remoto origin")

    def normalize(url: str) -> str:
        return url.lower().removesuffix("/").removesuffix(".git")

    assert normalize(_service("pose-api")["repo"]) == normalize(origin)


# --- Almacenamiento del video ---


def test_the_videos_wait_in_r2():
    """
    No en pose-db: guardar un video de celular como bytea agotó la memoria de
    la base (basic-256mb) en la prueba de punta a punta del 2026-10-16. Y no en
    Supabase desde el 2026-10-22: su plan gratuito pausa el proyecto tras una
    semana sin actividad. Van al bucket privado de R2 (r2_storage.py).
    """
    group = _group()

    assert group["STORAGE_BACKEND"] == "r2"
    assert group["R2_BUCKET"] == "pose-videos"
    # El de la plataforma es público: el del servicio nunca debe ser ese.
    assert group["R2_BUCKET"] != "telerehab-videos"
    assert [key for key in group if key.startswith("FIREBASE_")] == []


def test_the_r2_values_pass_the_startup_check(monkeypatch):
    """
    Lo que comprueba r2_storage.ensure_configured() al arrancar la API y el
    worker, con los valores del blueprint y claves de relleno de la longitud
    correcta (las reales solo están en el Dashboard).
    """
    from app.platforms.render import r2_storage
    from app.platforms.render.config import render_settings

    group = _group()
    monkeypatch.setattr(render_settings, "R2_ACCOUNT_ID", group["R2_ACCOUNT_ID"])
    monkeypatch.setattr(render_settings, "R2_BUCKET", group["R2_BUCKET"])
    monkeypatch.setattr(render_settings, "R2_ACCESS_KEY_ID", "a" * 32)
    monkeypatch.setattr(render_settings, "R2_SECRET_ACCESS_KEY", "s" * 64)
    r2_storage.ensure_configured()

    monkeypatch.setattr(render_settings, "R2_SECRET_ACCESS_KEY", "")
    with pytest.raises(RuntimeError, match="R2_SECRET_ACCESS_KEY"):
        r2_storage.ensure_configured()


def test_supabase_stays_available_as_the_alternative():
    """Volver a Supabase es cambiar STORAGE_BACKEND (y tener su clave en el Dashboard)."""
    group = _group()

    assert group["SUPABASE_URL"] == "https://eytjjlcthcghftlwxgab.supabase.co"
    assert group["SUPABASE_STORAGE_BUCKET"] == "pose-videos"


# --- El blueprint sigue siendo desplegable ---


def test_the_blueprint_matches_render_official_schema():
    """
    Contra el JSON Schema oficial de Render, guardado en el repositorio para no
    depender de la red (deploy/render/render.schema.json). Un blueprint mal
    escrito se descubriría recién al crearlo en el Dashboard.

    Para refrescar el esquema:
        curl -sL https://render.com/schema/render.yaml.json -o deploy/render/render.schema.json
    """
    import json

    from jsonschema import Draft202012Validator

    schema = json.loads((ROOT / "deploy" / "render" / "render.schema.json").read_text(encoding="utf-8"))
    errors = [error.message for error in Draft202012Validator(schema).iter_errors(_blueprint())]

    assert errors == []


def test_the_celery_commands_point_to_a_module_that_exists():
    """Si el worker se mueve de lugar, el blueprint no puede quedar apuntando al viejo."""
    from importlib import import_module

    for name in ("pose-worker", "pose-beat"):
        command = _service(name)["dockerCommand"]
        module_path, attribute = re.search(r"celery -A (\S+)", command).group(1).rsplit(".", 1)
        assert hasattr(import_module(module_path), attribute), (name, command)


def test_the_blueprint_runs_the_render_platform():
    """La imagen de Render fija PLATFORM=render; el blueprint no la contradice."""
    assert _group().get("PLATFORM", "render") == "render"
