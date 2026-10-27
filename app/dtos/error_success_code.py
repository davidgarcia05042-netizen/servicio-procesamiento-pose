"""
Códigos internos de error/éxito (patrón del repo de referencia).

Se devuelven en el campo `internal_status_code` de la respuesta para que la
plataforma cliente pueda distinguir POR QUÉ falló algo, sin depender solo del
código HTTP.
"""

from enum import Enum


class ErrorAndSuccessCodes(str, Enum):
    SUCCESS = "success"

    # Validación del video (todo esto se detecta EN MEMORIA, antes de subir
    # nada a Firebase -- ver app/utils/request_validations.py)
    NOT_SUPPORTED_FILE_TYPE = "not_supported_file_type"
    FILE_TOO_LARGE = "file_too_large"
    EMPTY_FILE = "empty_file"
    CORRUPT_FILE_HEADER = "corrupt_file_header"
    # El encabezado es de un contenedor conocido, pero el stream no se puede
    # decodificar (ej. archivo truncado: el contenedor promete frames que no
    # están). Se distingue de CORRUPT_FILE_HEADER porque acá el encabezado sí
    # era válido y el problema aparece recién al intentar leer un frame.
    UNDECODABLE_VIDEO = "undecodable_video"

    # Validación del request
    INVALID_POINTS = "invalid_points"
    INVALID_RESULTS_ENDPOINT = "invalid_results_endpoint"

    # Fallas de infraestructura / procesamiento
    PROCESSING_ERROR = "processing_error"
    # El job quedó en PROCESSING más allá del umbral y lo cerró el barrido
    # periódico (ver app/core/stuck_jobs.py). Se distingue de PROCESSING_ERROR
    # porque acá nadie llegó a reportar un error: el worker desapareció, y lo
    # único que se sabe es que el tiempo se agotó.
    PROCESSING_TIMEOUT = "processing_timeout"
    # El job nunca llegó a empezar: se quedó en QUEUED más allá del umbral.
    # Es un fallo DISTINTO de PROCESSING_TIMEOUT y por eso lleva código propio:
    # allá el worker tomó el trabajo y desapareció a mitad; acá el worker nunca
    # lo tomó, típicamente porque la tarea encolada se perdió (un reinicio del
    # broker vacía la cola) o porque no quedó ningún worker vivo.
    QUEUED_TIMEOUT = "queued_timeout"
