"""
Configuración leída UNA sola vez al arranque (patrón tomado del repo de
referencia: evita releer el archivo de entorno en cada request/tarea).

Tanto la API como el worker importan este mismo `settings`, así que ambos ven
exactamente la misma configuración.

Acá va solo lo COMÚN a todas las plataformas de despliegue. Lo propio de cada
una (en Render: Redis, Beat y el almacenamiento del video) vive en su paquete,
dentro de app/platforms/ (ver su __init__.py).
"""

from urllib.parse import SplitResult, parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.constants.pose_models import MODEL_COMPLEXITY

# Paquetes de app/platforms/ que ya se pueden desplegar.
SUPPORTED_PLATFORMS = frozenset({"render", "gcp"})

# Paquetes que implementan la interfaz pero todavía no están completos para
# producción (ver el __init__.py de cada uno). Se aceptan en desarrollo, para
# probarlos de punta a punta con emuladores, y NO en producción. Una plataforma
# pasa a SUPPORTED_PLATFORMS al terminarla.
PLATFORMS_UNDER_CONSTRUCTION: frozenset[str] = frozenset()


def _is_unix_socket(parts: SplitResult) -> bool:
    """
    ¿La URL de PostgreSQL va SOLO por un socket Unix?

    Es la forma `postgresql+asyncpg://usuario:clave@/base?host=/ruta`: sin host
    de red y con `host` apuntando a un directorio (asyncpg agrega el nombre del
    socket, `.s.PGSQL.5432`). Se exige que TODOS los hosts sean rutas: una URL
    que además nombre un host de red, o una lista que mezcle rutas con hosts,
    podría terminar conectándose por la red y no se exime de TLS.

    Una URL sin host de ningún tipo tampoco cuenta: asyncpg probaría por su
    cuenta sockets y localhost, y lo que termine usando no se ve en la URL.
    """
    if parts.hostname:
        return False
    hosts = [
        host.strip()
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key == "host"
        for host in value.split(",")
    ]
    return bool(hosts) and all(host.startswith("/") for host in hosts)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # "development" | "production". En producción se exige TLS hacia la base
    # (ver `database_url`) y la plataforma agrega sus propias exigencias.
    ENVIRONMENT: str = "development"

    # Dónde se despliega: elige el paquete de app/platforms/ (almacenamiento,
    # cola, tareas periódicas, sondeos de /health). Ver
    # app/platforms/__init__.py.
    PLATFORM: str = "render"

    # PostgreSQL. El driver va explícito en la URL: ver `_force_async_driver`.
    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/pose_service"

    # Autenticación con la plataforma cliente (HMAC, no cuentas de usuario)
    HMAC_SECRET: str = ""
    HMAC_MAX_SKEW_SECONDS: int = 300

    # Si es False, una firma ausente o inválida solo genera un log de
    # advertencia y la solicitud continúa. Arranca en False porque la
    # plataforma todavía no firma de su lado (bloqueador #2 de docs/DECISIONES_TECNICAS.md):
    # ponerlo en True antes de que lo hagan rechazaría todo su tráfico.
    HMAC_ENFORCE: bool = False

    # Validación de video
    MAX_VIDEO_SIZE_MB: int = 200

    # Límite de tasa del POST /video/ (sintaxis de slowapi, ej. "10/minute")
    RATE_LIMIT_VIDEO: str = "10/minute"

    # Modelo de pose por defecto
    POSE_MODEL: str = "mediapipe_heavy"

    # Un job que sigue en PROCESSING más de este tiempo se da por perdido: el
    # worker murió sin alcanzar a marcarlo FAILED.
    #
    # VALOR PROVISIONAL: todavía no hay medición de cuánto tarda un video real
    # en el hardware de Render, así que se eligió muy por encima de cualquier
    # procesamiento plausible. Quedarse corto es lo peligroso: cerraría como
    # atascado un job que solo era lento, y el worker seguiría trabajando sobre
    # una fila ya marcada FAILED.
    STUCK_JOB_TIMEOUT_MINUTES: int = 15

    # Un job que sigue en QUEUED más de este tiempo se da por perdido: nadie lo
    # tomó nunca. Pasa si la tarea encolada se pierde (un reinicio del broker
    # vacía la cola) o si no queda ningún worker vivo para consumirla.
    #
    # ES MÁS ALTO QUE EL DE PROCESSING A PROPÓSITO, y no por prudencia genérica:
    # esperar en la cola es NORMAL. Con C procesos en el worker (`--concurrency`
    # en Render), una acumulación de N videos hace que el último espere
    # alrededor de N/C veces lo que tarda un video. Si este umbral quedara por debajo de esa espera, el barrido
    # cerraría como perdidos justamente los jobs de un pico de carga -- el peor
    # momento posible para equivocarse.
    #
    # VALOR PROVISIONAL: el número correcto depende de cuánto tarda un video en
    # el hardware real y de qué acumulación se considera aceptable, y eso lo
    # mide app/tools/benchmark_pipeline.py. Hasta entonces, una hora es muy
    # superior a cualquier espera plausible en las pruebas.
    STUCK_JOB_QUEUED_TIMEOUT_MINUTES: int = 60

    # --- Observabilidad ---

    # Nivel del log del servicio. Se deja configurable porque en Render el log
    # es lo único que se ve desde afuera, y bajarlo a DEBUG para diagnosticar
    # un incidente no debería requerir un despliegue con cambio de código.
    LOG_LEVEL: str = "INFO"

    # Cada cuánto el worker emite el resumen del estado del servicio
    # (ver app/core/service_status.py).
    STATUS_REPORT_INTERVAL_MINUTES: int = 15

    # Ventana que cubre ese resumen: cuántos videos entraron y cómo terminaron.
    # Va por separado del intervalo a propósito. Si fueran el mismo número, dos
    # resúmenes consecutivos cubrirían tramos exactamente contiguos y cualquier
    # retraso de Beat dejaría un hueco sin reportar. Con la ventana más ancha
    # que el intervalo, los tramos se solapan y no se pierde nada.
    STATUS_REPORT_WINDOW_MINUTES: int = 60

    # --- A dónde puede enviarse el callback ---

    # Lista separada por comas de los hosts a los que se acepta enviar
    # resultados (ej. "api.plataforma.com,staging.plataforma.com").
    #
    # POR QUÉ EXISTE: el `resultsEndpoint` lo elige QUIEN HACE LA SOLICITUD.
    # Sin restricción, este servicio envía peticiones firmadas a la dirección
    # que le digan, y su política de reintentos las repite durante horas. Con
    # HMAC_ENFORCE en false -- que es como está hoy, porque la plataforma
    # todavía no firma -- eso queda al alcance de cualquiera que llegue a la
    # API.
    #
    # Vacía = sin lista, que es lo que permite desarrollar y probar contra
    # webhook.site. En producción hay que llenarla: mientras esté vacía, lo
    # único que protege son las comprobaciones de HTTPS y de direcciones
    # internas, que son defensa en profundidad y no el control real.
    CALLBACK_ALLOWED_HOSTS: str = ""

    # --- Reintentos de callbacks (ver app/core/callback_retry.py) ---

    # Cada cuánto el despachador busca los avisos cuyo próximo intento ya
    # venció y los encola. Es la resolución de los reintentos: el primero está
    # a 5 s, así que con 10 s de ciclo se hace entre los 5 y los 15 s. Cuesta
    # una consulta sobre una columna indexada.
    CALLBACK_DISPATCH_INTERVAL_SECONDS: int = 10

    # Pausa entre una ronda de reintentos (9 intentos en ~5,7 h) y la
    # siguiente, mientras siga abierta la ventana.
    CALLBACK_RETRY_INTERVAL_MINUTES: int = 60

    # Hasta cuándo se insiste, medido desde que ENTRÓ el job.
    #
    # POR QUÉ HAY UN TOPE: sin él, un endpoint que quedó mal configurado
    # recibiría rondas de nueve intentos para siempre, y eso deja de ser
    # recuperación para volverse hostigamiento. Con el tope, un resultado que
    # no se pudo entregar en tres días queda en `exhausted` y contado en
    # `undelivered_callbacks` del resumen de estado, que es donde alguien lo
    # tiene que ver.
    #
    # Tres días es la ventana de Stripe, que es la referencia más conservadora
    # de las que se revisaron al escribir la política de reintentos.
    CALLBACK_RETRY_WINDOW_HOURS: int = 72

    # Cuánto tiene que llevar un aviso en `pending` para darlo por perdido y
    # devolverlo al ciclo de reintentos.
    #
    # QUÉ CUBRE: `pending` significa "encolado para un intento inmediato", que
    # debería resolverse en segundos. Si la tarea se pierde -- el worker muere
    # a mitad del intento, o el broker se reinicia y vacía la cola --, la fila
    # se queda así, y `pending` no se cuenta como deuda en el resumen de estado.
    #
    # EL VALOR NO ES ARBITRARIO: tiene que superar el visibility_timeout del
    # broker (1 hora, ver app/platforms/render/worker.py), que es cuando Redis re-entrega
    # por su cuenta una tarea de un worker muerto, más lo que un aviso puede
    # esperar en la cola detrás de videos. Si fuera menor, el rescate
    # duplicaría una entrega que Redis estaba por re-entregar. Hay un test que
    # protege esa relación.
    CALLBACK_STALE_PENDING_HOURS: int = 3

    # Cuántos avisos toma como máximo cada corrida del despachador y del
    # rescate. Si la plataforma estuvo caída un día entero, la primera corrida
    # después no debe inundar la cola y dejar los videos nuevos detrás.
    CALLBACK_RETRY_BATCH_SIZE: int = 100

    # --- Retención de datos (Ley 1581 de 2012) ---

    # Cuántos días se conservan los datos personales de un job ya cerrado antes
    # de minimizarlo (ver app/core/data_retention.py).
    #
    # VALOR PROVISIONAL: el plazo correcto NO es una decisión técnica. Depende
    # de la política de tratamiento de datos de la plataforma y de cuánto
    # tiempo necesite el equipo clínico poder consultar un resultado. 30 días
    # es un punto de partida conservador que hay que confirmar con quien
    # responda por el tratamiento.
    DATA_RETENTION_DAYS: int = 30

    # Cada cuánto corre el barrido de retención. Una vez al día alcanza: el
    # plazo se mide en días, así que revisarlo más seguido solo agrega
    # consultas sin adelantar nada.
    RETENTION_SCAN_INTERVAL_HOURS: int = 24

    # Cuántos jobs minimiza como máximo cada corrida. Sin tope, la primera
    # ejecución sobre una base con historia acumulada intentaría borrar miles
    # de videos del almacenamiento en una sola tarea, bloqueando al worker. Lo
    # que sobre queda para la corrida siguiente.
    RETENTION_BATCH_SIZE: int = 500

    # Cada cuánto corre el barrido. Más fino que el umbral para que detectarlo
    # no tarde hasta el doble del umbral.
    STUCK_JOB_SCAN_INTERVAL_MINUTES: int = 5

    # --- Pool de conexiones a PostgreSQL (por proceso) ---

    # Conexiones que cada proceso mantiene abiertas, y cuántas más puede abrir
    # en un pico. Los valores por defecto son los de SQLAlchemy.
    #
    # LA BASE TIENE UN TECHO COMPARTIDO por todos los procesos: la API, cada
    # proceso hijo del worker, la API nueva que convive con la vieja durante un
    # despliegue y el `alembic upgrade head` del pre-despliegue. En Render
    # Postgres son 100 conexiones (planes de menos de 8 GB de RAM); en el
    # Session pooler gratuito de Supabase eran 15. render.yaml fija valores
    # que caben; hay un test que comprueba la suma.
    DB_POOL_SIZE: int = 5
    DB_MAX_OVERFLOW: int = 10

    @field_validator("PLATFORM")
    @classmethod
    def _known_platform(cls, value: str) -> str:
        """Una plataforma sin paquete en app/platforms/ fallaría al primer encolado."""
        normalized = value.strip().lower()
        known = SUPPORTED_PLATFORMS | PLATFORMS_UNDER_CONSTRUCTION
        if normalized not in known:
            raise ValueError(f"PLATFORM debe ser una de {sorted(known)}, no {value!r}")
        return normalized

    @model_validator(mode="after")
    def _no_unfinished_platform_in_production(self) -> "Settings":
        """
        Una plataforma a medias desplegada en serio se vería sana y le faltaría
        algo que nadie nota a tiempo (a gcp, antes de la etapa 4, le faltaban
        los reintentos de los avisos y la retención de datos).
        """
        if self.is_production and self.PLATFORM in PLATFORMS_UNDER_CONSTRUCTION:
            raise ValueError(
                f"PLATFORM={self.PLATFORM} todavía está en construcción: se puede usar en "
                f"desarrollo, no con ENVIRONMENT=production (ver app/platforms/{self.PLATFORM})."
            )
        return self

    @field_validator("POSE_MODEL")
    @classmethod
    def _known_pose_model(cls, value: str) -> str:
        """
        Un modelo desconocido NO puede degradar en silencio a otro. Antes, un
        valor como "yolo26_nano" corría MediaPipe Heavy sin avisar, y la tabla
        `jobs` registraba "yolo26_nano" como el modelo usado: exactamente la
        atribución falsa que la columna `pose_model` existe para evitar.
        """
        normalized = value.strip().lower()
        if normalized not in MODEL_COMPLEXITY:
            raise ValueError(
                f"POSE_MODEL debe ser uno de {sorted(MODEL_COMPLEXITY)}, no {value!r}"
            )
        return normalized

    @field_validator("LOG_LEVEL")
    @classmethod
    def _known_log_level(cls, value: str) -> str:
        """
        Un nivel mal escrito (LOG_LEVEL=INF0) haría que logging lo ignore en
        silencio y el servicio arrancaría con el nivel por defecto sin avisar.
        Mejor que no arranque.
        """
        normalized = value.strip().upper()
        valid = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}
        if normalized not in valid:
            raise ValueError(f"LOG_LEVEL debe ser uno de {sorted(valid)}, no {value!r}")
        return normalized

    @field_validator("DATABASE_URL")
    @classmethod
    def _force_async_driver(cls, value: str) -> str:
        """
        Obliga a que la URL use el driver asíncrono (`postgresql+asyncpg://`).

        POR QUÉ HACE FALTA: Render entrega la URL de su PostgreSQL como
        `postgresql://usuario:clave@host/base`, sin driver. SQLAlchemy, ante
        una URL sin driver, carga el que trae por defecto, que es psycopg2 --
        y psycopg2 NO está en requirements.txt, porque todo este servicio habla
        con la base de forma asíncrona (asyncpg). El resultado sería un
        `ModuleNotFoundError: psycopg2` en el primer arranque del servicio Y en
        `alembic upgrade head`, que es el comando de pre-despliegue: o sea, un
        despliegue que falla sin que la tabla `jobs` llegue a crearse.

        Se normaliza acá y no en cada punto de uso porque son varios (el engine
        de la aplicación y alembic/env.py) y basta con que uno se olvide para
        reintroducir el problema.

        También se acepta el esquema `postgres://`, que es el que usan por
        costumbre varios proveedores.

        Y se traduce `sslmode` a `ssl`. Las URLs de proveedores externos (por
        ejemplo Supabase) piden TLS con `?sslmode=require`, que es la forma de
        libpq; SQLAlchemy se lo pasa tal cual a asyncpg, que no lo conoce y
        falla al conectar con `TypeError: connect() got an unexpected keyword
        argument 'sslmode'` (verificado el 2026-10-05). asyncpg acepta los
        mismos valores bajo el nombre `ssl`.
        """
        for legacy in ("postgresql://", "postgres://"):
            if value.startswith(legacy):
                value = "postgresql+asyncpg://" + value[len(legacy) :]
                break

        parts = urlsplit(value)
        query = parse_qsl(parts.query, keep_blank_values=True)
        if any(key == "sslmode" for key, _ in query):
            has_ssl = any(key == "ssl" for key, _ in query)
            query = [
                ("ssl", val) if key == "sslmode" else (key, val)
                for key, val in query
                if not (key == "sslmode" and has_ssl)
            ]
            value = urlunsplit(parts._replace(query=urlencode(query)))
        return value

    @property
    def max_video_size_bytes(self) -> int:
        return self.MAX_VIDEO_SIZE_MB * 1024 * 1024

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT.strip().lower() == "production"

    @property
    def database_url(self) -> str:
        """
        La URL de PostgreSQL, exigiendo TLS en producción salvo hacia la red
        privada de la plataforma o por un socket Unix. Es la que usan el engine
        y Alembic.

        Una base fuera de la red privada se alcanza por internet, y por esta
        conexión viajan los resultados de salud de los pacientes: sin TLS
        irían en claro. Qué host cuenta como red privada lo decide cada
        plataforma (`is_private_network_host`; en Render, "dpg-..."). Falla al
        arrancar en vez de degradar en silencio.

        UN SOCKET UNIX NO SALE DE LA MÁQUINA, sea cual sea la plataforma (ver
        `_is_unix_socket`). Es como Cloud Run entrega Cloud SQL: un socket en
        /cloudsql/<proyecto>:<región>:<instancia>, atendido por el proxy de
        Cloud SQL, que cifra con TLS el tramo hasta la base y autoriza con la
        cuenta de servicio. Pedir TLS sobre el socket no agregaría nada.

        `ssl=prefer` (lo que hace asyncpg si la URL no dice nada) NO alcanza:
        cae a texto plano si el otro extremo, o alguien en el medio, no ofrece
        TLS.
        """
        url = self.DATABASE_URL
        if not self.is_production:
            return url

        parts = urlsplit(url)
        if _is_unix_socket(parts):
            return url

        from app.platforms import current  # import tardío: app.platforms importa este módulo

        if current().is_private_network_host(parts.hostname):
            return url

        ssl_mode = dict(parse_qsl(parts.query)).get("ssl", "")
        if ssl_mode not in {"require", "verify-ca", "verify-full"}:
            raise ValueError(
                f"ENVIRONMENT=production exige TLS hacia una base fuera de la red privada, "
                f"pero la URL hacia '{parts.hostname}' no lo pide. Agrega "
                f"'?sslmode=require' (o 'ssl=require') a DATABASE_URL."
            )
        return url

    @property
    def callback_allowed_hosts(self) -> set[str]:
        """Los hosts de `CALLBACK_ALLOWED_HOSTS`, normalizados a minúsculas."""
        return {
            host.strip().lower()
            for host in self.CALLBACK_ALLOWED_HOSTS.split(",")
            if host.strip()
        }


settings = Settings()
