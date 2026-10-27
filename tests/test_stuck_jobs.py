"""
Barrido de jobs colgados (app/core/stuck_jobs.py).

Lo que se protege acá son dos fallas silenciosas y distintas:

  - Un worker que muere a mitad no deja ninguna excepción que registrar, solo
    una fila en PROCESSING que dejó de avanzar.
  - Una tarea encolada que se pierde (un reinicio del broker vacía la cola)
    deja una fila en QUEUED que nadie va a tomar nunca.

En los dos casos, si el barrido se rompe o deja de estar programado, el fallo
vuelve a ser invisible y la plataforma espera un callback para siempre.
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.dialects import postgresql

from app.constants.job_constants import JobStatus
from app.core.config import settings
from app.dtos.error_success_code import ErrorAndSuccessCodes
from app.utils.db_connect import StuckJob
from tests.fakes import FakeSession


def _stuck_session(*rows: tuple) -> FakeSession:
    """Una sesión que devuelve filas con la forma de `UPDATE ... RETURNING id, results_endpoint, video_url`."""
    return FakeSession([SimpleNamespace(id=i, results_endpoint=e, video_url=None) for i, e in rows])


def _sweep_calls(mock_fail: AsyncMock) -> dict[str, tuple]:
    """Los argumentos de cada barrido, indexados por el estado que barrió."""
    return {call.args[0]: call.args for call in mock_fail.await_args_list}


# --- La consulta: a quién toca y a quién no ---


async def test_the_query_only_touches_rows_of_the_given_status_older_than_the_cutoff():
    """
    La condición y el cambio viajan en una sola sentencia. Si esto se partiera
    en leer-y-luego-escribir, dos barridos simultáneos podrían cerrar el mismo
    job dos veces, o pisar a un worker que terminó justo en el medio.
    """
    from app.utils import db_connect

    cutoff = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)
    session = _stuck_session(("job-1", "https://plataforma.example.com/r"))

    with patch.object(db_connect.database, "session", return_value=session):
        await db_connect.fail_stuck_jobs(
            JobStatus.PROCESSING.value, cutoff, "processing_timeout"
        )

    compiled = session.statements[0].compile(dialect=postgresql.dialect())
    sql = str(compiled)

    assert "UPDATE jobs SET" in sql
    assert "jobs.status =" in sql
    assert "jobs.updated_at <" in sql
    # El endpoint vuelve con el id: hace falta para avisarle a la plataforma.
    assert "RETURNING jobs.id, jobs.results_endpoint" in sql

    assert compiled.params["status_1"] == JobStatus.PROCESSING.value
    assert compiled.params["updated_at_1"] == cutoff
    assert compiled.params["status"] == JobStatus.FAILED.value
    assert compiled.params["error"] == "processing_timeout"


async def test_the_status_swept_is_the_one_received():
    """
    El estado es un parámetro, no una constante: el mismo UPDATE sirve para
    cerrar los colgados en PROCESSING y los que nunca salieron de QUEUED.
    """
    from app.utils import db_connect

    session = FakeSession()

    with patch.object(db_connect.database, "session", return_value=session):
        await db_connect.fail_stuck_jobs(
            JobStatus.QUEUED.value, datetime.now(timezone.utc), "queued_timeout"
        )

    compiled = session.statements[0].compile(dialect=postgresql.dialect())

    assert compiled.params["status_1"] == JobStatus.QUEUED.value
    assert compiled.params["error"] == "queued_timeout"


async def test_the_query_commits_and_returns_the_closed_jobs_with_their_endpoints():
    from app.utils import db_connect

    session = _stuck_session(("job-1", "https://a.example.com"), ("job-2", None))

    with patch.object(db_connect.database, "session", return_value=session):
        closed = await db_connect.fail_stuck_jobs(
            JobStatus.PROCESSING.value, datetime.now(timezone.utc), "processing_timeout"
        )

    assert closed == [
        StuckJob("job-1", "https://a.example.com"),
        StuckJob("job-2", None),
    ]
    assert session.commits == 1


# --- El barrido: los dos estados, cada uno con su ventana ---


async def test_both_states_are_swept_each_with_its_own_window_and_code():
    """
    Discriminante de que el caso QUEUED existe: si el barrido solo mirara
    PROCESSING, un job cuya tarea se perdió antes de empezar se quedaría en
    QUEUED para siempre, sin cerrarse y sin avisarle a la plataforma.
    """
    from app.core import stuck_jobs

    before = datetime.now(timezone.utc)
    with patch.object(stuck_jobs, "fail_stuck_jobs", new=AsyncMock(return_value=[])) as mock_fail:
        await stuck_jobs.sweep_stuck_jobs()
    after = datetime.now(timezone.utc)

    calls = _sweep_calls(mock_fail)
    assert set(calls) == {JobStatus.PROCESSING.value, JobStatus.QUEUED.value}

    _, processing_cutoff, processing_error = calls[JobStatus.PROCESSING.value]
    _, queued_cutoff, queued_error = calls[JobStatus.QUEUED.value]

    processing_window = timedelta(minutes=settings.STUCK_JOB_TIMEOUT_MINUTES)
    queued_window = timedelta(minutes=settings.STUCK_JOB_QUEUED_TIMEOUT_MINUTES)

    # Cada corte cae exactamente su propia ventana atrás del momento del barrido.
    assert before - processing_window <= processing_cutoff <= after - processing_window
    assert before - queued_window <= queued_cutoff <= after - queued_window

    # Códigos distintos: son fallas distintas y hay que poder diferenciarlas.
    assert processing_error == ErrorAndSuccessCodes.PROCESSING_TIMEOUT.value
    assert queued_error == ErrorAndSuccessCodes.QUEUED_TIMEOUT.value


async def test_every_closed_job_leaves_an_event(caplog):
    """Es el único rastro que queda: del lado del worker no hubo excepción."""
    from app.core import stuck_jobs

    async def fail(status, cutoff, error):
        if status == JobStatus.PROCESSING.value:
            return [StuckJob("job-1", "https://a.example.com")]
        return [StuckJob("job-2", "https://b.example.com")]

    with (
        patch.object(stuck_jobs, "fail_stuck_jobs", new=fail),
        patch.object(stuck_jobs, "_notify_platform"),
        caplog.at_level(logging.INFO, logger="job_events"),
    ):
        closed = await stuck_jobs.sweep_stuck_jobs()

    assert closed == ["job-1", "job-2"]

    events = {
        event["event"]: event
        for event in (
            json.loads(record.getMessage())
            for record in caplog.records
            if record.name == "job_events"
        )
    }

    assert events["job_timed_out"]["job_id"] == "job-1"
    assert events["job_timed_out"]["error"] == ErrorAndSuccessCodes.PROCESSING_TIMEOUT.value
    assert events["job_timed_out"]["timeout_minutes"] == settings.STUCK_JOB_TIMEOUT_MINUTES

    assert events["job_never_started"]["job_id"] == "job-2"
    assert events["job_never_started"]["error"] == ErrorAndSuccessCodes.QUEUED_TIMEOUT.value
    assert (
        events["job_never_started"]["timeout_minutes"]
        == settings.STUCK_JOB_QUEUED_TIMEOUT_MINUTES
    )


async def test_a_sweep_that_finds_nothing_stays_quiet(caplog):
    """El barrido corre cada pocos minutos: si hablara siempre, sería ruido."""
    from app.core import stuck_jobs

    with (
        patch.object(stuck_jobs, "fail_stuck_jobs", new=AsyncMock(return_value=[])),
        caplog.at_level(logging.INFO),
    ):
        closed = await stuck_jobs.sweep_stuck_jobs()

    assert closed == []
    assert not [record for record in caplog.records if "Barrido:" in record.getMessage()]


# --- El aviso a la plataforma ---


async def test_the_platform_is_told_that_the_job_failed():
    """
    Cerrar la fila saca del engaño a quien mire la base, pero la plataforma
    seguiría esperando para siempre. El aviso es lo que cierra el círculo.
    """
    from app.core import stuck_jobs

    async def fail(status, cutoff, error):
        if status == JobStatus.PROCESSING.value:
            return [StuckJob("job-1", "https://plataforma.example.com/r")]
        return []

    task = MagicMock()
    with (
        patch.object(stuck_jobs, "fail_stuck_jobs", new=fail),
        patch("app.platforms.render.worker.send_callback_task", task),
    ):
        await stuck_jobs.sweep_stuck_jobs()

    # Solo el id: el payload se arma desde la fila en cada intento, y
    # `fail_stuck_jobs` ya la dejó con el código de error.
    task.delay.assert_called_once_with("job-1")


async def test_closing_a_stuck_job_leaves_its_notice_pending_in_the_same_write():
    """
    Si la tarea del aviso se perdiera, el rescate de avisos perdidos solo la
    puede encontrar por `callback_status = pending`. Tiene que quedar en la
    misma escritura que cierra el job, y solo cuando hay a quién avisar.
    """
    from app.utils import db_connect

    session = FakeSession()

    with patch.object(db_connect.database, "session", return_value=session):
        await db_connect.fail_stuck_jobs(
            JobStatus.PROCESSING.value, datetime.now(timezone.utc), "processing_timeout"
        )

    sql = str(
        session.statements[0].compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert "callback_status=CASE WHEN (jobs.results_endpoint IS NOT NULL) THEN 'pending'" in sql


def test_the_failure_notice_carries_the_timeout_code_and_no_angles():
    """Lo que la plataforma recibe por un job colgado, armado desde la fila cerrada."""
    from app.core.callback_queue import payload_from_job

    closed_row = SimpleNamespace(
        error=ErrorAndSuccessCodes.PROCESSING_TIMEOUT.value,
        results={"requested_points": [24, 26, 28]},
        max_angle=None,
        min_angle=None,
    )
    payload = payload_from_job(closed_row).model_dump()

    assert payload["error"] == ErrorAndSuccessCodes.PROCESSING_TIMEOUT.value
    assert payload["results"]["points"] == []
    assert payload["max_angle"] is None


async def test_a_broker_failure_does_not_abort_the_rest_of_the_sweep(caplog):
    """
    Si encolar el aviso revienta, los jobs que faltan tienen que quedar
    cerrados igual: la fila cerrada es lo que impide que el estado siga
    mintiendo, y el broker caído es justamente uno de los motivos por los que
    un job se cuelga.
    """
    from app.core import stuck_jobs

    async def fail(status, cutoff, error):
        if status == JobStatus.PROCESSING.value:
            return [StuckJob("job-1", "https://a.example.com"), StuckJob("job-2", "https://b.example.com")]
        return []

    task = MagicMock()
    task.delay.side_effect = OSError("broker caído")

    with (
        patch.object(stuck_jobs, "fail_stuck_jobs", new=fail),
        patch("app.platforms.render.worker.send_callback_task", task),
        caplog.at_level(logging.ERROR),
    ):
        closed = await stuck_jobs.sweep_stuck_jobs()

    assert closed == ["job-1", "job-2"]
    assert task.delay.call_count == 2  # lo intentó con los dos
    assert "No se pudo encolar el aviso" in caplog.text


async def test_a_job_without_endpoint_is_closed_without_crashing(caplog):
    """La columna admite NULL aunque la API exija el endpoint al recibir."""
    from app.core import stuck_jobs

    async def fail(status, cutoff, error):
        if status == JobStatus.QUEUED.value:
            return [StuckJob("job-sin-endpoint", None)]
        return []

    task = MagicMock()
    with (
        patch.object(stuck_jobs, "fail_stuck_jobs", new=fail),
        patch("app.platforms.render.worker.send_callback_task", task),
        caplog.at_level(logging.WARNING),
    ):
        closed = await stuck_jobs.sweep_stuck_jobs()

    assert closed == ["job-sin-endpoint"]
    task.delay.assert_not_called()
    assert "no hay a quién avisar" in caplog.text


# --- El video del job cerrado ---


async def test_the_sweep_discards_the_video_of_every_job_it_closes():
    """
    Como cualquier otro cierre. Antes el video de un job cerrado por el
    barrido quedaba guardado hasta la retención, 30 días después, sin ninguna
    finalidad (Ley 1581).
    """
    from app.core import stuck_jobs

    async def fail(status, cutoff, error):
        if status == JobStatus.PROCESSING.value:
            return [StuckJob("job-1", "https://a.example.com", "videos/uno.mp4")]
        return [StuckJob("job-2", "https://b.example.com", "videos/dos.mp4"), StuckJob("job-3", None, None)]

    with (
        patch.object(stuck_jobs, "fail_stuck_jobs", new=fail),
        patch.object(stuck_jobs, "enqueue_callback"),
        patch.object(stuck_jobs, "delete_video", new=AsyncMock()) as delete,
    ):
        await stuck_jobs.sweep_stuck_jobs()

    assert [call.args[0] for call in delete.await_args_list] == ["videos/uno.mp4", "videos/dos.mp4"]


async def test_a_video_that_cannot_be_deleted_does_not_abort_the_sweep(caplog):
    """La fila conserva el video_url: el barrido de retención lo vuelve a intentar."""
    from app.core import stuck_jobs

    async def fail(status, cutoff, error):
        if status == JobStatus.PROCESSING.value:
            return [
                StuckJob("job-1", "https://a.example.com", "videos/uno.mp4"),
                StuckJob("job-2", "https://b.example.com", "videos/dos.mp4"),
            ]
        return []

    with (
        patch.object(stuck_jobs, "fail_stuck_jobs", new=fail),
        patch.object(stuck_jobs, "enqueue_callback") as callback,
        patch.object(stuck_jobs, "delete_video", new=AsyncMock(side_effect=OSError("base caída"))) as delete,
        caplog.at_level(logging.ERROR),
    ):
        closed = await stuck_jobs.sweep_stuck_jobs()

    assert closed == ["job-1", "job-2"]
    assert callback.call_count == 2
    assert delete.await_count == 2
    assert "No se pudo borrar el video del job job-1" in caplog.text


async def test_the_closing_query_returns_the_video_path():
    """Quien barre borra el video: la ruta sale de la misma sentencia que cierra."""
    from app.utils import db_connect

    session = FakeSession([SimpleNamespace(id="job-1", results_endpoint=None, video_url="videos/uno.mp4")])
    with patch.object(db_connect.database, "session", return_value=session):
        closed = await db_connect.fail_stuck_jobs(
            JobStatus.QUEUED.value, datetime.now(timezone.utc), "queued_timeout"
        )

    assert closed == [StuckJob("job-1", None, "videos/uno.mp4")]


# --- El cableado: sin esto, el barrido nunca corre ---


def test_the_sweep_is_registered_and_scheduled():
    from app.platforms.render.worker import celery

    entry = celery.conf.beat_schedule["sweep-stuck-jobs"]

    assert entry["task"] == "sweep_stuck_jobs_task"
    assert entry["schedule"] == settings.STUCK_JOB_SCAN_INTERVAL_MINUTES * 60
    assert "sweep_stuck_jobs_task" in celery.tasks


def test_the_scan_runs_more_often_than_the_timeout():
    """
    Si el barrido corriera más espaciado que el umbral, un job atascado podría
    tardar casi el doble del umbral en ser detectado.
    """
    assert settings.STUCK_JOB_SCAN_INTERVAL_MINUTES < settings.STUCK_JOB_TIMEOUT_MINUTES


def test_the_queued_threshold_is_more_generous_than_the_processing_one():
    """
    Esperar en la cola es NORMAL: con C procesos en el worker, una acumulación
    de N videos hace que el último espere alrededor de N/C veces lo que tarda uno.
    Si el umbral de QUEUED fuera tan corto como el de PROCESSING, el barrido
    cerraría como perdidos justamente los jobs de un pico de carga.
    """
    assert settings.STUCK_JOB_QUEUED_TIMEOUT_MINUTES > settings.STUCK_JOB_TIMEOUT_MINUTES
