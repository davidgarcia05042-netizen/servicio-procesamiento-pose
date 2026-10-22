"""
A dónde se acepta enviar resultados (app/utils/request_validations.py y la
segunda barrera en app/core/callback_delivery.py).

QUÉ SE ESTÁ EVITANDO: el `resultsEndpoint` lo elige quien hace la solicitud.
Sin restricción, este servicio se convierte en un emisor de peticiones firmadas
hacia donde le digan -- direcciones internas de la red del proveedor, el
servicio de metadatos de la nube, o un tercero cualquiera al que la política de
reintentos le mandaría intentos durante horas. Y con `HMAC_ENFORCE` en false,
que es como está hoy porque la plataforma todavía no firma, eso queda al
alcance de cualquiera que llegue a la API.
"""

import logging
from unittest.mock import patch

import pytest

from app.core.config import Settings
from app.dtos.error_success_code import ErrorAndSuccessCodes
from app.utils.request_validations import rejected_endpoint_reason, validate_results_endpoint

PUBLICO = "https://api.plataforma.com/resultados"


def _settings(**overrides) -> Settings:
    base = {"ENVIRONMENT": "development", "CALLBACK_ALLOWED_HOSTS": "", "_env_file": None}
    base.update(overrides)
    return Settings(**base)


def _con(config: Settings):
    from app.utils import request_validations

    return patch.object(request_validations, "settings", config)


# --- Lo que se tiene que bloquear sin lista configurada ---


@pytest.mark.parametrize(
    "url, por_que",
    [
        # El servicio de metadatos de la nube: el destino clásico de un SSRF.
        ("http://169.254.169.254/latest/meta-data/", "link-local"),
        ("http://127.0.0.1:8000/x", "loopback"),
        ("http://[::1]/x", "loopback IPv6"),
        ("http://10.0.0.5/x", "red privada"),
        ("http://192.168.1.10/x", "red privada"),
        ("http://172.16.0.9/x", "red privada"),
        ("http://0.0.0.0/x", "sin especificar"),
        # Sin punto no existe en internet: es localhost o el nombre interno de
        # otro servicio del proveedor.
        ("http://localhost:9099/r", "host interno"),
        ("http://redis:6379/", "host interno"),
        ("http://pose-api/x", "host interno"),
    ],
)
def test_internal_destinations_are_rejected(url, por_que):
    with _con(_settings()):
        assert rejected_endpoint_reason(url) is not None, f"debería rechazarse: {por_que}"


@pytest.mark.parametrize("url", ["", None, "ftp://x/y", "file:///etc/passwd", "https://", "x"])
def test_malformed_endpoints_are_rejected(url):
    with _con(_settings()):
        assert rejected_endpoint_reason(url) is not None


def test_a_public_endpoint_is_accepted():
    """El discriminante: esto no puede bloquear el caso normal."""
    with _con(_settings()):
        assert rejected_endpoint_reason(PUBLICO) is None
        assert rejected_endpoint_reason("http://api.plataforma.com/r") is None
        assert rejected_endpoint_reason("https://webhook.site/abc-123") is None


# --- La lista permitida, que es el control real ---


def test_with_a_list_only_the_listed_hosts_pass():
    config = _settings(CALLBACK_ALLOWED_HOSTS="api.plataforma.com")

    with _con(config):
        assert rejected_endpoint_reason(PUBLICO) is None
        # Un host público cualquiera deja de servir en cuanto hay lista.
        assert rejected_endpoint_reason("https://atacante.com/r") is not None


def test_the_list_is_case_insensitive_and_tolerates_spaces():
    config = _settings(CALLBACK_ALLOWED_HOSTS=" API.Plataforma.com , otra.com ,, ")

    with _con(config):
        assert rejected_endpoint_reason("https://api.PLATAFORMA.com/r") is None
        assert rejected_endpoint_reason("https://otra.com/r") is None


