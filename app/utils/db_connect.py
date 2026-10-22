"""
Conexión a PostgreSQL (reemplaza MongoDB del repo de referencia).

Una sola instancia de engine/sessionmaker por PROCESO, igual que la
`db_connect.py` de la referencia hacía con Mongo. Esto importa especialmente
con Celery: asyncpg ata sus conexiones al event loop donde se crearon, así
que el engine debe construirse DENTRO del loop persistente del worker (ver
app/platforms/render/worker.py::init_worker), no a nivel de import.

La tabla `jobs` usa una columna JSONB para el payload de resultados, que es
variable: cuántos puntos devuelve y qué ángulos se calculan depende del
ejercicio que envíe la plataforma.

La tabla `videos` guarda los videos en espera cuando el almacenamiento es la
propia base (ver `StoredVideo`).
"""

from datetime import datetime, timezone
from typing import Any, NamedTuple, Optional

from sqlalchemy import (
    DateTime,
    Float,
    Integer,
    LargeBinary,
    String,
    Text,
    case,
    func,
    null,
    or_,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.constants.job_constants import CallbackStatus, JobStatus
from app.core.config import settings
from app.utils.logger import get_logger

logger = get_logger("db")


class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Job(Base):
    """Un trabajo de procesamiento de video enviado por la plataforma."""

    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, index=True)

    exercise: Mapped[Optional[str]] = mapped_column(String(100))
    results_endpoint: Mapped[Optional[str]] = mapped_column(Text)
    video_url: Mapped[Optional[str]] = mapped_column(Text)

    # Payload variable de resultados: los keypoints por frame y lo que el
    # ejercicio requiera. JSONB porque la forma cambia según el ejercicio y no
    # queremos una migración por cada variante.
    results: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB)

    max_angle: Mapped[Optional[float]] = mapped_column(Float)
    min_angle: Mapped[Optional[float]] = mapped_column(Float)
    error: Mapped[Optional[str]] = mapped_column(Text)

    # Qué modelo de pose se pidió para este job. Se guarda por fila y no se
    # deduce de la config porque la config cambia con el tiempo: sin esto, un
    # job viejo quedaría atribuido al modelo que esté configurado hoy.
    pose_model: Mapped[Optional[str]] = mapped_column(String(50))

    # Segundos que tardó el worker en procesar, medidos alrededor del trabajo
    # real. No se deriva de updated_at - created_at porque esa resta incluye
    # el tiempo que el job pasó esperando en la cola.
    processing_seconds: Mapped[Optional[float]] = mapped_column(Float)

    # Cómo terminó el aviso a la plataforma. Dimensión aparte de `status` (ver
    # CallbackStatus): el procesamiento puede haber salido bien y el aviso no.
    # Queda en la tabla y no solo en el log porque los logs de Render son
    # efímeros, y esto es justo lo que hay que poder consultar días después
    # para saber qué resultados nunca llegaron a la plataforma.
    callback_status: Mapped[Optional[str]] = mapped_column(String(20), index=True)
    callback_attempts: Mapped[Optional[int]] = mapped_column(Integer)
    callback_error: Mapped[Optional[str]] = mapped_column(Text)

    # Cuándo toca el próximo intento de un aviso en `retrying`. La espera entre
    # reintentos vive ACÁ y no en el broker: una tarea de Celery programada
    # con countdown queda retenida en Redis, se duplica si la espera supera el
    # visibility_timeout y queda varada si el worker se reinicia. Ver
    # app/core/callback_retry.py.
    callback_next_attempt_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), index=True
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class StoredVideo(Base):
    """
    Un video esperando a que lo procese el worker, cuando el almacenamiento es
    la propia base (STORAGE_BACKEND=postgres, en Render: ver
    app/platforms/render/postgres_storage.py). Con otro almacenamiento, la
    tabla queda vacía.

    El modelo vive acá y no en el adaptador porque el esquema es uno solo para
    todas las plataformas: hay una sola cadena de migraciones, y el test que
    las ata a los modelos mira este `Base`.

    La fila dura lo que el video espera en cola: el worker la borra al cerrar
    el job (ver app/core/video_job.py).
    """

    __tablename__ = "videos"

    # La misma ruta que queda en `jobs.video_url` (ver app/utils/video_storage.py).
    path: Mapped[str] = mapped_column(Text, primary_key=True)
    content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)


