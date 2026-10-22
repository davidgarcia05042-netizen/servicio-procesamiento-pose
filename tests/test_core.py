"""
Tests de las piezas puras (sin red, sin base de datos, sin Firebase).

Correr con: pytest tests/test_core.py -v
"""

import re
import time
from pathlib import Path, PurePosixPath

import pytest

from app.constants.job_constants import JobStatus
from app.core.pose_analysis import _angle_deg
from app.dtos.error_success_code import ErrorAndSuccessCodes
from app.utils.hmac_auth import compute_signature, is_valid_signature
from app.utils.request_validations import (
    endpoint_origin,
    validate_content_type,
    validate_points,
    validate_results_endpoint,
    validate_video_bytes,
    validate_video_is_decodable,
)

# Encabezado mínimo de un MP4 válido: tamaño de box + "ftyp" + marca.
MP4_HEADER = b"\x00\x00\x00\x20ftypisom" + b"\x00" * 16


def test_job_status_values_match_persisted_strings():
    assert JobStatus.QUEUED.value == "queued"
    assert JobStatus.PROCESSING.value == "processing"
    assert JobStatus.DONE.value == "done"
    assert JobStatus.FAILED.value == "failed"
    assert JobStatus.terminal() == {JobStatus.DONE, JobStatus.FAILED}


def test_validate_content_type_rejects_non_video():
    assert validate_content_type("video/mp4") == ErrorAndSuccessCodes.SUCCESS
    assert validate_content_type("image/png") == ErrorAndSuccessCodes.NOT_SUPPORTED_FILE_TYPE
    assert validate_content_type(None) == ErrorAndSuccessCodes.NOT_SUPPORTED_FILE_TYPE


def test_validate_video_bytes_accepts_known_container():
    assert validate_video_bytes(MP4_HEADER) == ErrorAndSuccessCodes.SUCCESS
    assert validate_video_bytes(b"RIFF" + b"\x00" * 12) == ErrorAndSuccessCodes.SUCCESS


def test_validate_video_bytes_rejects_empty_and_fake_video():
    assert validate_video_bytes(b"") == ErrorAndSuccessCodes.EMPTY_FILE
    # Un archivo que dice ser video pero cuyo contenido no lo es: esto es
    # justamente lo que la validación en memoria debe atrapar antes de subirlo.
    assert validate_video_bytes(b"esto no es un video") == ErrorAndSuccessCodes.CORRUPT_FILE_HEADER


def test_validate_video_bytes_rejects_oversized_file(monkeypatch):
    from app.core import config

    monkeypatch.setattr(config.settings, "MAX_VIDEO_SIZE_MB", 0)
    assert validate_video_bytes(MP4_HEADER) == ErrorAndSuccessCodes.FILE_TOO_LARGE


def test_validate_video_is_decodable_accepts_a_real_video(real_video_bytes):
    assert validate_video_is_decodable(real_video_bytes) == ErrorAndSuccessCodes.SUCCESS


def test_validate_video_is_decodable_rejects_header_without_stream():
    """
    Exactamente el hueco que los magic bytes no cubren: encabezado de MP4
    válido pero sin los datos que promete ("moov atom not found").
    """
    assert validate_video_bytes(MP4_HEADER) == ErrorAndSuccessCodes.SUCCESS  # pasa el magic byte
    assert validate_video_is_decodable(MP4_HEADER) == ErrorAndSuccessCodes.UNDECODABLE_VIDEO


