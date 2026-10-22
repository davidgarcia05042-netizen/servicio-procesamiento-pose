"""
Re-entrega de tareas de Celery (app/core/video_job.py).

POR QUÉ EXISTE ESTE ARCHIVO: el worker corre con `task_acks_late`, así que si
muere sin confirmar la tarea, Celery se la vuelve a entregar a otro worker
cuando vence el `visibility_timeout` de Redis. Eso es deseable mientras el job
siga en curso -- es justo la recuperación que esa opción busca -- pero deja de
serlo en cuanto el job ya está cerrado.

El caso concreto que motivó la guarda: Render manda SIGTERM al desplegar y mata
con SIGKILL pasado el plazo. Un video cortado a la mitad deja el job en
PROCESSING; el barrido lo cierra a los 15 minutos y le avisa a la plataforma
que falló. Cuando después vence el `visibility_timeout` (una hora, ver
app/platforms/render/worker.py) y la tarea se re-entrega, reprocesarla le mandaría a la plataforma un callback
de ÉXITO por un job del que ya se le dijo que había fallado.
"""

import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.constants.job_constants import JobStatus

ANALYSIS = {
    "points": [{"frame": 0, "angle": 91.5}],
    "max_angle": 91.5,
    "min_angle": 91.5,
}


def _job(status: str) -> SimpleNamespace:
    return SimpleNamespace(
        id="job-1",
        status=status,
        video_url="videos/abc_video.mp4",
        results_endpoint="https://plataforma.example.com/r",
        results={"requested_points": [24, 26, 28]},
    )


async def _run(status: str):
    """Corre la tarea sobre un job en el estado dado y devuelve qué se tocó."""
    from app.core import video_job

    with (
        patch.object(video_job, "get_job", new=AsyncMock(return_value=_job(status))),
        patch.object(video_job, "update_open_job", new=AsyncMock(return_value=True)) as update,
        patch.object(video_job, "download_video", new=AsyncMock()) as download,
        patch.object(video_job, "delete_video", new=AsyncMock()) as delete,
        patch.object(video_job, "analyze_video", return_value=ANALYSIS) as analyze,
        patch.object(video_job, "enqueue_callback") as callback,
    ):
        result = await video_job.process_video_job("job-1")

    return SimpleNamespace(
        result=result,
        update=update,
        download=download,
        delete=delete,
        analyze=analyze,
        callback=callback,
    )


# --- Un job ya cerrado se descarta ---


@pytest.mark.parametrize("status", [JobStatus.DONE.value, JobStatus.FAILED.value])
async def test_a_closed_job_is_not_reprocessed(status):
    """
    Ni se vuelve a descargar el video, ni se vuelve a correr MediaPipe, ni se
    toca la fila. Para un job en FAILED, reprocesarlo contradiría el fallo que
    la plataforma ya recibió; para uno en DONE, le entregaría el resultado dos
    veces.
    """
    run = await _run(status)

    assert run.result == status
    run.download.assert_not_awaited()
    run.analyze.assert_not_called()
    run.update.assert_not_awaited()
    run.callback.assert_not_called()


async def test_discarding_a_closed_job_leaves_an_event(caplog):
    """
    Sin rastro, una tarea re-entregada que se descarta es indistinguible de una
    que nunca llegó. El evento es lo que permite saber después que el
    `visibility_timeout` está dejando pasar re-entregas inútiles.
    """
    with caplog.at_level(logging.INFO, logger="job_events"):
        await _run(JobStatus.FAILED.value)

    events = [
        json.loads(record.getMessage())
        for record in caplog.records
        if record.name == "job_events"
    ]
    closed = [event for event in events if event["event"] == "job_already_closed"]

    assert len(closed) == 1
    assert closed[0]["job_id"] == "job-1"
    assert closed[0]["status"] == JobStatus.FAILED.value


# --- El discriminante: un job NO cerrado sí se procesa ---


@pytest.mark.parametrize("status", [JobStatus.QUEUED.value, JobStatus.PROCESSING.value])
async def test_a_job_still_in_flight_is_processed_normally(status):
    """
    Esta es la mitad que importa de la guarda. Si rechazara también PROCESSING,
    rompería justamente la recuperación que `task_acks_late` existe para dar:
    un worker que murió a mitad deja el job en PROCESSING, y la re-entrega es
    la única forma de que ese video llegue a procesarse.
    """
    run = await _run(status)

    assert run.result == JobStatus.DONE.value
    run.download.assert_awaited_once()
    run.analyze.assert_called_once()
    run.callback.assert_called_once()


