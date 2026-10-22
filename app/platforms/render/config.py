"""
Configuración propia de la plataforma Render: Redis (la cola de Celery), el
estado de Celery Beat y el almacenamiento del video (la base, Supabase o
Firebase).

Vive acá y no en app/core/config.py porque ninguna otra plataforma la usa: en
Google Cloud no hay Redis ni Beat, y el video va a Cloud Storage. Lee las
mismas variables de entorno (y el mismo .env) que la configuración común, así
que se configura igual que antes.
"""

import re
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Dirección interna de Render: el identificador del servicio a secas, SIN
# dominio. "red-..." es una Key Value ("red-cl4bq5ij1k6c73a1b2c0") y "dpg-..."
# una base PostgreSQL ("dpg-cl4bq5ij1k6c73a1b2c0-a").
#
# La ausencia de punto es la parte que hace seguro el patrón, no el prefijo.
# Un host como "red-loquesea.atacante.com" empieza igual pero es un dominio
# público que resuelve fuera de Render: exigir que no haya punto es lo que
# impide que la excepción se cuele por ahí.
_RENDER_INTERNAL_HOST = re.compile(r"^(red|dpg)-[a-z0-9-]+$")

# Dónde puede esperar el video (ver `video_storage()` en __init__.py).
STORAGE_BACKENDS = frozenset({"postgres", "supabase", "firebase"})


def is_render_internal_host(hostname: Optional[str]) -> bool:
    """
    ¿Es una dirección que solo resuelve dentro de la red privada de Render?

    POR QUÉ SE ACEPTA SIN TLS: el tráfico entre servicios propios en la misma
    región de Render no sale a internet público, viaja por la red privada del
    proveedor. TLS ahí no mitiga ningún riesgo adicional real; solo agregaría
    configuración de certificados a cambio de nada.

    Esto NO toca el otro extremo del sistema: la conexión hacia la plataforma
    de telefisioterapia sí sale a internet, y mantiene sin cambios su exigencia
    de HTTPS y firma HMAC (ver app/utils/hmac_auth.py). Son dos conexiones
    distintas con modelos de amenaza distintos.
    """
    return bool(hostname) and bool(_RENDER_INTERNAL_HOST.match(hostname))


class RenderSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # La misma variable que la configuración común: en producción se exige
    # que Redis vaya por TLS (ver `_redis_url`).
    ENVIRONMENT: str = "development"

    # Cola. Se dejan como URL completa (y no en piezas host/puerto) porque es
    # lo que entrega Render para su Key Value: se pega tal cual.
    #
    # EL PREFIJO "REDIS_" NO ES DECORATIVO. Antes se llamaban BROKER_URL y
    # BACKEND_URL, que son los nombres de configuración heredados del propio
    # Celery: al encontrarlos en el entorno, Celery los usaba TAL CUAL y
    # descartaba lo que le pasábamos por código. El efecto era silencioso y
    # grave -- el worker ignoraba la contraseña inyectada por `_redis_url` y
    # también el `ssl_cert_reqs` que se agrega para TLS.
    #
    # Se verificó contra un Redis real el 2026-09-19: con la URL apuntando a
    # un Redis sin contraseña, `celery.conf.broker_url` mostraba credenciales
    # y aun así el worker conectaba sin ellas. Renombrarlas lo resolvió.
    REDIS_BROKER_URL: str = "redis://localhost:6379/0"
    REDIS_BACKEND_URL: str = "redis://localhost:6379/1"

    # Contraseña de Redis. Se mantiene aparte de las URLs para no repetir el
    # secreto en dos variables. Si la URL ya trae credenciales (caso Render),
    # esas mandan y esta se ignora.
    REDIS_PASSWORD: str = ""

    # Dónde guarda Celery Beat su estado (cuándo corrió cada tarea periódica).
    #
    # POR QUÉ ES CONFIGURABLE Y NO UN FLAG: el contenedor corre sin root y
    # /app es de root, así que Beat no puede escribir ahí y ARRANCA Y MUERE con
    # "Permission denied: 'celerybeat-schedule'". Sin Beat no corre ni el
    # barrido de jobs colgados ni el resumen de estado, y el resto del servicio
    # sigue funcionando con normalidad, así que el fallo pasa desapercibido.
    #
    # El Dockerfile crea un directorio escribible y apunta esta variable ahí,
    # de modo que la imagen es correcta sin que nadie tenga que acordarse de
    # pasar `-s` en la línea de comandos. El valor por defecto es el de Celery,
    # que en desarrollo local funciona porque el directorio es del usuario.
    BEAT_SCHEDULE_PATH: str = "celerybeat-schedule"

    # --- Almacenamiento del video ---

    # "postgres", "supabase" o "firebase" (ver `video_storage()` en
    # __init__.py). Desde el 2026-10-08, "postgres": el video espera en la
    # misma base de Render que los jobs (ver postgres_storage.py). Supabase
    # Storage y Firebase quedan disponibles; Firebase exige el plan Blaze.
    STORAGE_BACKEND: str = "postgres"

    # Supabase Storage. La URL es la del proyecto (https://<ref>.supabase.co),
    # no la de la base. La clave es la "secret key" (sb_secret_...): se salta
    # RLS, así que solo va en el servidor. El bucket se crea a mano, PRIVADO,
    # con límite de 50 MB por archivo (ver README).
    SUPABASE_URL: str = ""
    SUPABASE_SECRET_KEY: str = ""
    SUPABASE_STORAGE_BUCKET: str = "videos"

    # Firebase Storage
    FIREBASE_CREDENTIALS_PATH: str = "./firebase-credentials.json"
    FIREBASE_STORAGE_BUCKET: str = ""

    # Emulador local de almacenamiento (ej. "http://gcs:4443"). SOLO para la
    # demo y el desarrollo local: ver docker-compose.demo.yml.
    #
    # POR QUÉ EXISTE: desde septiembre de 2024 Cloud Storage for Firebase exige
    # el plan Blaze, o sea una cuenta de facturación vinculada, y mostrar el
    # servicio funcionando no debería depender de esa decisión. Además, con el
    # emulador el video de la demo -- el de una persona real -- nunca sale de
    # la máquina.
    #
    # La variable estándar de Google (`STORAGE_EMULATOR_HOST`) NO alcanza por
    # sí sola: redirige el tráfico, pero firebase-admin le pasa igual sus
    # credenciales al cliente, y el cliente intenta pedir un token a Google
    # antes de cada llamada. Por eso firebase_storage.py arma un cliente
    # anónimo aparte cuando esto está definido.
    #
    # En producción se niega a arrancar si está definido: ver
    # `_no_emulator_in_production`.
    STORAGE_EMULATOR_HOST: str = ""

    @model_validator(mode="after")
    def _no_emulator_in_production(self) -> "RenderSettings":
        """
        Un emulador en producción significaría que los videos se guardan en un
        proceso sin persistencia ni control de acceso, y que el servicio parece
        sano mientras tanto. Mejor que no arranque.
        """
        if self.is_production and self.STORAGE_EMULATOR_HOST.strip():
            raise ValueError(
                "STORAGE_EMULATOR_HOST está definido con ENVIRONMENT=production. "
                "El emulador es solo para la demo local: quítalo del entorno."
            )
        return self

    @model_validator(mode="after")
    def _known_storage_backend(self) -> "RenderSettings":
        """
        Solo el nombre. Que Supabase tenga URL y clave lo comprueba
        `supabase_storage.ensure_configured()` al arrancar la API y el worker,
        y no acá: Beat también construye esta configuración y no toca el
        almacenamiento, así que no debe necesitar la clave.
        """
        backend = self.STORAGE_BACKEND.strip().lower()
        if backend not in STORAGE_BACKENDS:
            raise ValueError(
                f"STORAGE_BACKEND debe ser uno de {sorted(STORAGE_BACKENDS)}, "
                f"no {self.STORAGE_BACKEND!r}"
            )
        self.STORAGE_BACKEND = backend
        return self

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT.strip().lower() == "production"

    def _redis_url(self, url: str) -> str:
        """
        Arma la URL final de Redis: inyecta la contraseña y exige TLS en
        producción, salvo en la red interna de Render.

        Falla en vez de degradar en silencio: si en producción la URL viniera
        como `redis://` hacia un host cualquiera, el tráfico de la cola (que
        incluye los job_id y el estado del procesamiento) viajaría en claro sin
        que nadie se entere. Un arranque que falla se nota; un downgrade
        silencioso no.
        """
        parts = urlsplit(url)

        if self.is_production and parts.scheme != "rediss":
            if not is_render_internal_host(parts.hostname):
                raise ValueError(
                    f"ENVIRONMENT=production exige Redis por TLS, pero la URL usa "
                    f"'{parts.scheme}://' hacia '{parts.hostname}'. Usa 'rediss://', "
                    f"o la dirección interna de Render (tipo 'red-xxxxx', sin dominio)."
                )

        # La credencial de la URL manda: es la que entrega el proveedor.
        #
        # EL USUARIO VA EXPLÍCITO ("default:"), NO VACÍO. Redis 6+ maneja las
        # credenciales por ACL, y `requirepass` es en realidad la contraseña
        # del usuario `default`. Con la forma `redis://:contraseña@host`, las
        # conexiones sueltas igual autentican, pero el consumer asíncrono del
        # worker de Celery NO: falla con "NOAUTH Authentication required" y
        # reintenta para siempre sin llegar a consumir nada.
        #
        # Se verificó contra un Redis real el 2026-09-19: con usuario vacío el
        # worker nunca llega a "ready"; nombrando a `default` arranca. El
        # síntoma engaña, porque publicar en la cola sí funciona -- solo se
        # rompe el lado que consume.
        if self.REDIS_PASSWORD and "@" not in parts.netloc:
            parts = parts._replace(netloc=f"default:{self.REDIS_PASSWORD}@{parts.netloc}")

        # Con rediss://, redis-py necesita saber qué hacer con el certificado;
        # sin este parámetro Celery revienta al abrir la conexión.
        if parts.scheme == "rediss":
            query = dict(parse_qsl(parts.query))
            query.setdefault("ssl_cert_reqs", "required")
            parts = parts._replace(query=urlencode(query))

        return urlunsplit(parts)

    @property
    def broker_url(self) -> str:
        return self._redis_url(self.REDIS_BROKER_URL)

    @property
    def backend_url(self) -> str:
        return self._redis_url(self.REDIS_BACKEND_URL)


render_settings = RenderSettings()