def test_the_list_is_an_explicit_override_of_the_internal_checks():
    """
    Poner un host interno en la lista es una decisión deliberada de quien
    configura el servicio, y es lo que permite apuntar a una máquina local para
    probar. Las comprobaciones automáticas no deben estorbar esa decisión.
    """
    config = _settings(CALLBACK_ALLOWED_HOSTS="localhost,127.0.0.1")

    with _con(config):
        assert rejected_endpoint_reason("http://localhost:9099/r") is None
        assert rejected_endpoint_reason("http://127.0.0.1:9099/r") is None
        # Pero sigue sin aceptar lo que no está en la lista.
        assert rejected_endpoint_reason("http://169.254.169.254/") is not None


# --- HTTPS en producción ---


def test_production_rejects_plaintext_http():
    """
    Por ahí viajan ángulos articulares de un paciente: datos de salud. Mandarlos
    en claro choca con el principio de seguridad de la misma Ley 1581 que
    obliga a no conservarlos de más.
    """
    config = _settings(ENVIRONMENT="production")

    with _con(config):
        assert rejected_endpoint_reason("http://api.plataforma.com/r") is not None
        assert rejected_endpoint_reason(PUBLICO) is None


def test_production_requires_https_even_for_a_listed_host():
    """La lista dice a QUIÉN se le puede enviar, no CÓMO."""
    config = _settings(ENVIRONMENT="production", CALLBACK_ALLOWED_HOSTS="api.plataforma.com")

    with _con(config):
        assert rejected_endpoint_reason("http://api.plataforma.com/r") is not None
        assert rejected_endpoint_reason(PUBLICO) is None


def test_development_allows_plaintext():
    """Exigir TLS en local solo daría fricción para probar."""
    with _con(_settings()):
        assert rejected_endpoint_reason("http://api.plataforma.com/r") is None


# --- Qué se le responde a quien mandó la solicitud ---


def test_the_reason_goes_to_the_log_and_not_to_the_response(caplog):
    """
    Distinguir "malformado" de "host no permitido" le serviría a un atacante
    para averiguar qué direcciones acepta el servicio, y no le sirve de nada a
    la plataforma, que solo necesita saber que su endpoint no se aceptó.
    """
    with _con(_settings()), caplog.at_level(logging.WARNING):
        codigo = validate_results_endpoint("http://169.254.169.254/latest/meta-data/")

    assert codigo == ErrorAndSuccessCodes.INVALID_RESULTS_ENDPOINT
    # El motivo concreto sí queda registrado del lado del servicio.
    assert "169.254.169.254" in caplog.text
    assert "reservada" in caplog.text


# --- La segunda barrera, en la entrega ---


async def test_delivery_refuses_a_forbidden_endpoint_without_sending_anything():
    """
    La validación de entrada bastaría si la fila se hubiera creado hoy. Pero una
    fila pudo guardarse antes de que se restringiera la lista, y el barrido de
    jobs colgados encola callbacks leyendo la tabla, sin volver a pasar por la
    entrada.
    """
    from app.core import callback_delivery

    enviado = []

    class _ClienteEspia:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, content=None, headers=None):
            enviado.append(url)

    with (
        patch.object(callback_delivery.httpx, "AsyncClient", _ClienteEspia),
        pytest.raises(callback_delivery.CallbackRejectedError, match="no permitido"),
    ):
        await callback_delivery.deliver_callback("http://169.254.169.254/", {"error": None})

    assert enviado == [], "no debería haberse enviado nada"


async def test_delivery_rejection_is_definitive_and_not_retryable():
    """
    Tiene que ser `CallbackRejectedError` y no `CallbackDeliveryError`:
    reintentar una dirección no permitida durante horas es justamente el abuso
    que esto evita.
    """
    from app.core import callback_delivery

    assert not issubclass(
        callback_delivery.CallbackRejectedError, callback_delivery.CallbackDeliveryError
    )