class Database:
    """Engine + sessionmaker por proceso. Ver docstring del módulo sobre el loop."""

    def __init__(self) -> None:
        self._engine: Optional[AsyncEngine] = None
        self._sessionmaker: Optional[async_sessionmaker[AsyncSession]] = None

    async def connect(self, create_tables: bool = False) -> None:
        """
        El esquema lo maneja Alembic (`alembic upgrade head`), no esta función.

        `create_tables` quedó en False a propósito: si la app creara las tablas
        al arrancar, lo haría sin registrar nada en `alembic_version`, y la
        siguiente migración correría contra una base que Alembic cree vacía.
        Sigue disponible en True solo para escenarios desechables (una base de
        prueba de usar y tirar), nunca para desarrollo ni producción.
        """
        if self._engine is not None:
            return
        self._engine = create_async_engine(
            settings.database_url,
            pool_pre_ping=True,
            pool_size=settings.DB_POOL_SIZE,
            max_overflow=settings.DB_MAX_OVERFLOW,
        )
        self._sessionmaker = async_sessionmaker(self._engine, expire_on_commit=False)
        if create_tables:
            async with self._engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
        logger.info("Conexión a PostgreSQL establecida")

    async def disconnect(self) -> None:
        """
        Cierra el pool. Deja rastro en el log a propósito: es lo que distingue
        un apagado ordenado de un SIGKILL, y en Render los dos casos se ven
        igual desde afuera (el servicio simplemente desaparece). Si esta línea
        no aparece, el proceso se murió sin llegar a limpiar.
        """
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
            self._sessionmaker = None
            logger.info("Conexión a PostgreSQL cerrada")

    def session(self) -> AsyncSession:
        if self._sessionmaker is None:
            raise RuntimeError(
                "La base de datos no está conectada en este proceso. "
                "FastAPI conecta en startup; Celery en worker_process_init."
            )
        return self._sessionmaker()


database = Database()


# --- Operaciones sobre jobs (equivalente al db_query.py de la referencia) ---


async def insert_job(
    job_id: str,
    exercise: str,
    results_endpoint: str,
    pose_model: Optional[str] = None,
    video_url: Optional[str] = None,
    results: Optional[dict[str, Any]] = None,
) -> None:
    # Solo los opcionales que llegaron. Pasarle `results=None` al modelo no es
    # lo mismo que no pasarlo: en una columna JSONB, el ORM guarda el `null`
    # de JSON y no el NULL de SQL (ver `anonymize_jobs`).
    optional = {
        name: value
        for name, value in (("video_url", video_url), ("results", results))
        if value is not None
    }
    async with database.session() as session:
        session.add(
            Job(
                id=job_id,
                status=JobStatus.QUEUED.value,
                exercise=exercise,
                results_endpoint=results_endpoint,
                pose_model=pose_model,
                **optional,
            )
        )
        await session.commit()


async def get_job(job_id: str) -> Optional[Job]:
    async with database.session() as session:
        result = await session.execute(select(Job).where(Job.id == job_id))
        return result.scalar_one_or_none()


async def update_job(job_id: str, **fields: Any) -> None:
    """Actualiza los campos indicados y refresca `updated_at`."""
    if not fields:
        return
    fields["updated_at"] = _utcnow()
    async with database.session() as session:
        await session.execute(update(Job).where(Job.id == job_id).values(**fields))
        await session.commit()


async def update_open_job(job_id: str, **fields: Any) -> bool:
    """
    Como `update_job`, pero solo si el job sigue abierto (ni DONE ni FAILED).
    Devuelve si lo actualizó.

    Es como el worker toma y cierra un job. Sin la condición, un worker que
    termina tarde pisaba el cierre de otro: si el barrido de jobs colgados ya
    había cerrado el job como FAILED (y avisado del fallo), el worker lo
    sobrescribía como DONE y la plataforma recibía después un aviso de éxito
    por el mismo job. Lo mismo con dos entregas de la misma tarea. La
    condición y el cambio van en una sola sentencia, así que no hay ventana
    entre comprobar y escribir.
    """
    fields["updated_at"] = _utcnow()
    terminal = [s.value for s in JobStatus.terminal()]
    async with database.session() as session:
        result = await session.execute(
            update(Job)
            .where(Job.id == job_id, Job.status.not_in(terminal))
            .values(**fields)
            .returning(Job.id)
        )
        updated = result.first() is not None
        await session.commit()
    return updated


class StuckJob(NamedTuple):
    """
    Un job cerrado por el barrido.

    Lleva el `results_endpoint` y el `video_url` además del id porque quien
    barre es también quien tiene que avisarle a la plataforma y borrar el
    video, y volver a consultar la fila para obtenerlos abriría una ventana en
    la que el dato pudo cambiar.
    """

    id: str
    results_endpoint: Optional[str]
    video_url: Optional[str] = None


