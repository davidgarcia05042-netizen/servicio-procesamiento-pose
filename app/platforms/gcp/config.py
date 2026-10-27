"""
Configuración propia de la plataforma Google Cloud.

Vive acá y no en app/core/config.py porque ninguna otra plataforma la usa. Lee
las mismas variables de entorno (y el mismo .env) que la configuración común.

NO HAY CREDENCIALES ACÁ, a propósito. En Cloud Run el servicio corre con una
cuenta de servicio, y las librerías de Google la toman sola del servidor de
metadatos (Application Default Credentials). No hay archivo de clave que
montar, rotar ni filtrar. En desarrollo, `gcloud auth application-default
login` cumple el mismo papel.
"""

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Quién firma los tokens OIDC de Cloud Tasks y dónde publica sus claves. Son
# los de Google; los del emulador solo se aceptan fuera de producción.
GOOGLE_OIDC_ISSUERS = frozenset({"https://accounts.google.com", "accounts.google.com"})
GOOGLE_OIDC_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"


class GcpSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # La misma variable que la configuración común.
    ENVIRONMENT: str = "development"

    # --- Almacenamiento (Cloud Storage) ---

    # Bucket de Cloud Storage donde espera cada video hasta que lo procesa el
    # worker. Se crea PRIVADO (acceso uniforme, prevención de acceso público)
    # y en la misma región que Cloud Run: así subir y bajar el video no cruza
    # regiones, que es lo que se cobra como egreso.
    GCS_BUCKET: str = ""

    # Emulador local de Cloud Storage (ej. "http://localhost:4443"), el mismo
    # fake-gcs-server de la demo de Render. SOLO para tests y desarrollo: ver
    # `_no_emulators_in_production`.
    STORAGE_EMULATOR_HOST: str = ""

    # --- Cola (Cloud Tasks hacia el worker en Cloud Run) ---

    # Dónde viven las colas: proyecto y región (la misma de Cloud Run).
    GCP_PROJECT: str = ""
    GCP_REGION: str = "us-central1"

    # Dos colas y no una: así un aviso a la plataforma no espera detrás de
    # videos de varios minutos, y cada cola tiene su propio límite de
    # concurrencia y de reintentos (ver deploy/gcp y el README).
    TASKS_VIDEO_QUEUE: str = "pose-videos"
    TASKS_CALLBACK_QUEUE: str = "pose-callbacks"

    # URL del servicio worker en Cloud Run (https://pose-worker-....run.app).
    # Es a donde Cloud Tasks entrega las tareas Y la audiencia del token que
    # las firma: el worker rechaza tokens emitidos para otro destino.
    WORKER_URL: str = ""

    # Cuenta de servicio con la que Cloud Tasks firma el token OIDC de cada
    # tarea. El worker solo acepta tareas firmadas por ESTA cuenta.
    TASKS_SERVICE_ACCOUNT: str = ""

    # Emulador local de Cloud Tasks (ej. "tareas:8123"). SOLO para tests y
    # desarrollo.
    CLOUD_TASKS_EMULATOR_HOST: str = ""

    # Emisor de los tokens OIDC y dónde están sus claves públicas. Por defecto,
    # Google. Con el emulador de Cloud Tasks se apuntan al emulador, que firma
    # con su propia clave; en producción se exigen los de Google.
    TASKS_OIDC_ISSUER: str = "https://accounts.google.com"
    TASKS_OIDC_JWKS_URL: str = GOOGLE_OIDC_JWKS_URL

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT.strip().lower() == "production"

    @property
    def worker_url(self) -> str:
        return self.WORKER_URL.strip().rstrip("/")

    @model_validator(mode="after")
    def _no_emulators_in_production(self) -> "GcpSettings":
        """
        Un emulador en producción significaría videos guardados en un proceso
        sin persistencia ni control de acceso, o un worker que acepta tareas
        firmadas con la clave pública de un emulador. Mejor que no arranque.
        """
        if not self.is_production:
            return self
        if self.STORAGE_EMULATOR_HOST.strip() or self.CLOUD_TASKS_EMULATOR_HOST.strip():
            raise ValueError(
                "STORAGE_EMULATOR_HOST o CLOUD_TASKS_EMULATOR_HOST está definido con "
                "ENVIRONMENT=production. Los emuladores son solo para tests y desarrollo."
            )
        if (
            self.TASKS_OIDC_ISSUER not in GOOGLE_OIDC_ISSUERS
            or self.TASKS_OIDC_JWKS_URL != GOOGLE_OIDC_JWKS_URL
        ):
            raise ValueError(
                "En producción, los tokens de las tareas tienen que ser de Google: "
                "quita TASKS_OIDC_ISSUER y TASKS_OIDC_JWKS_URL del entorno."
            )
        return self

    @model_validator(mode="after")
    def _queue_configured_in_production(self) -> "GcpSettings":
        """
        Sin esto, la API arrancaría sana y el primer video fallaría al encolar.
        Se exige solo en producción: los tests y la herramienta de prueba del
        almacenamiento no usan la cola.
        """
        if not self.is_production:
            return self
        missing = [
            name
            for name in ("GCP_PROJECT", "WORKER_URL", "TASKS_SERVICE_ACCOUNT")
            if not getattr(self, name).strip()
        ]
        if missing:
            raise ValueError(f"La plataforma gcp en producción exige {', '.join(missing)}.")
        if not self.worker_url.startswith("https://"):
            raise ValueError("WORKER_URL tiene que ser https:// (la URL del worker en Cloud Run).")
        return self


gcp_settings = GcpSettings()
