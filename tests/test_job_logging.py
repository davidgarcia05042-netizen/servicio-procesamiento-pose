"""
Registro estructurado del ciclo de vida (app/core/job_logging.py) y los datos
que quedan persistidos en la tabla `jobs`.

La decisión de diseño que se prueba acá: el registro vive en PostgreSQL, y el
tiempo de procesamiento se mide alrededor del trabajo real del worker, no como
`updated_at - created_at` (que incluiría la espera en cola).
"""

import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.constants.job_constants import JobStatus
from app.core.job_logging import log_job_event

ANALYSIS = {
    "points": [{"frame": 0, "angle": 91.5}, {"frame": 1, "angle": 92.0}],
    "max_angle": 92.0,
    "min_angle": 91.5,
}


def _fake_job() -> SimpleNamespace:
    return SimpleNamespace(
        id="job-1",
        # QUEUED es el estado real cuando el worker toma la tarea. Importa que
        # NO sea terminal: el worker descarta los jobs ya cerrados en vez de
        # reprocesarlos (ver tests/test_task_redelivery.py).
        status=JobStatus.QUEUED.value,
        video_url="videos/abc_video.mp4",
        results_endpoint=None,  # sin callback: no es lo que se prueba acá
        results={"requested_points": [24, 26, 28]},
    )


# --- El formato del evento ---


def test_log_job_event_emits_parseable_json(caplog):
    with caplog.at_level(logging.INFO, logger="job_events"):
        log_job_event("job_processed", "job-1", outcome="success", processing_seconds=1.25)

    payload = json.loads(caplog.records[-1].getMessage())
    assert payload == {
        "event": "job_processed",
        "job_id": "job-1",
        "outcome": "success",
        "processing_seconds": 1.25,
    }


def test_log_job_event_omits_empty_fields_and_allows_missing_job_id(caplog):
    """Una solicitud rechazada en validación no tiene job_id: nunca llegó a crearse la fila."""
    with caplog.at_level(logging.INFO, logger="job_events"):
        log_job_event("video_rejected", reason="invalid_points", pose_model=None)

    payload = json.loads(caplog.records[-1].getMessage())
    assert payload == {"event": "video_rejected", "reason": "invalid_points"}
    assert "job_id" not in payload


# --- Lo que queda persistido tras procesar ---


async def test_processing_time_and_model_are_persisted_on_success():
    from app.core import video_job

    with (
        patch.object(video_job, "get_job", new=AsyncMock(return_value=_fake_job())),
        patch.object(video_job, "update_open_job", new=AsyncMock(return_value=True)) as mock_update,
        patch.object(video_job, "download_video", new=AsyncMock()),
        patch.object(video_job, "delete_video", new=AsyncMock()),
        patch.object(video_job, "analyze_video", return_value=ANALYSIS),
    ):
        status = await video_job.process_video_job("job-1")

    assert status == JobStatus.DONE.value

    done_write = mock_update.await_args_list[-1]
    assert done_write.kwargs["status"] == JobStatus.DONE.value
    assert done_write.kwargs["processing_seconds"] >= 0

    # El tiempo viaja en la MISMA escritura que el cambio de estado: registrar
    # no debe costar una consulta extra.
    assert len(mock_update.await_args_list) == 2  # PROCESSING y luego DONE


async def test_processing_time_is_persisted_even_when_the_job_fails():
    """Distinguir un fallo inmediato de uno tras minutos de cómputo es justo el dato útil."""
    from app.core import video_job

    with (
        patch.object(video_job, "get_job", new=AsyncMock(return_value=_fake_job())),
        patch.object(video_job, "update_open_job", new=AsyncMock(return_value=True)) as mock_update,
        patch.object(video_job, "download_video", new=AsyncMock()),
        patch.object(video_job, "delete_video", new=AsyncMock()),
        patch.object(video_job, "analyze_video", side_effect=ValueError("video sin frames")),
    ):
        status = await video_job.process_video_job("job-1")

    assert status == JobStatus.FAILED.value

    failed_write = mock_update.await_args_list[-1]
    assert failed_write.kwargs["status"] == JobStatus.FAILED.value
    assert failed_write.kwargs["error"] == "video sin frames"
    assert failed_write.kwargs["processing_seconds"] >= 0


async def test_job_processed_event_reports_the_outcome(caplog):
    from app.core import video_job

    with (
        patch.object(video_job, "get_job", new=AsyncMock(return_value=_fake_job())),
        patch.object(video_job, "update_open_job", new=AsyncMock(return_value=True)),
        patch.object(video_job, "download_video", new=AsyncMock()),
        patch.object(video_job, "delete_video", new=AsyncMock()),
        patch.object(video_job, "analyze_video", return_value=ANALYSIS),
        caplog.at_level(logging.INFO, logger="job_events"),
    ):
        await video_job.process_video_job("job-1")

    # caplog recoge todo lo que llega a la raíz, no solo "job_events".
    events = [
        json.loads(record.getMessage())
        for record in caplog.records
        if record.name == "job_events"
    ]
    processed = next(event for event in events if event["event"] == "job_processed")

    assert processed["outcome"] == "success"
    assert processed["job_id"] == "job-1"
    assert processed["frames"] == 2
    assert "pose_model" in processed
    assert processed["processing_seconds"] >= 0
