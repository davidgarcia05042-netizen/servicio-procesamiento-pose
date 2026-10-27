"""
Las tareas periódicas en Google Cloud: la tabla (app/platforms/gcp/schedule.py),
el endpoint POST /cron/<nombre> del worker, los comandos de gcloud que la
convierten en trabajos de Cloud Scheduler (app/tools/gcp_scheduler.py) y el
reemplazo local de Cloud Scheduler (app/tools/local_scheduler.py).

La prueba de punta a punta usa el emulador de Cloud Tasks, igual que
test_gcp_tasks.py (ver su docstring para levantarlo en local).
"""

import inspect
from importlib import import_module
from unittest.mock import AsyncMock

import pytest

from app.core.callback_delivery import RETRY_DELAYS
from app.core.config import settings
from app.platforms.gcp import schedule
from app.platforms.gcp.schedule import cron_for, scheduled_jobs
from app.tools import gcp_scheduler, local_scheduler
from tests.gcp_fixtures import (  # noqa: F401 -- fixtures
    TASKS_ACCOUNT,
    WORKER_URL,
    gcp_config,
    issuer,
    running_worker,
    wait_for,
    worker_client,
)

pytestmark = pytest.mark.usefixtures("gcp_config")

JOB_NAMES = [job.name for job in scheduled_jobs()]


# --- La tabla ---


def _render_beat() -> dict:
    from app.platforms.render.worker import celery

    return celery.conf.beat_schedule


def test_google_runs_the_same_periodic_tasks_as_render():
    """Agregar una tarea periódica en una plataforma y olvidarla en la otra rompe la suite."""
    assert set(JOB_NAMES) == set(_render_beat())


