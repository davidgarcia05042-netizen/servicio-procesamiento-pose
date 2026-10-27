"""
Retención de datos personales (app/core/data_retention.py y el borrado del
video en app/core/video_job.py).

Lo que se protege acá es una obligación legal, no una preferencia de diseño: el
video de una persona haciendo un ejercicio y los ángulos que salen de él son
datos sensibles bajo el artículo 5 de la Ley 1581 de 2012, y el principio de
temporalidad prohíbe conservarlos más allá de la finalidad que justificó
recogerlos. Si esto se rompe, el servicio acumula datos de salud
indefinidamente sin que nada lo haga evidente.
"""

import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.dialects import postgresql

from app.constants.job_constants import CallbackStatus, JobStatus
from app.core.config import settings
from app.utils.db_connect import ExpiredJob
from tests.fakes import FakeSession

ANALYSIS = {"points": [{"frame": 0, "angle": 91.5}], "max_angle": 91.5, "min_angle": 91.5}


def _job(status: str = JobStatus.QUEUED.value) -> SimpleNamespace:
    return SimpleNamespace(
        id="job-1",
        status=status,
        video_url="videos/abc_video.mp4",
        results_endpoint="https://plataforma.example.com/r",
        results={"requested_points": [24, 26, 28]},
    )


async def _process(status=JobStatus.QUEUED.value, analyze=None, delete=None):
    """Corre la tarea del worker y devuelve el doble de `delete_video`."""
    from app.core import video_job

    delete_mock = delete or AsyncMock()
    with (
        patch.object(video_job, "get_job", new=AsyncMock(return_value=_job(status))),
        patch.object(video_job, "update_open_job", new=AsyncMock(return_value=True)),
        patch.object(video_job, "download_video", new=AsyncMock()),
        patch.object(video_job, "delete_video", new=delete_mock),
        patch.object(
            video_job, "analyze_video", **(analyze or {"return_value": ANALYSIS})
        ),
        patch.object(video_job, "enqueue_callback"),
    ):
        result = await video_job.process_video_job("job-1")

    return result, delete_mock


# --- 1. El video se borra en cuanto el job queda cerrado ---


async def test_a_processed_job_discards_its_video():
    """
    Los ángulos ya están calculados y guardados: el video no tiene ningún uso
    posterior acá, y este servicio ni siquiera es su sistema de registro.
    """
    result, delete = await _process()

    assert result == JobStatus.DONE.value
    delete.assert_awaited_once_with("videos/abc_video.mp4")


async def test_a_failed_job_also_discards_its_video():
    """
    Con la guarda de estado terminal la tarea no se va a reprocesar, así que el
    video tampoco sirve. Se pierde poder inspeccionarlo para diagnosticar, y es
    un costo aceptado: guardarlo "por si acaso" es justo lo que el principio de
    temporalidad no permite.
    """
    result, delete = await _process(analyze={"side_effect": ValueError("video sin frames")})

    assert result == JobStatus.FAILED.value
    delete.assert_awaited_once_with("videos/abc_video.mp4")


async def test_a_failure_deleting_the_video_does_not_change_the_outcome(caplog):
    """
    Los ángulos ya están guardados y el aviso a la plataforma ya está encolado.
    Marcar el job como fallido porque Firebase no respondió sería mentir sobre
    un procesamiento que salió bien.
    """
    delete = AsyncMock(side_effect=OSError("Firebase no responde"))

    with caplog.at_level(logging.ERROR):
        result, _ = await _process(delete=delete)

    assert result == JobStatus.DONE.value
    assert "No se pudo borrar el video" in caplog.text


async def test_a_discarded_redelivery_does_not_touch_the_video():
    """
    Si el job ya estaba cerrado, su video se borró cuando se cerró. Volver a
    intentarlo sería trabajo inútil sobre un blob que ya no existe.
    """
    _, delete = await _process(status=JobStatus.DONE.value)

    delete.assert_not_awaited()


# --- 2. La consulta del barrido ---


async def test_the_sweep_only_looks_at_closed_jobs_that_still_hold_personal_data():
    """
    Las dos condiciones importan. Solo jobs cerrados: uno en curso necesita sus
    datos por viejo que sea. Y solo los que todavía tienen algo que minimizar:
    eso es lo que hace el barrido idempotente sin una columna extra, porque una
    fila ya minimizada deja de aparecer sola.
    """
    from app.utils import db_connect

    cutoff = datetime(2026, 8, 1, tzinfo=timezone.utc)
    session = FakeSession()

    with patch.object(db_connect.database, "session", return_value=session):
        await db_connect.find_expired_jobs(cutoff, limit=500)

    sql = str(session.statements[0].compile(dialect=postgresql.dialect()))

    assert "jobs.status IN" in sql
    assert "jobs.created_at <" in sql
    # El OR de "todavía tiene datos personales".
    assert "jobs.video_url IS NOT NULL" in sql
    assert "jobs.results IS NOT NULL" in sql
    assert "jobs.results_endpoint IS NOT NULL" in sql
    assert "LIMIT" in sql


