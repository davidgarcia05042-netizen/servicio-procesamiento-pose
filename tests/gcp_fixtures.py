"""
Lo que comparten los tests de la plataforma gcp (test_gcp_tasks.py y
test_gcp_scheduler.py): la configuración de prueba, un emisor de tokens OIDC
con su propia clave, y un worker real escuchando para el emulador de Cloud
Tasks.

Los fixtures se importan en cada módulo de test; `gcp_config` se activa con
`pytestmark = pytest.mark.usefixtures("gcp_config")`.
"""

import os
import socket
import threading
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from app.platforms.gcp import cloud_tasks, task_auth, worker
from app.platforms.gcp.config import gcp_settings

WORKER_URL = "https://pose-worker-abc123-uc.a.run.app"
TASKS_ACCOUNT = "pose-tasks@proyecto-pruebas.iam.gserviceaccount.com"


@pytest.fixture
def gcp_config(monkeypatch):
    """Configuración de gcp de prueba y clientes nuevos en cada test."""
    for name, value in {
        "GCP_PROJECT": "proyecto-pruebas",
        "GCP_REGION": "us-central1",
        "TASKS_VIDEO_QUEUE": "pose-videos",
        "TASKS_CALLBACK_QUEUE": "pose-callbacks",
        "WORKER_URL": WORKER_URL,
        "TASKS_SERVICE_ACCOUNT": TASKS_ACCOUNT,
        "CLOUD_TASKS_EMULATOR_HOST": "",
        "TASKS_OIDC_ISSUER": "https://accounts.google.com",
    }.items():
        monkeypatch.setattr(gcp_settings, name, value)
    monkeypatch.setattr(cloud_tasks, "_client", None)
    monkeypatch.setattr(task_auth, "_jwks", None)


# --- Tokens ---


class Issuer:
    """Un emisor de tokens OIDC de prueba, con su propia clave RSA."""

    def __init__(self, issuer: str = "https://accounts.google.com", kid: str = "clave-1") -> None:
        self.issuer = issuer
        self.kid = kid
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def jwks(self) -> dict:
        jwk = jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key(), as_dict=True)
        return {"keys": [{**jwk, "kid": self.kid, "alg": "RS256", "use": "sig"}]}

    def token(self, **overrides) -> str:
        now = datetime.now(timezone.utc)
        claims = {
            "iss": self.issuer,
            "aud": WORKER_URL,
            "email": TASKS_ACCOUNT,
            "email_verified": True,
            "sub": "1234567890",
            "iat": now,
            "exp": now + timedelta(minutes=5),
        }
        claims.update(overrides)
        claims = {k: v for k, v in claims.items() if v is not None}
        return jwt.encode(claims, self.key, algorithm="RS256", headers={"kid": self.kid})


@pytest.fixture
def issuer(monkeypatch):
    """Las claves públicas de este emisor, servidas como las de Google."""
    google = Issuer()
    client = jwt.PyJWKClient("https://www.googleapis.com/oauth2/v3/certs")
    monkeypatch.setattr(client, "fetch_data", google.jwks)
    monkeypatch.setattr(task_auth, "_jwks", client)
    return google


@pytest.fixture
def worker_client():
    # Sin `with`: no corre el lifespan, que conectaría la base.
    return TestClient(worker.app, raise_server_exceptions=False)


# --- Contra el emulador de Cloud Tasks ---

EMULATOR = os.environ.get("CLOUD_TASKS_EMULATOR_FOR_TESTS", "").strip()
# Cómo llega el emulador (en un contenedor) hasta el worker de este proceso.
TARGET_HOST = os.environ.get("CLOUD_TASKS_TARGET_HOST_FOR_TESTS", "localhost").strip()
EMULATOR_ISSUER = os.environ.get("CLOUD_TASKS_OIDC_ISSUER_FOR_TESTS", "http://localhost:8980").strip()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("0.0.0.0", 0))
        return sock.getsockname()[1]


@pytest.fixture
def running_worker(monkeypatch):
    """
    Un worker real escuchando, y la cola apuntando al emulador y a ese worker.

    Sin lifespan: no hay base. Cada test sustituye las funciones de la parte
    común que espera que se llamen; lo que se prueba es el camino de la tarea.
    """
    if not EMULATOR:
        pytest.skip("CLOUD_TASKS_EMULATOR_FOR_TESTS no está definido (ver test_gcp_tasks.py)")
    import uvicorn

    port = _free_port()
    worker_url = f"http://{TARGET_HOST}:{port}"
    monkeypatch.setattr(gcp_settings, "CLOUD_TASKS_EMULATOR_HOST", EMULATOR)
    monkeypatch.setattr(gcp_settings, "WORKER_URL", worker_url)
    monkeypatch.setattr(gcp_settings, "TASKS_OIDC_ISSUER", EMULATOR_ISSUER)
    monkeypatch.setattr(gcp_settings, "TASKS_OIDC_JWKS_URL", f"{EMULATOR_ISSUER}/jwks")

    server = uvicorn.Server(uvicorn.Config(worker.app, host="0.0.0.0", port=port, lifespan="off", log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)
    yield worker_url
    server.should_exit = True
    thread.join(timeout=5)
    cloud_tasks.close()


def wait_for(mock: AsyncMock, calls: int = 1, seconds: float = 15.0) -> None:
    deadline = time.monotonic() + seconds
    while mock.await_count < calls and time.monotonic() < deadline:
        time.sleep(0.1)