@pytest.mark.parametrize("name", JOB_NAMES)
def test_google_runs_them_as_often_as_render_allowing_for_the_minute_floor(name):
    render_every = _render_beat()[name]["schedule"]
    expected = max(schedule.MIN_INTERVAL_SECONDS, -(-render_every // 60) * 60)

    assert schedule.find(name).every_seconds == expected


def _render_target(name: str) -> str:
    """
    Qué función común llama la tarea de Celery de esa entrada de Beat, como
    "módulo:función". Se corre la tarea interceptando `run_async`, así que se
    ve la corrutina que de verdad le pasa, sin ejecutarla.
    """
    from unittest.mock import patch

    from app.platforms.render import worker as render_worker

    task = render_worker.celery.tasks[_render_beat()[name]["task"]]
    called = []

    def capture(coro):
        called.append(f"{coro.cr_frame.f_globals['__name__']}:{coro.cr_code.co_name}")
        coro.close()

    with patch.object(render_worker, "run_async", capture):
        task.run()
    return called[0]


@pytest.mark.parametrize("name", JOB_NAMES)
def test_google_calls_the_same_common_function_as_render(name):
    """Un nombre igual que llamara a otra función pasaría todos los demás tests."""
    assert schedule.find(name).target == _render_target(name)


@pytest.mark.parametrize("job", scheduled_jobs(), ids=JOB_NAMES)
def test_every_job_points_to_a_common_coroutine_without_arguments(job):
    module_name, function_name = job.target.split(":")
    function = getattr(import_module(module_name), function_name)

    assert module_name.startswith("app.core.")
    assert inspect.iscoroutinefunction(function)
    assert not inspect.signature(function).parameters


@pytest.mark.parametrize("job", scheduled_jobs(), ids=JOB_NAMES)
def test_every_job_fits_what_cloud_scheduler_accepts(job):
    assert job.cron  # lanza si la frecuencia no se puede expresar
    assert 15 <= job.attempt_deadline_seconds <= 1800


def test_the_dispatcher_runs_as_often_as_cloud_scheduler_allows():
    """
    Cada minuto, no cada 10 s como en Render (ver el docstring de schedule.py).
    Los dos primeros reintentos de un aviso se corren hasta un minuto; desde el
    tercero, el despachador ya es más fino que la espera y no se nota.
    """
    dispatcher = schedule.find("dispatch-due-callbacks")

    assert dispatcher.every_seconds == schedule.MIN_INTERVAL_SECONDS
    assert dispatcher.every_seconds < RETRY_DELAYS[2]


def test_the_retention_runs_once_a_day_at_night():
    assert schedule.find("purge-expired-data").cron == f"0 {schedule.DAILY_HOUR} * * *"


def test_the_frequencies_come_from_the_configuration(monkeypatch):
    monkeypatch.setattr(settings, "STUCK_JOB_SCAN_INTERVAL_MINUTES", 10)

    assert schedule.find("sweep-stuck-jobs").cron == "*/10 * * * *"


@pytest.mark.parametrize(
    "seconds, cron",
    [
        (60, "* * * * *"),
        (300, "*/5 * * * *"),
        (900, "*/15 * * * *"),
        (7200, "0 */2 * * *"),
        (86400, f"0 {schedule.DAILY_HOUR} * * *"),
    ],
)
def test_cron_expressions(seconds, cron):
    assert cron_for(seconds) == cron


@pytest.mark.parametrize("seconds", [90, 7 * 60, 5 * 3600, 2 * 86400])
def test_frequencies_cron_cannot_express_are_refused(seconds):
    """Cada 7 minutos, en cron, se desfasa en cada cambio de hora: mejor fallar."""
    with pytest.raises(ValueError):
        cron_for(seconds)


# --- El endpoint del worker ---


def _cron(client, name: str, token: str | None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.post(f"/cron/{name}", headers=headers)


@pytest.mark.parametrize("job", scheduled_jobs(), ids=JOB_NAMES)
def test_the_worker_runs_each_periodic_task(worker_client, issuer, monkeypatch, job):
    target = AsyncMock(return_value=["job-1", "job-2"])
    monkeypatch.setattr(job.target.replace(":", "."), target)

    response = _cron(worker_client, job.name, issuer.token())

    assert response.status_code == 200
    assert response.json() == {"task": job.name, "result": 2}
    target.assert_awaited_once_with()


def test_a_summary_that_is_not_a_list_goes_as_is(worker_client, issuer, monkeypatch):
    report = AsyncMock(return_value={"queued": 0, "undelivered_callbacks": 1})
    monkeypatch.setattr("app.core.service_status.report_service_status", report)

    response = _cron(worker_client, "report-service-status", issuer.token())

    assert response.json()["result"] == {"queued": 0, "undelivered_callbacks": 1}


def test_an_unknown_task_is_404(worker_client, issuer):
    assert _cron(worker_client, "borrar-todo", issuer.token()).status_code == 404


def test_without_a_valid_token_no_periodic_task_runs(worker_client, issuer, monkeypatch):
    purge = AsyncMock(return_value=0)
    monkeypatch.setattr("app.core.data_retention.purge_expired_data", purge)

    assert _cron(worker_client, "purge-expired-data", None).status_code == 401
    purge.assert_not_awaited()


def test_a_failing_task_answers_500(worker_client, issuer, monkeypatch):
    """Cloud Scheduler lo registra como fallo; la corrida siguiente retoma lo pendiente."""
    monkeypatch.setattr(
        "app.core.stuck_jobs.sweep_stuck_jobs", AsyncMock(side_effect=RuntimeError("base caída"))
    )

    assert _cron(worker_client, "sweep-stuck-jobs", issuer.token()).status_code == 500


# --- Los comandos de gcloud ---


def _commands() -> list[str]:
    return gcp_scheduler.commands("proyecto-pruebas", "us-central1", WORKER_URL + "/", TASKS_ACCOUNT, "America/Bogota")


def test_one_cloud_scheduler_job_per_periodic_task():
    created = [line for line in _commands() if line.startswith("gcloud scheduler jobs create http")]

    assert [line.split()[5] for line in created] == [f"pose-{name}" for name in JOB_NAMES]


@pytest.mark.parametrize("job", scheduled_jobs(), ids=JOB_NAMES)
def test_each_job_calls_the_worker_signed_like_cloud_tasks(job):
    (command,) = [line for line in _commands() if f"pose-{job.name} " in line]

    assert f"--uri={WORKER_URL}{job.path}" in command
    assert f'--schedule="{job.cron}"' in command
    assert f"--oidc-service-account-email={TASKS_ACCOUNT}" in command
    # La misma audiencia que verifica el worker (WORKER_URL, sin la ruta).
    assert f"--oidc-token-audience={WORKER_URL} " in command
    assert f"--attempt-deadline={job.attempt_deadline_seconds}s" in command
    assert "--max-retry-attempts=0" in command
    assert '--time-zone="America/Bogota"' in command


def test_the_command_tool_refuses_a_worker_that_is_not_https(capsys):
    args = ["--proyecto", "p", "--worker-url", "http://pose-worker", "--cuenta", TASKS_ACCOUNT]

    assert gcp_scheduler.main(args) == 1


# --- El reemplazo local de Cloud Scheduler ---


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def test_the_local_scheduler_fires_everything_at_start_and_then_on_schedule(monkeypatch):
    fired: list[tuple[float, str]] = []
    clock = FakeClock()
    monkeypatch.setattr(local_scheduler, "enqueue", lambda job: fired.append((clock.now, job.name)))

    # Diez minutos simulados, en vueltas de a lo sumo 5 s.
    local_scheduler.run(clock=clock, sleep=clock.sleep, rounds=600 // 5 + 1)

    at_start = {name for when, name in fired if when == 0}
    dispatches = [when for when, name in fired if name == "dispatch-due-callbacks"]
    sweeps = [when for when, name in fired if name == "sweep-stuck-jobs"]
    assert at_start == set(JOB_NAMES)
    assert dispatches == [60.0 * i for i in range(11)]
    assert sweeps == [0.0, 300.0, 600.0]


def test_one_failing_task_does_not_stop_the_others(monkeypatch):
    fired = []

    def enqueue(job):
        if job.name == "sweep-stuck-jobs":
            raise RuntimeError("emulador caído")
        fired.append(job.name)

    monkeypatch.setattr(local_scheduler, "enqueue", enqueue)
    clock = FakeClock()
    local_scheduler.run(clock=clock, sleep=clock.sleep, rounds=1)

    assert set(fired) == set(JOB_NAMES) - {"sweep-stuck-jobs"}


def test_the_local_scheduler_only_runs_against_the_emulator(capsys):
    assert local_scheduler.main() == 1


def test_a_periodic_task_travels_through_the_emulator_to_the_worker(running_worker, monkeypatch):
    """Como en docker-compose.gcp.yml: la llamada pasa por el emulador, firmada."""
    dispatch = AsyncMock(return_value=[])
    monkeypatch.setattr("app.core.callback_retry.dispatch_due_callbacks", dispatch)
    # Una cola que el emulador de los tests ya tiene.
    monkeypatch.setattr(local_scheduler, "QUEUE", "pose-callbacks")

    local_scheduler.enqueue(schedule.find("dispatch-due-callbacks"))
    wait_for(dispatch)

    dispatch.assert_awaited_once_with()