async def test_minimizing_clears_the_identifying_fields_and_keeps_the_rest():
    """
    Minimizar es quitar lo que identifica, no destruir el registro operativo:
    el id, el estado, el ejercicio, el modelo y los tiempos no identifican a
    nadie y son lo que permite seguir midiendo el servicio.
    """
    from app.utils import db_connect

    session = FakeSession([("job-1",)])

    with patch.object(db_connect.database, "session", return_value=session):
        await db_connect.anonymize_jobs(["job-1"])

    compiled = session.statements[0].compile(dialect=postgresql.dialect())
    sql = str(compiled)

    for borrado in ("results", "results_endpoint", "video_url", "error", "callback_error"):
        assert f"{borrado}=" in sql, f"{borrado} debería limpiarse"
    for conservado in ("exercise", "pose_model", "processing_seconds", "created_at"):
        assert f"{conservado}=" not in sql, f"{conservado} NO debería tocarse"

    assert session.commits == 1


async def test_the_angles_are_cleared_with_a_real_sql_null():
    """
    En una columna JSONB, SQLAlchemy traduce `None` al valor JSON `null`, que
    NO es el NULL de SQL. La fila quedaba con el contenido borrado pero
    `results IS NOT NULL` seguía siendo cierto, así que el barrido la volvía a
    seleccionar en cada corrida: reintentaba el borrado en Firebase y repetía
    la advertencia para siempre. Detectado contra PostgreSQL real.
    """
    from app.utils import db_connect

    session = FakeSession([("job-1",)])

    with patch.object(db_connect.database, "session", return_value=session):
        await db_connect.anonymize_jobs(["job-1"])

    sql = str(
        session.statements[0].compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )

    assert "results=NULL" in sql, "results quedaría como JSON null, no como NULL de SQL"


async def test_minimizing_nothing_does_not_hit_the_database():
    from app.utils import db_connect

    session = FakeSession()

    with patch.object(db_connect.database, "session", return_value=session):
        assert await db_connect.anonymize_jobs([]) == 0

    assert session.statements == []


# --- 3. El barrido completo ---


def _sweep(expired, delete=None):
    from app.core import data_retention

    return (
        patch.object(
            data_retention, "find_expired_jobs", new=AsyncMock(return_value=expired)
        ),
        patch.object(
            data_retention, "anonymize_jobs", new=AsyncMock(return_value=len(expired))
        ),
        patch.object(data_retention, "delete_video", new=delete or AsyncMock()),
    )


async def test_the_cutoff_comes_from_the_configured_retention():
    from app.core import data_retention

    find, anonymize, delete = _sweep([])
    with find as mock_find, anonymize, delete:
        await data_retention.purge_expired_data()

    cutoff, limit = mock_find.await_args.args
    expected = datetime.now(timezone.utc) - timedelta(days=settings.DATA_RETENTION_DAYS)

    assert abs((cutoff - expected).total_seconds()) < 5
    assert limit == settings.RETENTION_BATCH_SIZE


async def test_expired_jobs_lose_their_video_and_then_their_data():
    from app.core import data_retention

    expired = [
        ExpiredJob("job-1", "videos/uno.mp4", CallbackStatus.DELIVERED.value),
        ExpiredJob("job-2", "videos/dos.mp4", CallbackStatus.DELIVERED.value),
    ]
    find, anonymize, delete = _sweep(expired)
    with find, anonymize as mock_anon, delete as mock_delete:
        minimized = await data_retention.purge_expired_data()

    assert minimized == 2
    assert [c.args[0] for c in mock_delete.await_args_list] == [
        "videos/uno.mp4",
        "videos/dos.mp4",
    ]
    mock_anon.assert_awaited_once_with(["job-1", "job-2"])


async def test_a_job_whose_video_is_already_gone_is_minimized_anyway():
    """
    El worker borra el video al cerrar el job, así que lo normal es que a esta
    altura ya no esté y la fila conserve el `video_url` en nulo.
    """
    from app.core import data_retention

    find, anonymize, delete = _sweep([ExpiredJob("job-1", None, None)])
    with find, anonymize as mock_anon, delete as mock_delete:
        await data_retention.purge_expired_data()

    mock_delete.assert_not_awaited()
    mock_anon.assert_awaited_once_with(["job-1"])


