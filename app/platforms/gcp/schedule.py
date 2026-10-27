"""
Las tareas periódicas en Google Cloud: Cloud Scheduler llama a un endpoint
del worker para cada una (POST /cron/<nombre>, ver worker.py).

Son las mismas cinco que Celery Beat dispara en Render, con los mismos nombres
y la misma frecuencia (ver `beat_schedule` en app/platforms/render/worker.py).
Un test compara las dos tablas: agregar una tarea periódica en una plataforma
y olvidarla en la otra rompe la suite.

LA ÚNICA DIFERENCIA: Cloud Scheduler no baja de un minuto. El despachador de
reintentos de avisos, que en Render corre cada CALLBACK_DISPATCH_INTERVAL_SECONDS
(10 s), acá corre cada minuto. Los dos primeros reintentos (a los 5 y a los
30 s) se hacen entonces hasta un minuto después de su hora. Se aceptó así: la
alternativa, un despachador que se quede despierto el minuto entero, es pagar
una instancia siempre encendida, que es justo lo que se quiere evitar en
Google. Y para la plataforma, recibir un resultado un minuto más tarde después
de una caída breve no cambia nada.

Cloud Scheduler firma cada llamada con el mismo token OIDC que Cloud Tasks (la
cuenta TASKS_SERVICE_ACCOUNT, para WORKER_URL), así que el worker las verifica
igual.

Los trabajos de Cloud Scheduler se crean con los comandos que genera
`python -m app.tools.gcp_scheduler` a partir de esta tabla (ver README).
"""

from dataclasses import dataclass

from app.core.config import settings

# Cloud Scheduler no programa más seguido que esto.
MIN_INTERVAL_SECONDS = 60

# La retención corre una vez al día a esta hora (de Bogotá; ver
# `time_zone` en la herramienta de comandos), cuando no hay tráfico.
DAILY_HOUR = 3


@dataclass(frozen=True)
class ScheduledJob:
    # El mismo nombre que la entrada del beat_schedule de Render.
    name: str
    # La función de la parte común, "módulo:función".
    target: str
    every_seconds: int
    # Cuánto espera Cloud Scheduler la respuesta (entre 15 s y 30 min).
    attempt_deadline_seconds: int

    @property
    def path(self) -> str:
        return f"/cron/{self.name}"

    @property
    def cron(self) -> str:
        return cron_for(self.every_seconds)


def cron_for(every_seconds: int) -> str:
    """
    La expresión cron de "cada `every_seconds`". Solo admite frecuencias que
    cron puede expresar sin derivar: minutos que dividen la hora, horas que
    dividen el día, o una vez al día.
    """
    if every_seconds % 60:
        raise ValueError(f"Cloud Scheduler programa en minutos enteros, no cada {every_seconds} s")
    minutes = every_seconds // 60
    if minutes < 60 and 60 % minutes == 0:
        return "* * * * *" if minutes == 1 else f"*/{minutes} * * * *"
    if minutes % 60 == 0:
        hours = minutes // 60
        if hours == 24:
            return f"0 {DAILY_HOUR} * * *"
        if 24 % hours == 0:
            return f"0 */{hours} * * *"
    raise ValueError(f"Cada {every_seconds} s no se puede expresar como cron sin derivar")


def _at_least_a_minute(seconds: int) -> int:
    """Lo más cerca posible de `seconds` que Cloud Scheduler puede programar."""
    minutes = max(1, -(-seconds // 60))
    return minutes * 60


def scheduled_jobs() -> list[ScheduledJob]:
    """Las tareas periódicas, con la frecuencia que dice la configuración."""
    sweep_every = settings.STUCK_JOB_SCAN_INTERVAL_MINUTES * 60
    return [
        ScheduledJob(
            "sweep-stuck-jobs",
            "app.core.stuck_jobs:sweep_stuck_jobs",
            sweep_every,
            attempt_deadline_seconds=180,
        ),
        ScheduledJob(
            "report-service-status",
            "app.core.service_status:report_service_status",
            settings.STATUS_REPORT_INTERVAL_MINUTES * 60,
            attempt_deadline_seconds=180,
        ),
        # Borra hasta RETENTION_BATCH_SIZE videos del almacenamiento en una
        # corrida: le toca el plazo más largo.
        ScheduledJob(
            "purge-expired-data",
            "app.core.data_retention:purge_expired_data",
            settings.RETENTION_SCAN_INTERVAL_HOURS * 3600,
            attempt_deadline_seconds=900,
        ),
        ScheduledJob(
            "dispatch-due-callbacks",
            "app.core.callback_retry:dispatch_due_callbacks",
            _at_least_a_minute(settings.CALLBACK_DISPATCH_INTERVAL_SECONDS),
            attempt_deadline_seconds=60,
        ),
        ScheduledJob(
            "recover-lost-callbacks",
            "app.core.callback_retry:recover_lost_callbacks",
            sweep_every,
            attempt_deadline_seconds=180,
        ),
    ]


def find(name: str) -> ScheduledJob | None:
    return next((job for job in scheduled_jobs() if job.name == name), None)
