"""
Autenticación con la plataforma cliente por firma HMAC.

Reemplaza por completo el login/usuarios/contraseñas del repo de referencia:
acá no hay usuarios finales, solo UNA plataforma cliente que comparte un
secreto con este servicio. La plataforma firma el cuerpo del request y el
servicio verifica la firma.

Esquema esperado:
    X-Timestamp: <epoch en segundos>
    X-Signature: hex(HMAC_SHA256(secreto, "<timestamp>.<cuerpo crudo>"))

El timestamp entra en la firma y se rechaza si está fuera de la ventana
`HMAC_MAX_SKEW_SECONDS`, para que una firma capturada no se pueda reusar
indefinidamente (anti-replay).

NOTA: el esquema exacto (nombres de header, formato del mensaje firmado) está
en la lista de bloqueadores de docs/DECISIONES_TECNICAS.md -- falta acordarlo formalmente con la
plataforma. Esto implementa una convención razonable, no algo ya confirmado.
"""

import hashlib
import hmac
import time

from app.core.config import settings


def compute_signature(timestamp: str, body: bytes, secret: str | None = None) -> str:
    """Firma que debería traer el request. Expuesta también para los tests y el cliente."""
    key = (secret if secret is not None else settings.HMAC_SECRET).encode("utf-8")
    message = timestamp.encode("utf-8") + b"." + body
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def is_valid_signature(timestamp: str, signature: str, body: bytes) -> bool:
    """
    Verifica firma y frescura del timestamp.

    La comparación usa `hmac.compare_digest` (tiempo constante): comparar con
    `==` filtra información por timing y permitiría adivinar la firma byte a
    byte.
    """
    if not settings.HMAC_SECRET or not signature or not timestamp:
        return False

    try:
        sent_at = int(timestamp)
    except ValueError:
        return False

    if abs(time.time() - sent_at) > settings.HMAC_MAX_SKEW_SECONDS:
        return False

    return hmac.compare_digest(compute_signature(timestamp, body), signature)
