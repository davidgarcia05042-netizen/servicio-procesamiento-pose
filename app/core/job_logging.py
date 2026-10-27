"""
Registro estructurado del ciclo de vida de cada solicitud.

DÓNDE VIVE EL REGISTRO (la decisión): en la tabla `jobs` de PostgreSQL, no en
un archivo de log aparte. La tabla ya guardaba id, estado, error y timestamps;
lo único que faltaba de la lista era el modelo usado y cuánto tardó el
procesamiento, así que se agregaron como columnas. Abrir un archivo paralelo
habría significado mantener dos registros sincronizados del mismo hecho.

Este módulo NO escribe a la base. Hace dos cosas:

  1. Emite la línea de log estructurada (JSON por línea) para poder seguir el
     flujo en vivo en Render sin abrir una consulta SQL. Es observabilidad
     efímera, no el registro: la fuente de verdad es la tabla.
  2. Da un formato único para esas líneas, para que se puedan filtrar y
     parsear en vez de quedar como texto libre distinto en cada módulo.

La persistencia viaja en el MISMO `update_job` que ya hace el cambio de
estado, así que registrar el modelo y el tiempo no cuesta escrituras extra.

Las solicitudes RECHAZADAS en validación son el único caso que no queda en la
tabla: se rechazan antes de crear el job, justamente para no gastar una fila
ni una subida. Para esas, esta línea de log es el único rastro, y por eso el
`job_id` es opcional.
"""

import json
from typing import Any, Optional

from app.utils.logger import get_logger

logger = get_logger("job_events")


def log_job_event(event: str, job_id: Optional[str] = None, **fields: Any) -> None:
    """
    Emite un evento del ciclo de vida como una línea JSON.

    El timestamp no va en el payload: ya lo pone el formateador del logger
    (ver app/utils/logger.py) y duplicarlo solo agrega ruido.
    """
    payload: dict[str, Any] = {"event": event}
    if job_id is not None:
        payload["job_id"] = job_id
    payload.update({key: value for key, value in fields.items() if value is not None})

    logger.info(json.dumps(payload, default=str))
