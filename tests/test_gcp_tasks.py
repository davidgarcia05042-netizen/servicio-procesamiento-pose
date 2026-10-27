"""
La cola en Google Cloud: Cloud Tasks hacia el worker en Cloud Run
(app/platforms/gcp/cloud_tasks.py, task_auth.py y worker.py).

Tres niveles:

  - Sin red: qué tarea se construye, la configuración, y la verificación del
    token OIDC con claves RSA generadas acá mismo.
  - Los endpoints del worker, con tokens firmados de verdad.
  - Contra el emulador de Cloud Tasks (aertje/cloud-tasks-emulator), que habla
    la misma API gRPC que el servicio real: se encola de verdad, el emulador
    le entrega la tarea por HTTP a un worker real y el worker verifica el
    token que firmó el emulador. Corren si CLOUD_TASKS_EMULATOR_FOR_TESTS
    apunta al emulador; el workflow de tests lo levanta. En local:

        docker run -d --rm -p 127.0.0.1:8123:8123 -p 127.0.0.1:8980:8980 `
            ghcr.io/aertje/cloud-tasks-emulator:2.0.1 -host 0.0.0.0 -port 8123 `
            -openid-issuer http://localhost:8980 `
            -initial-queue projects/proyecto-pruebas/locations/us-central1/queues/pose-videos `
            -initial-queue projects/proyecto-pruebas/locations/us-central1/queues/pose-callbacks
        $env:CLOUD_TASKS_EMULATOR_FOR_TESTS = "localhost:8123"
        $env:CLOUD_TASKS_TARGET_HOST_FOR_TESTS = "host.docker.internal"
"""

import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import jwt
import pytest
from google.api_core import exceptions

from app.core import task_queue
from app.core.config import settings
from app.platforms import gcp
from app.platforms.gcp import cloud_tasks, task_auth
from app.platforms.gcp.config import GcpSettings, gcp_settings
from tests.gcp_fixtures import (  # noqa: F401 -- fixtures
    TASKS_ACCOUNT,
    WORKER_URL,
    Issuer,
    gcp_config,
    issuer,
    running_worker,
    wait_for,
    worker_client,
)

pytestmark = pytest.mark.usefixtures("gcp_config")


# --- Configuración ---


def _production(**overrides) -> GcpSettings:
    values = {
        "_env_file": None,
        "ENVIRONMENT": "production",
        "GCP_PROJECT": "proyecto",
        "WORKER_URL": WORKER_URL,
        "TASKS_SERVICE_ACCOUNT": TASKS_ACCOUNT,
    }
    values.update(overrides)
    return GcpSettings(**values)


def test_production_accepts_a_complete_configuration():
    assert _production().worker_url == WORKER_URL


@pytest.mark.parametrize("missing", ["GCP_PROJECT", "WORKER_URL", "TASKS_SERVICE_ACCOUNT"])
def test_production_refuses_to_start_without_the_queue(missing):
    """Si no, la API arrancaría sana y el primer video fallaría al encolar."""
    with pytest.raises(ValueError, match=missing):
        _production(**{missing: ""})


def test_production_requires_an_https_worker():
    with pytest.raises(ValueError, match="https"):
        _production(WORKER_URL="http://pose-worker.run.app")


@pytest.mark.parametrize(
    "override",
    [
        {"CLOUD_TASKS_EMULATOR_HOST": "tareas:8123"},
        {"TASKS_OIDC_ISSUER": "http://tareas:8980"},
        {"TASKS_OIDC_JWKS_URL": "http://tareas:8980/jwks"},
    ],
)
def test_production_refuses_the_emulator(override):
    """Un worker que aceptara tokens firmados por el emulador aceptaría cualquiera."""
    with pytest.raises(ValueError):
        _production(**override)


def test_development_accepts_the_emulator():
    GcpSettings(
        _env_file=None,
        CLOUD_TASKS_EMULATOR_HOST="tareas:8123",
        TASKS_OIDC_ISSUER="http://tareas:8980",
        TASKS_OIDC_JWKS_URL="http://tareas:8980/jwks",
    )


# --- La tarea que se encola ---