def test_validate_video_is_decodable_rejects_truncated_video(real_video_bytes):
    """Un video real cortado a la mitad: el contenedor arranca bien y el stream no está completo."""
    truncated = real_video_bytes[: len(real_video_bytes) // 2]
    assert validate_video_is_decodable(truncated) == ErrorAndSuccessCodes.UNDECODABLE_VIDEO


def test_validate_video_is_decodable_leaves_no_temporary_files(real_video_bytes):
    """El temporal pesa hasta MAX_VIDEO_SIZE_MB: no puede quedar colgado tras cada solicitud."""
    import tempfile
    from pathlib import Path

    temp_dir = Path(tempfile.gettempdir())
    before = set(temp_dir.glob("*.mp4"))

    validate_video_is_decodable(real_video_bytes)
    validate_video_is_decodable(b"\x00\x00\x00\x20ftypisom" + b"\x00" * 16)

    assert set(temp_dir.glob("*.mp4")) == before


def test_validate_points_requires_three_valid_landmark_indices():
    assert validate_points([24, 26, 28]) == ErrorAndSuccessCodes.SUCCESS
    assert validate_points([24, 26]) == ErrorAndSuccessCodes.INVALID_POINTS
    assert validate_points([]) == ErrorAndSuccessCodes.INVALID_POINTS
    # 33 landmarks -> índices válidos 0..32
    assert validate_points([24, 26, 99]) == ErrorAndSuccessCodes.INVALID_POINTS


def test_validate_points_rejects_booleans_that_python_counts_as_ints():
    """`[true, 26, 28]` en JSON: `True` es un int en Python y pasaba como el landmark 1."""
    assert validate_points([True, 26, 28]) == ErrorAndSuccessCodes.INVALID_POINTS
    assert validate_points([24, False, 28]) == ErrorAndSuccessCodes.INVALID_POINTS


def test_validate_points_rejects_repeated_points():
    """
    Con dos puntos iguales el ángulo no existe. Antes se aceptaba y el job
    terminaba en `done` con todos los ángulos nulos: un resultado vacío
    presentado como exitoso.
    """
    assert validate_points([26, 26, 28]) == ErrorAndSuccessCodes.INVALID_POINTS
    assert validate_points([24, 26, 24]) == ErrorAndSuccessCodes.INVALID_POINTS


def test_validate_points_rejects_what_is_not_a_list_of_ints():
    assert validate_points([24.0, 26, 28]) == ErrorAndSuccessCodes.INVALID_POINTS
    assert validate_points(["24", 26, 28]) == ErrorAndSuccessCodes.INVALID_POINTS
    assert validate_points({"a": 1, "b": 2, "c": 3}) == ErrorAndSuccessCodes.INVALID_POINTS
    assert validate_points([-1, 26, 28]) == ErrorAndSuccessCodes.INVALID_POINTS
    assert validate_points([0, 16, 32]) == ErrorAndSuccessCodes.SUCCESS


def test_validate_content_type_ignores_parameters_and_case():
    """El mismo tipo con parámetros o en mayúsculas no puede dejar afuera a un cliente legítimo."""
    assert validate_content_type("video/mp4; codecs=avc1") == ErrorAndSuccessCodes.SUCCESS
    assert validate_content_type("Video/MP4") == ErrorAndSuccessCodes.SUCCESS
    assert validate_content_type("image/png; x=video/mp4") == ErrorAndSuccessCodes.NOT_SUPPORTED_FILE_TYPE


def test_endpoint_origin_keeps_only_scheme_and_host():
    """
    La ruta y los parámetros del resultsEndpoint suelen llevar identificadores
    del paciente, y las credenciales no pueden terminar en un log.
    """
    assert endpoint_origin("https://api.x.com/pacientes/123?sesion=9") == "https://api.x.com"
    assert endpoint_origin("https://user:clave@api.x.com:8443/r") == "https://api.x.com:8443"
    assert endpoint_origin("") == "(sin host)"
    assert endpoint_origin(None) == "(sin host)"


def test_rejected_endpoint_log_leaves_out_the_path(caplog):
    import logging

    with caplog.at_level(logging.WARNING):
        validate_results_endpoint("ftp://x.com/pacientes/123")

    assert "rechazado" in caplog.text
    assert "pacientes/123" not in caplog.text


def test_an_unknown_pose_model_stops_the_service_from_starting():
    """
    Antes, un modelo desconocido corría MediaPipe Heavy en silencio y la tabla
    lo registraba con el nombre equivocado.
    """
    from app.core.config import Settings

    with pytest.raises(ValueError, match="POSE_MODEL"):
        Settings(POSE_MODEL="yolo26_nano", _env_file=None)

    assert Settings(POSE_MODEL=" MediaPipe_Lite ", _env_file=None).POSE_MODEL == "mediapipe_lite"


def test_every_configurable_model_maps_to_a_mediapipe_complexity():
    """Lo que la configuración acepta tiene que ser algo que MediaPipe sepa correr."""
    from app.constants.pose_models import MODEL_COMPLEXITY

    assert set(MODEL_COMPLEXITY.values()) == {0, 1, 2}


def test_validate_results_endpoint_requires_http_url():
    # Con punto: "plataforma" a secas se rechaza por ser un host interno, que
    # es lo que cubre tests/test_callback_endpoint.py.
    assert (
        validate_results_endpoint("https://plataforma.example.com/callback")
        == ErrorAndSuccessCodes.SUCCESS
    )
    assert validate_results_endpoint("") == ErrorAndSuccessCodes.INVALID_RESULTS_ENDPOINT
    assert validate_results_endpoint("ftp://x/y") == ErrorAndSuccessCodes.INVALID_RESULTS_ENDPOINT


def test_hmac_accepts_valid_signature(monkeypatch):
    from app.core import config

    monkeypatch.setattr(config.settings, "HMAC_SECRET", "secreto-de-prueba")
    body = b'{"exercise":"sentadilla"}'
    timestamp = str(int(time.time()))
    signature = compute_signature(timestamp, body, secret="secreto-de-prueba")

    assert is_valid_signature(timestamp, signature, body) is True


def test_hmac_rejects_tampered_body_and_stale_timestamp(monkeypatch):
    from app.core import config

    monkeypatch.setattr(config.settings, "HMAC_SECRET", "secreto-de-prueba")
    monkeypatch.setattr(config.settings, "HMAC_MAX_SKEW_SECONDS", 300)

    body = b'{"exercise":"sentadilla"}'
    timestamp = str(int(time.time()))
    signature = compute_signature(timestamp, body, secret="secreto-de-prueba")

    # Cuerpo alterado con la misma firma
    assert is_valid_signature(timestamp, signature, b'{"exercise":"otro"}') is False

    # Firma vieja (fuera de la ventana anti-replay)
    stale = str(int(time.time()) - 400)
    assert is_valid_signature(stale, compute_signature(stale, body, secret="secreto-de-prueba"), body) is False


def test_angle_deg_computes_straight_and_right_angles():
    # Puntos colineales -> 180°
    assert _angle_deg((0, 0), (10, 0), (20, 0)) == pytest.approx(180.0)
    # Ángulo recto
    assert _angle_deg((0, 10), (0, 0), (10, 0)) == pytest.approx(90.0)


def test_run_async_fails_loudly_without_worker_loop():
    """Sin worker inicializado no debe crear un loop al vuelo, debe fallar explícito."""
    from app.platforms.render.worker import run_async

    async def noop():
        return "ok"

    coro = noop()
    with pytest.raises(RuntimeError, match="event loop de worker"):
        run_async(coro)
    coro.close()


def test_worker_reuses_the_same_event_loop_across_tasks(monkeypatch):
    """
    Invariante central del diseño: todas las tareas de un proceso worker corren
    sobre EL MISMO loop. Si alguien reemplaza esto por asyncio.run() por tarea,
    cada llamada traería un loop distinto y las conexiones de asyncpg abiertas
    en init_worker quedarían atadas a un loop ya cerrado.
    """
    import asyncio

    from app.platforms.render import worker
    from app.utils import db_connect

    calls = {"connect": 0, "disconnect": 0}

    async def fake_connect(*args, **kwargs):
        calls["connect"] += 1

    async def fake_disconnect(*args, **kwargs):
        calls["disconnect"] += 1

    monkeypatch.setattr(db_connect.database, "connect", fake_connect)
    monkeypatch.setattr(db_connect.database, "disconnect", fake_disconnect)

    async def current_loop():
        return id(asyncio.get_running_loop())

    try:
        worker.init_worker()
        assert calls["connect"] == 1

        first = worker.run_async(current_loop())
        second = worker.run_async(current_loop())
        assert first == second == id(worker._loop)

        # El apagado suelta las conexiones y libera el loop, no solo lo abandona.
        worker.shutdown_worker()
        assert calls["disconnect"] == 1
        assert worker._loop is None
    finally:
        if worker._loop is not None:
            worker._loop.close()
            worker._loop = None


# --- Configuración del worker que solo falla en el contenedor ---


def test_beat_takes_its_schedule_path_from_the_configuration(monkeypatch):
    """
    Beat guarda en disco cuándo corrió cada tarea periódica. En el contenedor
    el directorio de trabajo es de root y el proceso corre sin privilegios, así
    que con el valor por defecto de Celery, Beat ARRANCA Y MUERE con
    "Permission denied: 'celerybeat-schedule'".

    Eso es grave porque el fallo no se nota: el resto del servicio sigue
    procesando videos con normalidad, y lo único que desaparece en silencio es
    el barrido de jobs colgados y el resumen de estado.

    Se recarga el módulo con un valor distinto porque comparar contra
    `render_settings.BEAT_SCHEDULE_PATH` a secas no probaría nada: su valor por
    defecto es exactamente el de Celery, así que la igualdad se cumpliría
    igual aunque nadie hubiera configurado nada.
    """
    import importlib

    from app.platforms.render import config, worker

    monkeypatch.setattr(config.render_settings, "BEAT_SCHEDULE_PATH", "/estado/escribible/beat")
    try:
        recargado = importlib.reload(worker)
        assert recargado.celery.conf.beat_schedule_filename == "/estado/escribible/beat"
    finally:
        monkeypatch.undo()
        importlib.reload(worker)


def test_the_image_gives_beat_a_directory_it_can_write_to():
    """
    La otra mitad: de nada sirve que la ruta sea configurable si la imagen no
    apunta a un sitio escribible. Se comprueba sobre el Dockerfile porque es
    ahí donde vive el arreglo, y porque el fallo solo aparece dentro del
    contenedor, que es justo donde la suite no corre.
    """
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")

    declared = re.search(r"ENV BEAT_SCHEDULE_PATH=(\S+)", dockerfile)
    assert declared, "la imagen no fija BEAT_SCHEDULE_PATH"

    directory = PurePosixPath(declared.group(1)).parent
    assert str(directory) not in ("/", "/app"), "Beat no puede escribir ahí sin root"

    # Y ese directorio tiene que existir y pertenecerle al usuario del servicio.
    assert f"mkdir -p {directory}" in dockerfile
    assert re.search(rf"chown\s+appuser:appuser\s+{directory}", dockerfile), (
        "el directorio existe pero sigue siendo de root"
    )


def test_celery_does_not_take_over_the_logging_configuration():
    """
    Celery reconfigura el logging al arrancar el worker y fija el nivel con
    `--loglevel`. Con eso, LOG_LEVEL no tenía efecto justo en el worker -- y en
    Render la línea de comandos vive en render.yaml, así que cambiar el nivel
    para diagnosticar un incidente habría exigido un despliegue.

    Conectarse a la señal `setup_logging` es lo que se lo impide.
    `worker_hijack_root_logger=False` por sí solo no alcanza: evita que Celery
    pise los manejadores, pero el nivel lo sigue fijando igual.
    """
    from celery.signals import setup_logging

    import app.platforms.render.worker  # noqa: F401 -- conecta la señal al importarse

    assert setup_logging.receivers, "Celery volvería a configurar el logging"