async def fail_stuck_jobs(status: str, cutoff: datetime, error: str) -> list[StuckJob]:
    """
    Marca FAILED los jobs que siguen en `status` desde antes de `cutoff`.

    El estado es un parámetro porque hay DOS formas distintas de quedarse
    colgado y ambas hay que cerrarlas: en PROCESSING el worker tomó el trabajo
    y desapareció a mitad; en QUEUED nunca lo tomó nadie. Cada una tiene su
    propio umbral y su propio código de error (ver app/core/stuck_jobs.py).

    Una sola sentencia UPDATE ... RETURNING, y no leer-y-después-escribir: la
    condición y el cambio viajan juntos, así que dos barridos simultáneos no
    pueden cerrar el mismo job dos veces, ni pisar a un worker que justo
    terminó entre la lectura y la escritura.

    Se compara contra `updated_at` y no contra `created_at`. Para PROCESSING,
    updated_at quedó fijado cuando el worker marcó ese estado, así que la
    espera en cola no cuenta contra el umbral. Para QUEUED quedó fijado al
    encolar, que es exactamente desde cuándo el job está esperando.

    Devuelve los jobs efectivamente cerrados (vacío si no había ninguno).
    """
    async with database.session() as session:
        result = await session.execute(
            update(Job)
            .where(Job.status == status, Job.updated_at < cutoff)
            .values(
                status=JobStatus.FAILED.value,
                error=error,
                # El aviso de fallo queda pendiente en la MISMA escritura que
                # cierra el job, igual que al cerrar uno normalmente: si la
                # tarea de entrega se perdiera, el barrido de avisos perdidos
                # solo puede encontrarla por este estado.
                callback_status=case(
                    (Job.results_endpoint.is_not(None), CallbackStatus.PENDING.value),
                    else_=None,
                ),
                updated_at=_utcnow(),
            )
            .returning(Job.id, Job.results_endpoint, Job.video_url)
        )
        jobs = [StuckJob(row.id, row.results_endpoint, row.video_url) for row in result.all()]
        await session.commit()
    return jobs


# --- Conteos para el resumen de estado (ver app/core/service_status.py) ---
#
# Las tres consultas se apoyan en los índices de `status` y `callback_status`,
# así que cuestan poco aunque la tabla crezca.


async def count_jobs_by_status(since: datetime) -> dict[str, int]:
    """
    Cuántos jobs ENTRARON desde `since`, agrupados por el estado en que están
    ahora. Es la foto de actividad reciente: cuántos videos llegaron y cómo
    terminaron.
    """
    async with database.session() as session:
        result = await session.execute(
            select(Job.status, func.count()).where(Job.created_at >= since).group_by(Job.status)
        )
        return {status: count for status, count in result.all()}


async def count_open_jobs() -> dict[str, int]:
    """
    Backlog actual: jobs sin terminar, sin importar cuándo entraron.

    Es una pregunta DISTINTA de la anterior y por eso va aparte: un job que
    entró hace tres horas y sigue en `queued` no aparece en la ventana
    reciente, pero es exactamente el síntoma de que el worker no da abasto.

    Los estados abiertos se derivan de `JobStatus.terminal()` en vez de
    escribirse a mano, para que agregar un estado nuevo no obligue a acordarse
    de este archivo.
    """
    open_states = [s.value for s in JobStatus if s not in JobStatus.terminal()]
    async with database.session() as session:
        result = await session.execute(
            select(Job.status, func.count())
            .where(Job.status.in_(open_states))
            .group_by(Job.status)
        )
        return {status: count for status, count in result.all()}


async def count_undelivered_callbacks() -> int:
    """
    Resultados calculados y guardados que la plataforma nunca recibió.

    Se cuentan TODOS, no solo los recientes: el número es la deuda pendiente
    de re-entregar, y no baja solo.
    """
    attention = [s.value for s in CallbackStatus.needs_attention()]
    async with database.session() as session:
        result = await session.execute(
            select(func.count()).select_from(Job).where(Job.callback_status.in_(attention))
        )
        return int(result.scalar_one())


# --- Planificación de los avisos (ver app/core/callback_retry.py) ---


async def _move_callbacks(conditions: list, order_by, limit: int, **values: Any) -> list[str]:
    """
    Toma hasta `limit` avisos que cumplen `conditions` y les aplica `values`,
    todo en una sola sentencia. Devuelve los ids tomados.

    TOMAR Y MARCAR VAN JUNTOS a propósito: si fueran un SELECT seguido de un
    UPDATE, dos corridas solapadas leerían la misma fila y la plataforma
    recibiría el mismo aviso dos veces. El propio cambio de estado saca la
    fila del alcance de la consulta, así que hace de reserva.

    Todas las consultas de acá exigen `results_endpoint IS NOT NULL`. No es
    solo defensa: al minimizar por retención se borra el endpoint, así que un
    job cuyo plazo legal venció queda fuera solo.
    """
    candidates = (
        select(Job.id)
        .where(Job.results_endpoint.is_not(None), *conditions)
        .order_by(order_by)
        .limit(limit)
        .scalar_subquery()
    )
    async with database.session() as session:
        result = await session.execute(
            update(Job)
            .where(Job.id.in_(candidates))
            .values(updated_at=_utcnow(), **values)
            .returning(Job.id)
        )
        ids = [row.id for row in result.all()]
        await session.commit()
    return ids