class FakeTasksClient:
    def __init__(self, error: Exception | None = None) -> None:
        self.created: list[tuple[str, object]] = []
        self.error = error

    def create_task(self, parent, task, retry=None, timeout=None):
        if self.error:
            raise self.error
        self.created.append((parent, task))
        return task


@pytest.fixture
def fake_client(monkeypatch):
    client = FakeTasksClient()
    monkeypatch.setattr(cloud_tasks, "_client", client)
    return client


def test_a_video_task_goes_to_the_worker_signed_for_it(fake_client):
    cloud_tasks.enqueue_video_processing("job-1")

    ((parent, task),) = fake_client.created
    request = task.http_request
    assert parent == "projects/proyecto-pruebas/locations/us-central1/queues/pose-videos"
    assert request.url == f"{WORKER_URL}/tasks/process-video"
    assert request.http_method.name == "POST"
    assert json.loads(request.body) == {"job_id": "job-1"}
    assert request.headers["Content-Type"] == "application/json"
    assert request.oidc_token.service_account_email == TASKS_ACCOUNT
    assert request.oidc_token.audience == WORKER_URL


def test_a_video_task_is_named_after_its_job(fake_client):
    """Con nombre, Cloud Tasks rechaza crearla dos veces: no se procesa dos veces."""
    cloud_tasks.enqueue_video_processing("job-1")

    ((parent, task),) = fake_client.created
    assert task.name == f"{parent}/tasks/video-job-1"


def test_a_callback_task_goes_to_its_own_queue_without_a_name(fake_client):
    cloud_tasks.enqueue_callback("job-1")

    ((parent, task),) = fake_client.created
    assert parent.endswith("/queues/pose-callbacks")
    assert task.http_request.url == f"{WORKER_URL}/tasks/send-callback"
    assert task.name == ""


def test_a_video_task_that_already_exists_counts_as_enqueued(monkeypatch):
    monkeypatch.setattr(cloud_tasks, "_client", FakeTasksClient(exceptions.AlreadyExists("ya está")))

    cloud_tasks.enqueue_video_processing("job-1")  # no lanza


def test_any_other_error_reaches_the_caller(monkeypatch):
    """La API marca el job FAILED si no pudo encolar: tiene que enterarse."""
    monkeypatch.setattr(cloud_tasks, "_client", FakeTasksClient(exceptions.PermissionDenied("sin rol")))

    with pytest.raises(exceptions.PermissionDenied):
        cloud_tasks.enqueue_video_processing("job-1")


def test_only_an_unavailable_service_is_retried():
    """Un timeout pudo haber creado la tarea: reintentar un aviso podría duplicarlo."""
    predicate = cloud_tasks._CREATE_RETRY._predicate

    assert predicate(exceptions.ServiceUnavailable("caído"))
    assert not predicate(exceptions.DeadlineExceeded("lento"))
    assert not predicate(exceptions.PermissionDenied("sin rol"))


def test_without_a_worker_url_nothing_is_enqueued(fake_client, monkeypatch):
    monkeypatch.setattr(gcp_settings, "WORKER_URL", "")

    with pytest.raises(RuntimeError, match="WORKER_URL"):
        cloud_tasks.enqueue_video_processing("job-1")
    assert fake_client.created == []


def test_the_platform_enqueues_through_cloud_tasks(fake_client, monkeypatch):
    """El puerto común llega a Cloud Tasks con PLATFORM=gcp."""
    monkeypatch.setattr(settings, "PLATFORM", "gcp")

    task_queue.enqueue_video_processing("job-1")
    task_queue.enqueue_callback("job-1")

    assert [task.http_request.url.rsplit("/", 1)[1] for _, task in fake_client.created] == [
        "process-video",
        "send-callback",
    ]


def test_the_video_deadline_outlasts_the_stuck_job_sweep():
    """
    Si el plazo venciera antes que el barrido, Cloud Tasks reintentaría un
    video que el worker todavía procesa: dos copias sobre el mismo job.
    """
    deadline_minutes = cloud_tasks.VIDEO_DISPATCH_DEADLINE.total_seconds() / 60

    assert deadline_minutes > settings.STUCK_JOB_TIMEOUT_MINUTES
    assert deadline_minutes <= 30  # el máximo que admite Cloud Tasks