async def test_a_job_whose_video_could_not_be_deleted_is_left_intact(caplog):
    """
    El discriminante más importante de este archivo.

    Si la fila se minimizara igual, se le borraría el `video_url` y el blob
    quedaría en Firebase SIN NINGÚN REGISTRO de su ruta: imposible de borrar
    después, y el video de un paciente conservado para siempre. Dejando la fila
    intacta, la próxima corrida lo vuelve a intentar.

    Y un blob problemático no puede congelar la minimización de los demás.
    """
    from app.core import data_retention

    delete = AsyncMock(side_effect=[OSError("Firebase caído"), None])
    expired = [
        ExpiredJob("job-falla", "videos/uno.mp4", None),
        ExpiredJob("job-ok", "videos/dos.mp4", None),
    ]

    find, anonymize, delete_patch = _sweep(expired, delete=delete)
    with find, anonymize as mock_anon, delete_patch, caplog.at_level(logging.WARNING):
        await data_retention.purge_expired_data()

    assert delete.await_count == 2  # lo intentó con los dos
    # Solo se minimiza el que sí se pudo borrar.
    mock_anon.assert_awaited_once_with(["job-ok"])
    assert "queda para la próxima corrida" in caplog.text


async def test_nothing_is_minimized_when_no_video_could_be_deleted(caplog):
    from app.core import data_retention

    delete = AsyncMock(side_effect=OSError("Firebase caído"))
    expired = [ExpiredJob("job-1", "videos/uno.mp4", None)]

    find, anonymize, delete_patch = _sweep(expired, delete=delete)
    with find, anonymize as mock_anon, delete_patch, caplog.at_level(logging.WARNING):
        assert await data_retention.purge_expired_data() == 0

    mock_anon.assert_not_awaited()
    assert "ninguno de los 1 job(s)" in caplog.text


async def test_a_job_that_could_not_be_cleaned_is_not_announced_as_destroyed(caplog):
    """
    La advertencia de "resultado que nadie recibió" dice que un dato se va a
    destruir. Si se emitiera para una fila que al final NO se minimiza, estaría
    anunciando una pérdida que no ocurrió -- y la repetiría en cada corrida.
    """
    from app.core import data_retention

    expired = [ExpiredJob("job-perdido", "videos/uno.mp4", CallbackStatus.EXHAUSTED.value)]
    find, anonymize, delete_patch = _sweep(expired, delete=AsyncMock(side_effect=OSError("x")))
    with find, anonymize, delete_patch, caplog.at_level(logging.WARNING):
        await data_retention.purge_expired_data()

    assert "nunca recibió" not in caplog.text


async def test_results_the_platform_never_received_are_named_before_being_destroyed(caplog):
    """
    Sin este aviso, un callback agotado se borraría en silencio al vencer el
    plazo y nadie sabría nunca que ese procesamiento se perdió.
    """
    from app.core import data_retention

    expired = [
        ExpiredJob("job-ok", None, CallbackStatus.DELIVERED.value),
        ExpiredJob("job-perdido", None, CallbackStatus.EXHAUSTED.value),
        ExpiredJob("job-rechazado", None, CallbackStatus.REJECTED.value),
    ]
    find, anonymize, delete = _sweep(expired)
    with find, anonymize, delete, caplog.at_level(logging.WARNING):
        await data_retention.purge_expired_data()

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]

    assert len(warnings) == 1
    assert "job-perdido" in warnings[0]
    assert "job-rechazado" in warnings[0]
    assert "job-ok" not in warnings[0]


async def test_a_sweep_with_nothing_expired_stays_quiet(caplog):
    from app.core import data_retention

    find, anonymize, delete = _sweep([])
    with find, anonymize as mock_anon, delete, caplog.at_level(logging.INFO):
        assert await data_retention.purge_expired_data() == 0

    mock_anon.assert_not_awaited()
    assert "Retención:" not in caplog.text


# --- 4. El cableado ---


def test_the_retention_sweep_is_registered_and_scheduled():
    from app.platforms.render.worker import celery

    entry = celery.conf.beat_schedule["purge-expired-data"]

    assert entry["task"] == "purge_expired_data_task"
    assert entry["schedule"] == settings.RETENTION_SCAN_INTERVAL_HOURS * 3600
    assert "purge_expired_data_task" in celery.tasks


@pytest.mark.parametrize(
    "setting", ["DATA_RETENTION_DAYS", "RETENTION_SCAN_INTERVAL_HOURS", "RETENTION_BATCH_SIZE"]
)
def test_the_retention_settings_are_positive(setting):
    """Un cero acá desactivaría la retención en silencio, o la haría infinita."""
    assert getattr(settings, setting) > 0
