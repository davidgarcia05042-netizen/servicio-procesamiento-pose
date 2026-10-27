"""
Logging centralizado (patrón del repo de referencia).

Va todo a stdout, que es de donde Render recoge el log. El nivel sale de
`LOG_LEVEL` (ver app/core/config.py): en producción el log es lo único que se
ve desde afuera, y bajarlo a DEBUG para diagnosticar un incidente no debería
requerir un despliegue con cambio de código.
"""

import logging
import sys

from app.core.config import settings

_CONFIGURED = False


def _configure_once() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)-8s | %(name)s | %(message)s")
    )
    root = logging.getLogger()
    root.setLevel(settings.LOG_LEVEL)
    root.addHandler(handler)
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    _configure_once()
    return logging.getLogger(name)
