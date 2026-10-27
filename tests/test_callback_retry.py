"""
Entrega de los avisos y planificación de sus reintentos
(app/core/callback_retry.py).

QUÉ SE ESTÁ PROTEGIENDO: que un resultado calculado llegue a la plataforma una
vez, aunque ella esté caída un rato, aunque el worker se reinicie en medio, y
sin que la cola de Redis guarde esperas largas. Antes cada reintento era una
tarea programada con countdown: se duplicaba si la espera superaba el
visibility_timeout y quedaba varada si el worker se reiniciaba (las dos cosas
verificadas contra Redis real el 2026-09-24). Ahora la hora del próximo
intento vive en la tabla y la cola solo lleva tareas inmediatas.
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.dialects import postgresql

from app.constants.job_constants import CallbackStatus, JobStatus
from app.core.callback_delivery import (
    RETRY_DELAYS,
    ROUND_ATTEMPTS,
    CallbackDeliveryError,
    CallbackRejectedError,
)
from app.core.config import settings
from tests.fakes import FakeSession

ENDPOINT = "https://api.plataforma.com/resultados"
POINTS = [{"frame": 0, "angle": 91.5}]


def _row(**overrides) -> SimpleNamespace:
    """Una fila de `jobs` con su aviso pendiente."""
    base = {
        "id": "job-1",
        "status": JobStatus.DONE.value,
        "results_endpoint": ENDPOINT,
        "error": None,
        "results": {"requested_points": [24, 26, 28], "points": POINTS},
        "max_angle": 120.0,
        "min_angle": 45.0,
        "callback_status": CallbackStatus.PENDING.value,
        "callback_attempts": None,
        "callback_next_attempt_at": None,
        "created_at": datetime.now(timezone.utc) - timedelta(minutes=5),
    }
    base.update(overrides)
    return SimpleNamespace(**base)


async def _attempt(row, outcome=None):
    """Corre un intento con la base y la red simuladas. Devuelve lo que pasó."""
    from app.core import callback_retry

    deliver = AsyncMock(side_effect=outcome)
    update = AsyncMock()
    with (
        patch.object(callback_retry, "get_job", new=AsyncMock(return_value=row)),
        patch.object(callback_retry, "update_job", new=update),
        patch.object(callback_retry, "deliver_callback", new=deliver),
    ):
        result = await callback_retry.attempt_callback("job-1")

    written = update.await_args.kwargs if update.await_args else None
    return SimpleNamespace(result=result, deliver=deliver, written=written)


# --- 1. Un intento ---


async def test_a_successful_attempt_is_recorded_as_delivered():
    run = await _attempt(_row())

    assert run.result == CallbackStatus.DELIVERED.value
    endpoint, payload = run.deliver.await_args.args
    assert endpoint == ENDPOINT
    assert payload["results"]["points"] == POINTS
    assert run.written == {
        "callback_status": CallbackStatus.DELIVERED.value,
        "callback_attempts": 1,
        "callback_error": None,
        "callback_next_attempt_at": None,
    }


async def test_attempts_are_counted_across_rounds():
    """
    `callback_attempts` es el total histórico, no el de la ronda. Antes se
    reiniciaba en cada ronda y el total solo se podía reconstruir del log.
    """
    run = await _attempt(_row(callback_attempts=ROUND_ATTEMPTS))

    assert run.written["callback_attempts"] == ROUND_ATTEMPTS + 1


async def test_a_rejection_is_final_and_keeps_its_reason():
    """
    Un 4xx significa que la plataforma rechazó el contenido o la firma: el
    mismo payload falla igual mañana. El motivo es lo que permite distinguir
    un 401 (la firma) de un 422 (el payload).
    """
    run = await _attempt(_row(), outcome=CallbackRejectedError("api.plataforma.com respondió 401"))

    assert run.result == CallbackStatus.REJECTED.value
    assert "401" in run.written["callback_error"]
    assert run.written["callback_next_attempt_at"] is None


async def test_a_transient_failure_leaves_the_next_attempt_written_in_the_table():
    """
    Lo central del diseño: la espera NO se programa en el broker. Queda la
    hora en la fila, y el despachador la cumple.
    """
    before = datetime.now(timezone.utc)
    run = await _attempt(_row(), outcome=CallbackDeliveryError("respondió 503"))

    assert run.result == CallbackStatus.RETRYING.value
    assert run.written["callback_status"] == CallbackStatus.RETRYING.value
    wait = (run.written["callback_next_attempt_at"] - before).total_seconds()
    assert RETRY_DELAYS[0] * 0.8 - 1 <= wait <= RETRY_DELAYS[0] * 1.2 + 1
    assert "503" in run.written["callback_error"]


async def test_the_retry_after_header_is_honoured():
    before = datetime.now(timezone.utc)
    run = await _attempt(_row(), outcome=CallbackDeliveryError("429", retry_after=90.0))

    wait = (run.written["callback_next_attempt_at"] - before).total_seconds()
    assert 89 <= wait <= 91


async def test_a_failure_past_the_window_gives_up():
    """Una vez fuera de la ventana, el aviso queda `exhausted` y pasa a ser deuda visible."""
    old = datetime.now(timezone.utc) - timedelta(hours=settings.CALLBACK_RETRY_WINDOW_HOURS)
    run = await _attempt(_row(created_at=old), outcome=CallbackDeliveryError("503"))

    assert run.result == CallbackStatus.EXHAUSTED.value
    assert run.written["callback_next_attempt_at"] is None


@pytest.mark.parametrize(
    "status",
    [
        CallbackStatus.DELIVERED.value,
        CallbackStatus.REJECTED.value,
        CallbackStatus.RETRYING.value,
        CallbackStatus.EXHAUSTED.value,
        None,
    ],
)
async def test_an_attempt_on_a_notice_that_is_not_pending_does_nothing(status):
    """
    Si la tarea llega cuando el aviso ya no está `pending` -- una re-entrega
    del broker de un intento que ya tuvo desenlace --, actuar igual podría
    mandarle a la plataforma un aviso ya entregado.
    """
    run = await _attempt(_row(callback_status=status))

    assert run.result == "skipped"
    run.deliver.assert_not_awaited()
    assert run.written is None


async def test_an_attempt_on_a_missing_job_does_nothing():
    from app.core import callback_retry

    with (
        patch.object(callback_retry, "get_job", new=AsyncMock(return_value=None)),
        patch.object(callback_retry, "deliver_callback", new=AsyncMock()) as deliver,
    ):
        assert await callback_retry.attempt_callback("no-existe") == "skipped"

    deliver.assert_not_awaited()


async def test_a_notice_whose_endpoint_was_minimized_is_closed_without_sending():
    """Retención borró el endpoint mientras el aviso esperaba: ya no hay a quién avisar."""
    run = await _attempt(_row(results_endpoint=None))

    assert run.result == CallbackStatus.EXHAUSTED.value
    run.deliver.assert_not_awaited()


async def test_each_attempt_leaves_an_event_with_its_next_attempt(caplog):
    with caplog.at_level(logging.INFO, logger="job_events"):
        await _attempt(_row(), outcome=CallbackDeliveryError("503"))

    events = [json.loads(r.getMessage()) for r in caplog.records if r.name == "job_events"]
    retrying = [e for e in events if e["event"] == "callback_retrying"]
    assert len(retrying) == 1
    assert retrying[0]["attempts"] == 1
    assert "next_attempt_at" in retrying[0]


# --- 2. El payload se arma desde la fila ---


def test_a_successful_result_is_built_from_the_row():
    from app.core.callback_queue import payload_from_job

    payload = payload_from_job(_row()).model_dump()

    assert payload == {
        "error": None,
        "results": {"points": POINTS},
        "max_angle": 120.0,
        "min_angle": 45.0,
    }


def test_a_failure_is_built_as_the_failure_payload():
    from app.core.callback_queue import payload_from_job

    row = _row(
        error="processing_error",
        results={"requested_points": [24, 26, 28]},
        max_angle=None,
        min_angle=None,
    )
    payload = payload_from_job(row).model_dump()

    assert payload["error"] == "processing_error"
    assert payload["results"]["points"] == []


def test_the_internal_requested_points_never_leak_to_the_platform():
    """El contrato de la plataforma es `{error, results: {points}, max_angle, min_angle}`."""
    from app.core.callback_queue import payload_from_job

    payload = payload_from_job(_row()).model_dump()

    assert set(payload) == {"error", "results", "max_angle", "min_angle"}
    assert set(payload["results"]) == {"points"}


def test_a_row_with_no_results_does_not_break_the_payload():
    from app.core.callback_queue import payload_from_job

    assert payload_from_job(_row(results=None)).model_dump()["results"]["points"] == []


def test_the_task_carries_only_the_job_id():
    """
    El payload no viaja en el mensaje: se arma desde la tabla en cada
    intento. Así un reintento no depende de que un mensaje haya sobrevivido
    en Redis, y hay una sola fuente de verdad.
    """
    from app.core import callback_queue

    task = MagicMock()
    with patch("app.platforms.render.worker.send_callback_task", task):
        assert callback_queue.enqueue_callback("job-1") is True

    task.delay.assert_called_once_with("job-1")


def test_a_broker_failure_while_enqueueing_is_reported_not_raised(caplog):
    from app.core import callback_queue

    task = MagicMock()
    task.delay.side_effect = ConnectionError("redis caído")
    with patch("app.platforms.render.worker.send_callback_task", task), caplog.at_level(logging.ERROR):
        assert callback_queue.enqueue_callback("job-1") is False

    assert "No se pudo encolar el aviso" in caplog.text


# --- 3. El despachador ---


async def test_the_dispatcher_enqueues_every_due_notice():
    from app.core import callback_retry

    claim = AsyncMock(return_value=["a", "b"])
    with (
        patch.object(callback_retry, "claim_due_callbacks", new=claim),
        patch.object(callback_retry, "enqueue_callback", return_value=True) as enqueue,
    ):
        assert await callback_retry.dispatch_due_callbacks() == ["a", "b"]

    assert [c.args[0] for c in enqueue.call_args_list] == ["a", "b"]
    _, limit = claim.await_args.args
    assert limit == settings.CALLBACK_RETRY_BATCH_SIZE


async def test_a_failed_enqueue_goes_back_to_retrying_right_away():
    """
    Si quedara en `pending`, solo lo encontraría el rescate de avisos
    perdidos, horas después. Devuelto a `retrying` con la hora vencida, lo
    toma la próxima corrida del despachador, en segundos. Y un broker caído no
    frena el despacho de los demás.
    """
    from app.core import callback_retry

    update = AsyncMock()
    with (
        patch.object(callback_retry, "claim_due_callbacks", new=AsyncMock(return_value=["a", "b"])),
        patch.object(callback_retry, "enqueue_callback", side_effect=[False, True]),
        patch.object(callback_retry, "update_job", new=update),
    ):
        assert await callback_retry.dispatch_due_callbacks() == ["b"]

    update.assert_awaited_once()
    assert update.await_args.args[0] == "a"
    fields = update.await_args.kwargs
    assert fields["callback_status"] == CallbackStatus.RETRYING.value
    assert fields["callback_next_attempt_at"] <= datetime.now(timezone.utc)


# --- 4. El rescate de avisos sin tarea ---


async def test_notices_stuck_in_pending_go_back_to_the_cycle(caplog):
    from app.core import callback_retry

    requeue = AsyncMock(return_value=["perdido"])
    before = datetime.now(timezone.utc)
    with (
        patch.object(callback_retry, "requeue_stale_callbacks", new=requeue),
        caplog.at_level(logging.INFO, logger="job_events"),
    ):
        assert await callback_retry.recover_lost_callbacks() == ["perdido"]

    idle_before = requeue.await_args.kwargs["idle_before"]
    expected = before - timedelta(hours=settings.CALLBACK_STALE_PENDING_HOURS)
    assert abs((idle_before - expected).total_seconds()) < 5
    assert "callback_recovered" in caplog.text


def test_the_stale_threshold_outlasts_what_redis_would_redeliver_on_its_own():
    """
    Si un worker muere a mitad de un intento, Redis re-entrega la tarea por su
    cuenta al vencer el visibility_timeout. Si el rescate actuara antes,
    habría dos tareas para el mismo aviso. Además hace falta margen para lo
    que un aviso puede esperar en la cola detrás de videos.
    """
    from app.platforms.render.worker import VISIBILITY_TIMEOUT_SECONDS

    assert settings.CALLBACK_STALE_PENDING_HOURS * 3600 >= VISIBILITY_TIMEOUT_SECONDS + 3600


# --- 5. Las consultas: tomar y marcar en una sola sentencia ---


async def _compiled(call) -> tuple[str, FakeSession]:
    from app.utils import db_connect

    session = FakeSession([SimpleNamespace(id="job-1")])
    with patch.object(db_connect.database, "session", return_value=session):
        ids = await call(db_connect)

    sql = str(
        session.statements[0].compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert ids == ["job-1"]
    return sql, session


async def test_claiming_due_notices_is_one_statement_that_reserves_them():
    """
    Si fueran un SELECT y después un UPDATE, dos corridas solapadas tomarían
    la misma fila y la plataforma recibiría el aviso dos veces.
    """
    now = datetime(2026, 9, 24, tzinfo=timezone.utc)
    sql, session = await _compiled(lambda db: db.claim_due_callbacks(now, 100))

    assert sql.startswith("UPDATE jobs SET")
    assert "callback_status='pending'" in sql
    assert "callback_next_attempt_at=NULL" in sql
    assert "jobs.callback_status = 'retrying'" in sql
    assert "jobs.callback_next_attempt_at <= '2026-09-24" in sql
    assert "jobs.results_endpoint IS NOT NULL" in sql
    assert "ORDER BY jobs.callback_next_attempt_at" in sql
    assert "DESC" not in sql, "invertido: se atendería primero lo menos atrasado"
    assert "LIMIT 100" in sql
    assert "RETURNING jobs.id" in sql
    assert len(session.statements) == 1
    assert session.commits == 1


async def test_requeueing_stale_notices_only_touches_closed_jobs():
    now = datetime(2026, 9, 24, tzinfo=timezone.utc)
    sql, _ = await _compiled(
        lambda db: db.requeue_stale_callbacks(now - timedelta(hours=3), now, 100)
    )

    assert "callback_status='retrying'" in sql
    assert "jobs.callback_status = 'pending'" in sql
    assert "jobs.status IN (" in sql, "un job en curso va a generar su propio aviso"
    assert "jobs.updated_at < '2026-09-23 21:00:00" in sql
    assert "jobs.results_endpoint IS NOT NULL" in sql


# --- 6. El ciclo completo ---


async def test_a_notice_survives_an_outage_through_the_table():
    """
    El recorrido entero sobre una tabla en memoria: la plataforma está caída
    en el primer intento, el aviso queda en `retrying` con su hora, el
    despachador no lo toma antes de tiempo, lo toma cuando la hora llega, y el
    segundo intento lo entrega. En ningún momento hay una espera en la cola.
    """
    from app.core import callback_retry

    table = {"job-1": _row()}
    queue: list[str] = []

    async def get_job(job_id):
        return table.get(job_id)

    async def update_job(job_id, **fields):
        for key, value in fields.items():
            setattr(table[job_id], key, value)

    async def claim_due_callbacks(now, limit):
        due = [
            row.id
            for row in table.values()
            if row.callback_status == CallbackStatus.RETRYING.value
            and row.callback_next_attempt_at <= now
        ]
        for job_id in due:
            await update_job(
                job_id,
                callback_status=CallbackStatus.PENDING.value,
                callback_next_attempt_at=None,
            )
        return due

    def enqueue(job_id):
        queue.append(job_id)
        return True

    deliver = AsyncMock(side_effect=[CallbackDeliveryError("503"), None])
    with (
        patch.object(callback_retry, "get_job", new=get_job),
        patch.object(callback_retry, "update_job", new=update_job),
        patch.object(callback_retry, "claim_due_callbacks", new=claim_due_callbacks),
        patch.object(callback_retry, "deliver_callback", new=deliver),
        patch.object(callback_retry, "enqueue_callback", side_effect=enqueue),
    ):
        assert await callback_retry.attempt_callback("job-1") == "retrying"

        # Antes de la hora, el despachador no lo toma.
        assert await callback_retry.dispatch_due_callbacks() == []
        assert queue == []

        # Cuando la hora llega, sí.
        table["job-1"].callback_next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        assert await callback_retry.dispatch_due_callbacks() == ["job-1"]

        assert await callback_retry.attempt_callback(queue.pop()) == "delivered"

    row = table["job-1"]
    assert row.callback_status == CallbackStatus.DELIVERED.value
    assert row.callback_attempts == 2
    assert deliver.await_count == 2


# --- 7. El cableado en Celery y Beat ---


def test_the_delivery_task_takes_only_the_job_id_and_runs_one_attempt():
    from app.platforms.render import worker

    def fake_run(coroutine):
        name = coroutine.__qualname__
        coroutine.close()
        return name

    with patch.object(worker, "run_async", side_effect=fake_run):
        assert worker.send_callback_task.run("job-1") == "attempt_callback"


def test_beat_runs_the_dispatcher_often_and_drops_stale_runs():
    """
    El despachador corre cada pocos segundos y comparte la cola con los
    videos: sin `expires`, con los workers ocupados, sus corridas se
    acumularían detrás de ellos.
    """
    from app.platforms.render.worker import celery

    entry = celery.conf.beat_schedule["dispatch-due-callbacks"]
    assert entry["task"] == "dispatch_due_callbacks_task"
    assert entry["schedule"] == settings.CALLBACK_DISPATCH_INTERVAL_SECONDS
    assert entry["options"]["expires"] == settings.CALLBACK_DISPATCH_INTERVAL_SECONDS
    assert "dispatch_due_callbacks_task" in celery.tasks


def test_beat_runs_the_recovery_sweep():
    from app.platforms.render.worker import celery

    entry = celery.conf.beat_schedule["recover-lost-callbacks"]
    assert entry["task"] == "recover_lost_callbacks_task"
    assert "recover_lost_callbacks_task" in celery.tasks


def test_the_dispatcher_is_finer_than_the_early_retries():
    """
    Los primeros reintentos están a segundos. Si el despachador corriera cada
    minuto, esos peldaños no existirían en la práctica.
    """
    assert settings.CALLBACK_DISPATCH_INTERVAL_SECONDS < RETRY_DELAYS[1]


def test_the_window_fits_inside_the_retention_period():
    """Insistir con datos que el plazo legal ya venció sería contradictorio."""
    assert settings.CALLBACK_RETRY_WINDOW_HOURS < settings.DATA_RETENTION_DAYS * 24