async def claim_due_callbacks(now: datetime, limit: int) -> list[str]:
    """
    Los avisos cuyo próximo intento ya venció, pasados a `pending` para
    encolarlos. Los más atrasados primero.
    """
    return await _move_callbacks(
        [
            Job.callback_status == CallbackStatus.RETRYING.value,
            Job.callback_next_attempt_at <= now,
        ],
        Job.callback_next_attempt_at,
        limit,
        callback_status=CallbackStatus.PENDING.value,
        callback_next_attempt_at=None,
    )


async def requeue_stale_callbacks(idle_before: datetime, now: datetime, limit: int) -> list[str]:
    """
    Los avisos que llevan en `pending` desde antes de `idle_before`: su tarea
    se perdió. Vuelven a `retrying` con el intento para ya, y el despachador
    los toma en su próxima corrida.

    Solo jobs cerrados: uno en curso todavía va a generar su propio aviso.
    """
    return await _move_callbacks(
        [
            Job.callback_status == CallbackStatus.PENDING.value,
            Job.status.in_([s.value for s in JobStatus.terminal()]),
            Job.updated_at < idle_before,
        ],
        Job.updated_at,
        limit,
        callback_status=CallbackStatus.RETRYING.value,
        callback_next_attempt_at=now,
    )


async def count_retrying_callbacks() -> int:
    """Avisos que siguen intentándose. Todavía no son deuda: el sistema los está atendiendo."""
    async with database.session() as session:
        result = await session.execute(
            select(func.count())
            .select_from(Job)
            .where(Job.callback_status == CallbackStatus.RETRYING.value)
        )
        return int(result.scalar_one())


# --- Retención de datos (ver app/core/data_retention.py) ---


class ExpiredJob(NamedTuple):
    """Un job cuyos datos personales ya cumplieron su plazo de conservación."""

    id: str
    video_url: Optional[str]
    callback_status: Optional[str]


async def find_expired_jobs(cutoff: datetime, limit: int) -> list[ExpiredJob]:
    """
    Jobs ya cerrados, anteriores a `cutoff`, que TODAVÍA conservan datos
    personales.

    Esa última condición es la que hace el barrido idempotente sin necesidad de
    una columna extra: una fila ya minimizada no vuelve a aparecer, porque lo
    que la selecciona es justamente tener algo que minimizar.

    Solo se miran jobs en estado final: uno que sigue en curso todavía necesita
    sus datos, por viejo que sea (y si lleva demasiado tiempo abierto, de eso
    se encarga el barrido de jobs colgados, no este).
    """
    terminal = [s.value for s in JobStatus.terminal()]
    async with database.session() as session:
        result = await session.execute(
            select(Job.id, Job.video_url, Job.callback_status)
            .where(
                Job.status.in_(terminal),
                Job.created_at < cutoff,
                or_(
                    Job.video_url.is_not(None),
                    Job.results.is_not(None),
                    Job.results_endpoint.is_not(None),
                ),
            )
            .order_by(Job.created_at)
            .limit(limit)
        )
        return [ExpiredJob(*row) for row in result.all()]


async def anonymize_jobs(job_ids: list[str]) -> int:
    """
    Borra los campos con datos personales y conserva el resto de la fila.

    NO se borra la fila entera a propósito. Lo que identifica a una persona son
    los ángulos (`results`), la ruta del video y el endpoint de la plataforma,
    que suele llevar identificadores del paciente o de la sesión. El id, el
    estado, el ejercicio, el modelo usado y los tiempos no identifican a nadie
    y son lo que permite seguir midiendo el servicio. Minimizar es quitar lo
    que identifica, no destruir el registro operativo.
    """
    if not job_ids:
        return 0

    async with database.session() as session:
        result = await session.execute(
            update(Job)
            .where(Job.id.in_(job_ids))
            .values(
                # `null()` y NO `None`: en una columna JSONB, SQLAlchemy
                # traduce None al valor JSON `null`, que NO es el NULL de SQL.
                # La fila quedaba con el contenido borrado pero `results IS NOT
                # NULL` seguía siendo cierto, así que `find_expired_jobs` la
                # volvía a seleccionar en cada corrida: el barrido no era
                # idempotente y repetía para siempre los borrados en Firebase y
                # la advertencia de resultados nunca recibidos.
                # Detectado verificando contra PostgreSQL real el 2026-09-22.
                results=null(),
                results_endpoint=None,
                video_url=None,
                error=None,
                callback_error=None,
                updated_at=_utcnow(),
            )
        )
        await session.commit()
    return int(result.rowcount)
