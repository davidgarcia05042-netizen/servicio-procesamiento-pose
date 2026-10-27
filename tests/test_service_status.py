"""
Resumen periódico del estado del servicio (app/core/service_status.py).

Lo que se protege acá: desplegado en Render el log es lo único que se ve desde
afuera. `/health` responde si PostgreSQL y Redis contestan, pero con las
dependencias sanas y el worker atascado sigue devolviendo 200. Si este resumen
se rompe o deja de estar agendado, el servicio puede quedarse horas sin
procesar nada sin que ninguna señal lo diga.
"""

import json
import logging
from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

from app.constants.job_constants import CallbackStatus, JobStatus
from app.core.config import settings


@contextmanager
def _counts(recent=None, open_jobs=None, undelivered=0, retrying=0):
    """Parchea las consultas con números fijos y entrega sus dobles."""
    from app.core import service_status

    with (
        patch.object(
            service_status, "count_jobs_by_status", new=AsyncMock(return_value=recent or {})
        ) as by_status,
        patch.object(
            service_status, "count_open_jobs", new=AsyncMock(return_value=open_jobs or {})
        ) as open_now,
        patch.object(
            service_status, "count_undelivered_callbacks", new=AsyncMock(return_value=undelivered)
        ) as undelivered_now,
        patch.object(
            service_status, "count_retrying_callbacks", new=AsyncMock(return_value=retrying)
        ),
    ):
        yield by_status, open_now, undelivered_now


# --- Los números ---


async def test_the_summary_separates_recent_activity_from_the_backlog():
    """
    Son preguntas distintas y por eso se cuentan por separado: un job que entró
    hace tres horas y sigue en `queued` no aparece en la ventana reciente, pero
    es exactamente el síntoma de que el worker no da abasto.
    """
    from app.core import service_status

    with _counts(recent={"done": 8, "failed": 2}, open_jobs={"queued": 30, "processing": 2}):
        status = await service_status.collect_status()

    assert status["received"] == 10  # todo lo que entró en la ventana
    assert status["done"] == 8
    assert status["failed"] == 2
    # El backlog no sale de la ventana: son 30 encolados ahora mismo.
    assert status["queued"] == 30
    assert status["processing"] == 2
    assert status["window_minutes"] == settings.STATUS_REPORT_WINDOW_MINUTES


async def test_a_quiet_service_reports_zeros_and_not_a_hole():
    """
    Sin actividad el resumen igual sale, con ceros. Es el latido: permite
    distinguir "no llegó ningún video" de "el worker lleva horas muerto", que
    desde afuera se ven idénticos.
    """
    from app.core import service_status

    with _counts():
        status = await service_status.collect_status()

    assert status["received"] == 0
    assert status["queued"] == 0
    assert status["undelivered_callbacks"] == 0


async def test_the_window_used_is_the_configured_one():
    from app.core import service_status

    with _counts() as (recent, _, _):
        await service_status.collect_status()

    (since,) = recent.await_args.args
    from datetime import datetime, timedelta, timezone

    expected = datetime.now(timezone.utc) - timedelta(
        minutes=settings.STATUS_REPORT_WINDOW_MINUTES
    )
    assert abs((since - expected).total_seconds()) < 5


# --- Lo que queda en el log ---


async def test_the_summary_is_emitted_as_one_parseable_line(caplog):
    from app.core import service_status

    with (
        _counts(recent={"done": 3}, open_jobs={"queued": 1}),
        caplog.at_level(logging.INFO, logger="job_events"),
    ):
        await service_status.report_service_status()

    events = [
        json.loads(record.getMessage())
        for record in caplog.records
        if record.name == "job_events"
    ]
    summary = [event for event in events if event["event"] == "service_status"]

    assert len(summary) == 1
    # Los ceros TIENEN que aparecer: un campo ausente no se distingue de un
    # campo que nadie midió.
    assert summary[0]["failed"] == 0
    assert summary[0]["done"] == 3
    assert summary[0]["queued"] == 1


async def test_undelivered_results_raise_a_separate_warning(caplog):
    """
    En su propia línea y con nivel WARNING, para poder filtrarla y alertar
    sobre ella sin parsear el resumen.
    """
    from app.core import service_status

    with _counts(undelivered=3), caplog.at_level(logging.WARNING):
        await service_status.report_service_status()

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]

    assert len(warnings) == 1
    assert "3 resultado(s)" in warnings[0].getMessage()


async def test_a_service_with_nothing_wrong_does_not_warn(caplog):
    """Si advirtiera siempre, la advertencia dejaría de significar algo."""
    from app.core import service_status

    with _counts(recent={"done": 5}, undelivered=0), caplog.at_level(logging.WARNING):
        await service_status.report_service_status()

    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


# --- Las definiciones compartidas ---


def test_undelivered_means_the_shared_definition():
    """
    El conteo sale de `CallbackStatus.needs_attention()`, no de una lista
    escrita a mano: un estado nuevo que requiera atención tiene que entrar solo.
    """
    assert CallbackStatus.needs_attention() == {
        CallbackStatus.REJECTED,
        CallbackStatus.EXHAUSTED,
    }
    assert CallbackStatus.DELIVERED not in CallbackStatus.needs_attention()
    # PENDING tampoco: todavía está en camino, no es una deuda.
    assert CallbackStatus.PENDING not in CallbackStatus.needs_attention()


def test_the_backlog_is_everything_that_is_not_terminal():
    open_states = {s for s in JobStatus if s not in JobStatus.terminal()}

    assert open_states == {JobStatus.QUEUED, JobStatus.PROCESSING}


# --- El cableado: sin esto, el resumen nunca se emite ---


def test_the_report_is_registered_and_scheduled():
    from app.platforms.render.worker import celery

    entry = celery.conf.beat_schedule["report-service-status"]

    assert entry["task"] == "report_service_status_task"
    assert entry["schedule"] == settings.STATUS_REPORT_INTERVAL_MINUTES * 60
    assert "report_service_status_task" in celery.tasks


def test_the_window_is_wider_than_the_interval():
    """
    Si fueran iguales, dos resúmenes consecutivos cubrirían tramos exactamente
    contiguos y cualquier retraso de Beat dejaría un hueco sin reportar. Con la
    ventana más ancha, los tramos se solapan y no se pierde nada.
    """
    assert settings.STATUS_REPORT_WINDOW_MINUTES > settings.STATUS_REPORT_INTERVAL_MINUTES


async def test_callbacks_still_being_retried_are_reported_apart_from_the_debt():
    """
    Un aviso en `retrying` todavía se está atendiendo: contarlo como deuda
    dispararía la advertencia por cada parpadeo de la plataforma. Pero va en
    el resumen, porque si crece es que la plataforma no está respondiendo.
    """
    from app.core import service_status

    with _counts(undelivered=1, retrying=7):
        status = await service_status.collect_status()

    assert status["retrying_callbacks"] == 7
    assert status["undelivered_callbacks"] == 1
