"""
Estados del ciclo de vida de un trabajo de procesamiento.

Adaptado del patrón del repo de referencia (Saved/Processing/Processed/Failed)
a los nombres que ya usaba `JobStatus` en el entorno experimental:

    referencia   ->  acá
    SAVED        ->  QUEUED      (registro creado, video aún no procesándose)
    PROCESSING   ->  PROCESSING  (worker de Celery trabajando sobre el video)
    PROCESSED    ->  DONE        (ángulos calculados y callback enviado)
    FAILED       ->  FAILED      (cualquier error en el camino)

El valor en minúsculas es el que se persiste en la columna `status` de
PostgreSQL y el que se expone hacia afuera.
"""

from enum import Enum


class JobStatus(str, Enum):
    QUEUED = "queued"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"

    @classmethod
    def terminal(cls) -> set["JobStatus"]:
        """Estados finales: un job en uno de estos ya no va a cambiar solo."""
        return {cls.DONE, cls.FAILED}


class CallbackStatus(str, Enum):
    """
    Cómo terminó el aviso a la plataforma. Es una dimensión APARTE de
    `JobStatus`: un job puede haber quedado `done` (los ángulos se calcularon
    y están guardados) y aun así tener su callback en `exhausted` porque la
    plataforma estuvo caída seis horas. Mezclar ambas cosas en una sola
    columna haría que el estado del procesamiento mintiera sobre sí mismo.
    """

    PENDING = "pending"      # encolado para un intento inmediato
    RETRYING = "retrying"    # esperando su próximo intento (callback_next_attempt_at)
    DELIVERED = "delivered"  # la plataforma respondió 2xx
    REJECTED = "rejected"    # respondió algo no reintentable (típicamente 4xx)
    EXHAUSTED = "exhausted"  # se agotó la ventana de insistencia sin entregarse

    @classmethod
    def final(cls) -> set["CallbackStatus"]:
        """Desenlaces: un aviso en uno de estos ya no se va a volver a intentar solo."""
        return {cls.DELIVERED, cls.REJECTED, cls.EXHAUSTED}

    @classmethod
    def needs_attention(cls) -> set["CallbackStatus"]:
        """
        Los que requieren que alguien mire: el resultado existe y está
        guardado, pero la plataforma nunca lo recibió.
        """
        return {cls.REJECTED, cls.EXHAUSTED}