def test_the_callback_deadline_outlasts_one_delivery_attempt():
    from app.core import callback_delivery

    assert cloud_tasks.CALLBACK_DISPATCH_DEADLINE.total_seconds() > callback_delivery._TIMEOUT_SECONDS


def test_with_the_emulator_the_client_skips_tls_and_credentials(monkeypatch):
    built = {}

    class Recorder:
        def __init__(self, **kwargs):
            built.update(kwargs)

    monkeypatch.setattr(gcp_settings, "CLOUD_TASKS_EMULATOR_HOST", "tareas:8123")
    monkeypatch.setattr(cloud_tasks.tasks_v2, "CloudTasksClient", Recorder)

    cloud_tasks._get_client()

    assert "transport" in built


def test_in_production_the_client_uses_the_service_account(monkeypatch):
    built = []
    monkeypatch.setattr(cloud_tasks.tasks_v2, "CloudTasksClient", lambda **kwargs: built.append(kwargs))

    cloud_tasks._get_client()

    assert built == [{}]


# --- El token de cada tarea ---


def _verify(token: str):
    return task_auth.verify_task_token(f"Bearer {token}")


def test_a_token_from_cloud_tasks_for_this_worker_is_accepted(issuer):
    assert _verify(issuer.token())["email"] == TASKS_ACCOUNT


def test_both_forms_of_the_google_issuer_are_accepted(issuer):
    _verify(issuer.token(iss="accounts.google.com"))


@pytest.mark.parametrize(
    "claims, reason",
    [
        ({"aud": "https://otro-servicio.run.app"}, "emitido para otro servicio"),
        ({"email": "otra-cuenta@proyecto.iam.gserviceaccount.com"}, "otra cuenta de servicio"),
        ({"email_verified": False}, "correo sin verificar"),
        ({"email": None}, "sin cuenta"),
        ({"iss": "https://accounts.example.com"}, "otro emisor"),
        ({"exp": datetime.now(timezone.utc) - timedelta(minutes=1)}, "vencido"),
    ],
)
def test_a_token_that_does_not_match_is_rejected(issuer, claims, reason):
    with pytest.raises(task_auth.TaskAuthError):
        _verify(issuer.token(**claims))


def test_a_token_signed_with_another_key_is_rejected(issuer):
    """Mismo kid, otra clave: alguien que fabricó su propio token."""
    forger = Issuer(kid=issuer.kid)

    with pytest.raises(task_auth.TaskAuthError):
        _verify(forger.token())


def test_an_unsigned_token_is_rejected(issuer):
    unsigned = jwt.encode({"iss": issuer.issuer, "aud": WORKER_URL, "email": TASKS_ACCOUNT}, key=None, algorithm="none")

    with pytest.raises(task_auth.TaskAuthError):
        _verify(unsigned)


@pytest.mark.parametrize("header", [None, "", "Basic abc", "Bearer", "Bearer no-es-un-jwt"])
def test_a_request_without_a_valid_bearer_token_is_rejected(issuer, header):
    with pytest.raises(task_auth.TaskAuthError):
        task_auth.verify_task_token(header)


def test_unreachable_keys_are_unavailable_not_invalid(issuer, monkeypatch):
    """503 y no 401: Cloud Tasks reintenta, y la tarea no se pierde."""

    def unreachable():
        raise jwt.PyJWKClientConnectionError("sin red")

    monkeypatch.setattr(task_auth._jwks, "fetch_data", unreachable)
    # Sin claves en memoria, tiene que pedirlas.
    monkeypatch.setattr(task_auth._jwks, "jwk_set_cache", None)

    with pytest.raises(task_auth.TaskAuthUnavailable):
        _verify(issuer.token())


# --- Los endpoints del worker ---


@pytest.fixture
def jobs(monkeypatch):
    process = AsyncMock(return_value="done")
    attempt = AsyncMock(return_value="delivered")
    monkeypatch.setattr("app.core.video_job.process_video_job", process)
    monkeypatch.setattr("app.core.callback_retry.attempt_callback", attempt)
    return process, attempt


