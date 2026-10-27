"""
Endurecimiento de Redis, la cola de la plataforma Render: contraseña y TLS
obligatorio en producción (app/platforms/render/config.py).

Se construyen instancias de RenderSettings propias en vez de tocar el `settings`
global, para no dejar estado contaminado entre tests.
"""

import pytest

from app.platforms.render.config import RenderSettings as Settings

LOCAL_BROKER = "redis://localhost:6379/0"
RENDER_BROKER = "rediss://red-abc123:secretoDeRender@oregon-keyvalue.render.com:6379"


def _settings(**overrides) -> Settings:
    """Settings aislada del .env del desarrollador, para que el test sea estable."""
    base = {
        "ENVIRONMENT": "development",
        "REDIS_BROKER_URL": LOCAL_BROKER,
        "REDIS_BACKEND_URL": "redis://localhost:6379/1",
        "REDIS_PASSWORD": "",
        "_env_file": None,
    }
    base.update(overrides)
    return Settings(**base)


# --- Los nombres de las variables ---


def test_the_queue_variables_do_not_collide_with_celery_own_config():
    """
    No pueden llamarse BROKER_URL ni BACKEND_URL.

    Son los nombres de configuración heredados del propio Celery: si están en
    el entorno, Celery los toma TAL CUAL y descarta lo que le pasamos por
    código. El worker terminaba ignorando la contraseña que inyecta
    `_redis_url` y también el ssl_cert_reqs de TLS, sin un solo error visible:
    `celery.conf.broker_url` mostraba las credenciales correctas mientras la
    conexión real iba sin ellas.

    Se descubrió el 2026-09-19 corriendo un worker contra un Redis real: nunca
    llegaba a "ready" y repetía "NOAUTH Authentication required" para siempre.
    """
    prohibidos = {"BROKER_URL", "BACKEND_URL"}
    declarados = set(Settings.model_fields)

    assert not (prohibidos & declarados), (
        "Estos nombres se los queda Celery desde el entorno: usa REDIS_BROKER_URL "
        "y REDIS_BACKEND_URL."
    )
    assert {"REDIS_BROKER_URL", "REDIS_BACKEND_URL"} <= declarados


# --- Contraseña ---


def test_password_is_injected_with_an_explicit_default_user():
    """
    El usuario va nombrado ("default"), no vacío.

    Redis 6+ maneja credenciales por ACL y `requirepass` es la contraseña del
    usuario `default`. Con `redis://:clave@host` las conexiones sueltas
    autentican igual, pero el consumer asíncrono del worker de Celery no:
    queda en "NOAUTH Authentication required" reintentando para siempre.
    Verificado contra un Redis real el 2026-09-19.
    """
    config = _settings(REDIS_PASSWORD="clave-secreta")

    assert config.broker_url == "redis://default:clave-secreta@localhost:6379/0"
    assert config.backend_url == "redis://default:clave-secreta@localhost:6379/1"


def test_url_without_password_is_left_untouched():
    config = _settings()

    assert config.broker_url == LOCAL_BROKER


def test_credentials_already_in_the_url_win_over_redis_password():
    """Caso Render: la Key Value entrega la URL con credenciales incluidas."""
    config = _settings(
        ENVIRONMENT="production",
        REDIS_BROKER_URL=RENDER_BROKER,
        REDIS_BACKEND_URL=RENDER_BROKER,
        REDIS_PASSWORD="otra-clave-que-debe-ignorarse",
    )

    assert "otra-clave-que-debe-ignorarse" not in config.broker_url
    assert "secretoDeRender" in config.broker_url


# --- TLS en producción ---


def test_production_rejects_plaintext_redis():
    """
    Un downgrade silencioso a texto plano es justo lo que no puede pasar: por
    la cola viajan job_id y estado del procesamiento.
    """
    config = _settings(ENVIRONMENT="production", REDIS_BROKER_URL=LOCAL_BROKER)

    with pytest.raises(ValueError, match="exige Redis por TLS"):
        _ = config.broker_url


# --- La excepción: red interna de Render ---


def test_production_allows_plaintext_on_render_internal_host():
    """
    Entre servicios propios de Render el tráfico no sale a internet: va por la
    red privada del proveedor, donde TLS no mitiga un riesgo adicional real.
    """
    config = _settings(
        ENVIRONMENT="production",
        REDIS_BROKER_URL="redis://red-cl4bq5ij1k6c73a1b2c0:6379/0",
        REDIS_BACKEND_URL="redis://red-cl4bq5ij1k6c73a1b2c0:6379/1",
    )

    assert config.broker_url.startswith("redis://")
    assert config.backend_url.startswith("redis://")


def test_production_allows_render_internal_host_with_password():
    config = _settings(
        ENVIRONMENT="production",
        REDIS_BROKER_URL="redis://red-abc123:6379/0",
        REDIS_PASSWORD="clave",
    )

    assert config.broker_url == "redis://default:clave@red-abc123:6379/0"


@pytest.mark.parametrize(
    "hostile_url",
    [
        # La trampa: empieza por "red-" pero es un dominio PÚBLICO. Si el
        # patrón solo mirara el prefijo, esto se colaría.
        "redis://red-cualquiera.atacante.com:6379/0",
        "redis://red-abc123.onrender.com:6379/0",
        # Otros hosts que no son la red interna de Render
        "redis://198.51.100.7:6379/0",
        "redis://cache.ejemplo.com:6379/0",
        "redis://localhost:6379/0",
        "redis://redis:6379/0",
    ],
)
def test_production_still_requires_tls_for_any_other_host(hostile_url):
    """La excepción es SOLO para la red interna de Render, no un permiso general."""
    config = _settings(ENVIRONMENT="production", REDIS_BROKER_URL=hostile_url)

    with pytest.raises(ValueError, match="exige Redis por TLS"):
        _ = config.broker_url


def test_production_accepts_tls_redis():
    config = _settings(
        ENVIRONMENT="production", REDIS_BROKER_URL=RENDER_BROKER, REDIS_BACKEND_URL=RENDER_BROKER
    )

    assert config.broker_url.startswith("rediss://")


def test_development_allows_plaintext_redis():
    """En local el tráfico no sale de la máquina: exigir TLS solo daría fricción."""
    config = _settings(ENVIRONMENT="development")

    assert config.broker_url.startswith("redis://")


def test_tls_urls_carry_ssl_cert_reqs():
    """Sin este parámetro, Celery revienta al abrir una conexión rediss://."""
    config = _settings(ENVIRONMENT="production", REDIS_BROKER_URL=RENDER_BROKER)

    assert "ssl_cert_reqs=required" in config.broker_url


def test_explicit_ssl_cert_reqs_is_respected():
    config = _settings(
        ENVIRONMENT="production",
        REDIS_BROKER_URL=f"{RENDER_BROKER}?ssl_cert_reqs=none",
    )

    assert "ssl_cert_reqs=none" in config.broker_url
    assert "ssl_cert_reqs=required" not in config.broker_url