# --- La definición de "cerrado" vive en un solo lugar ---


def test_the_guard_uses_the_shared_definition_of_terminal_state():
    """
    Si alguien agrega un estado final nuevo a JobStatus, la guarda tiene que
    cubrirlo sin que haya que acordarse de tocar video_job.
    """
    assert JobStatus.terminal() == {JobStatus.DONE, JobStatus.FAILED}
    assert JobStatus.QUEUED not in JobStatus.terminal()
    assert JobStatus.PROCESSING not in JobStatus.terminal()


# --- Si ni siquiera se puede leer el job ---


async def test_a_job_that_cannot_be_read_is_left_for_the_sweep():
    """
    Si la base no responde al leer el job, no se sabe a qué endpoint avisar.
    Antes, el manejador de error lo marcaba FAILED -- cerrado, SIN aviso -- y
    además reventaba con UnboundLocalError al tocar `job.video_url`. El barrido
    de jobs colgados solo mira los abiertos, así que la plataforma no se
    enteraba nunca. Dejándolo en QUEUED, el barrido lo cierra y le avisa.
    """
    from app.core import video_job

    with (
        patch.object(video_job, "get_job", new=AsyncMock(side_effect=ConnectionError("db caída"))),
        patch.object(video_job, "update_open_job", new=AsyncMock(return_value=True)) as update,
        patch.object(video_job, "delete_video", new=AsyncMock()) as delete,
        patch.object(video_job, "enqueue_callback") as callback,
    ):
        result = await video_job.process_video_job("job-1")

    assert result == JobStatus.QUEUED.value
    update.assert_not_awaited()
    delete.assert_not_awaited()
    callback.assert_not_called()


# --- Otro cerró el job mientras este worker lo tenía ---


async def _run_with_closings(closings: list[bool], analyze=None):
    """
    Como `_run`, sobre un job en QUEUED, pero decidiendo qué responde cada
    `update_open_job`: el primero es tomar el job; el segundo, cerrarlo.
    """
    from app.core import video_job

    with (
        patch.object(video_job, "get_job", new=AsyncMock(return_value=_job(JobStatus.QUEUED.value))),
        patch.object(video_job, "update_open_job", new=AsyncMock(side_effect=closings)) as update,
        patch.object(video_job, "download_video", new=AsyncMock()) as download,
        patch.object(video_job, "delete_video", new=AsyncMock()) as delete,
        patch.object(video_job, "analyze_video", **(analyze or {"return_value": ANALYSIS})) as analyzed,
        patch.object(video_job, "enqueue_callback") as callback,
    ):
        result = await video_job.process_video_job("job-1")

    return SimpleNamespace(
        result=result, update=update, download=download, delete=delete, analyze=analyzed, callback=callback
    )


async def test_a_job_closed_between_reading_and_taking_it_is_not_processed():
    """El barrido pudo cerrarlo en el instante entre la lectura y la toma."""
    run = await _run_with_closings([False])

    assert run.result == "closed_elsewhere"
    run.download.assert_not_awaited()
    run.analyze.assert_not_called()
    run.callback.assert_not_called()


async def test_a_result_that_arrives_after_the_sweep_closed_the_job_is_discarded(caplog):
    """
    El caso que motivó condicionar el cierre: el barrido cerró el job como
    FAILED (y avisó del fallo) mientras el worker seguía. Antes el worker lo
    sobrescribía como DONE y encolaba un aviso de éxito: la plataforma recibía
    dos mensajes contradictorios por el mismo job.
    """
    with caplog.at_level(logging.INFO, logger="job_events"):
        run = await _run_with_closings([True, False])

    assert run.result == "closed_elsewhere"
    run.callback.assert_not_called()
    # El video ya no sirve: se borra igual.
    run.delete.assert_awaited_once_with("videos/abc_video.mp4")
    events = [json.loads(r.getMessage()) for r in caplog.records if r.name == "job_events"]
    assert [e["outcome"] for e in events if e["event"] == "job_result_discarded"] == ["success"]
    assert not [e for e in events if e["event"] == "job_processed"]


async def test_a_failure_after_someone_else_closed_the_job_sends_no_second_notice():
    run = await _run_with_closings([True, False], analyze={"side_effect": ValueError("sin frames")})

    assert run.result == "closed_elsewhere"
    run.callback.assert_not_called()
    run.delete.assert_awaited_once_with("videos/abc_video.mp4")