def _post(client, path: str, token: str | None, job_id: str = "job-1", retry_count: str = "0"):
    headers = {"X-CloudTasks-TaskRetryCount": retry_count, "X-CloudTasks-QueueName": "pose-videos"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return client.post(path, json={"job_id": job_id}, headers=headers)


def test_the_worker_processes_a_video_task(worker_client, jobs, issuer):
    process, _ = jobs

    response = _post(worker_client, "/tasks/process-video", issuer.token())

    assert response.status_code == 200
    assert response.json() == {"job_id": "job-1", "status": "done"}
    process.assert_awaited_once_with("job-1")


def test_a_video_that_failed_still_answers_200(worker_client, jobs, issuer):
    """El desenlace ya quedó en la base: que Cloud Tasks lo reintente no cambia nada."""
    process, _ = jobs
    process.return_value = "failed"

    assert _post(worker_client, "/tasks/process-video", issuer.token()).status_code == 200


def test_an_unhandled_error_answers_500_so_cloud_tasks_retries(worker_client, jobs, issuer):
    process, _ = jobs
    process.side_effect = RuntimeError("se cayó la base a mitad")

    assert _post(worker_client, "/tasks/process-video", issuer.token()).status_code == 500


def test_the_worker_attempts_a_callback(worker_client, jobs, issuer):
    _, attempt = jobs

    response = _post(worker_client, "/tasks/send-callback", issuer.token())

    assert response.json() == {"job_id": "job-1", "status": "delivered"}
    attempt.assert_awaited_once_with("job-1")


@pytest.mark.parametrize("path", ["/tasks/process-video", "/tasks/send-callback"])
def test_without_a_valid_token_nothing_runs(worker_client, jobs, issuer, path):
    process, attempt = jobs

    assert _post(worker_client, path, None).status_code == 401
    assert _post(worker_client, path, Issuer().token()).status_code == 401
    process.assert_not_awaited()
    attempt.assert_not_awaited()


def test_a_redelivery_is_logged_with_its_retry_count(worker_client, jobs, issuer, caplog):
    with caplog.at_level("INFO", logger="job_events"):
        _post(worker_client, "/tasks/process-video", issuer.token(), retry_count="2")

    events = [json.loads(r.getMessage()) for r in caplog.records if r.getMessage().startswith("{")]
    (received,) = [e for e in events if e.get("event") == "task_received"]
    assert received["retry_count"] == "2" and received["task"] == "process_video"


def test_a_malformed_task_is_rejected(worker_client, jobs, issuer):
    response = worker_client.post(
        "/tasks/process-video", json={"id": "x"}, headers={"Authorization": f"Bearer {issuer.token()}"}
    )

    assert response.status_code == 422


def test_the_worker_exposes_no_documentation(worker_client, issuer):
    for path in ("/docs", "/openapi.json"):
        assert worker_client.get(path, headers={"Authorization": f"Bearer {issuer.token()}"}).status_code == 404


def test_the_platform_has_no_extra_health_probe():
    """Cloud Tasks es administrado: no hay una instancia nuestra que sondear."""
    assert gcp.health_checks() == {}


# --- Contra el emulador de Cloud Tasks ---


@pytest.fixture
def emulator(running_worker, jobs):
    return jobs


def test_a_video_travels_from_the_queue_to_the_worker(emulator):
    process, _ = emulator
    job_id = str(uuid.uuid4())

    cloud_tasks.enqueue_video_processing(job_id)
    wait_for(process)

    process.assert_awaited_once_with(job_id)


def test_enqueuing_the_same_video_twice_processes_it_once(emulator):
    process, _ = emulator
    job_id = str(uuid.uuid4())

    cloud_tasks.enqueue_video_processing(job_id)
    cloud_tasks.enqueue_video_processing(job_id)
    wait_for(process)
    time.sleep(1.0)  # margen por si llegara una segunda entrega

    assert process.await_count == 1


def test_a_callback_travels_from_the_queue_to_the_worker(emulator):
    _, attempt = emulator
    job_id = str(uuid.uuid4())

    cloud_tasks.enqueue_callback(job_id)
    wait_for(attempt)

    attempt.assert_awaited_once_with(job_id)
