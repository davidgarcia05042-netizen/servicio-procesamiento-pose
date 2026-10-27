"""
Validación del video EN MEMORIA, antes de subir nada a Firebase.

Patrón tomado del repo de referencia: si el archivo no sirve, se rechaza sin
haber gastado una operación de almacenamiento externo (allá era S3, acá
Firebase). La diferencia es que acá no basta con confiar en el `content_type`
que manda el cliente -- ese header es trivial de falsificar -- así que además
se revisan los magic bytes del contenedor ya cargado en memoria.

La decodificación es el caso aparte: `cv2.VideoCapture` necesita una RUTA, no
se le puede pasar un buffer. Como aun así conviene atrapar un video ilegible
antes de gastar una subida a Firebase y un job encolado,
`validate_video_is_decodable` escribe el contenido a un archivo temporal, lo
abre, lee un frame y borra el archivo. Es la única validación que toca disco;
las demás son puramente en memoria.
"""

import ipaddress
import os
import tempfile
from typing import Optional
from urllib.parse import urlsplit

from app.core.config import settings
from app.dtos.error_success_code import ErrorAndSuccessCodes
from app.utils.logger import get_logger
from app.utils.video_io import open_video

logger = get_logger("request_validations")

ALLOWED_CONTENT_TYPES = {
    "video/mp4",
    "video/x-msvideo",   # AVI
    "video/quicktime",   # MOV
    "video/x-ms-wmv",    # WMV
    "video/x-matroska",  # MKV
    "video/webm",
}

# Firmas de contenedor de video. (offset, bytes esperados).
_MAGIC_SIGNATURES: tuple[tuple[int, bytes], ...] = (
    (4, b"ftyp"),                                    # MP4 / MOV (ISO BMFF)
    (0, b"RIFF"),                                    # AVI
    (0, b"\x30\x26\xb2\x75\x8e\x66\xcf\x11"),        # WMV / ASF
    (0, b"FLV"),                                     # FLV
    (0, b"\x1a\x45\xdf\xa3"),                        # MKV / WebM (EBML)
)


def validate_content_type(content_type: Optional[str]) -> ErrorAndSuccessCodes:
    """
    Compara solo el tipo, sin parámetros y sin distinguir mayúsculas.

    "video/mp4; codecs=avc1" y "Video/MP4" son el mismo tipo (RFC 9110), y
    rechazarlos por la forma del header dejaría afuera a un cliente legítimo.
    De todas formas este header no es el control: los magic bytes sí lo son.
    """
    media_type = (content_type or "").split(";", 1)[0].strip().lower()
    if media_type not in ALLOWED_CONTENT_TYPES:
        return ErrorAndSuccessCodes.NOT_SUPPORTED_FILE_TYPE
    return ErrorAndSuccessCodes.SUCCESS


def validate_video_bytes(content: bytes) -> ErrorAndSuccessCodes:
    """
    Valida el contenido ya cargado en memoria: que no esté vacío, que no
    exceda el tamaño máximo, y que el encabezado corresponda a algún
    contenedor de video conocido.
    """
    if not content:
        return ErrorAndSuccessCodes.EMPTY_FILE

    if len(content) > settings.max_video_size_bytes:
        return ErrorAndSuccessCodes.FILE_TOO_LARGE

    header = content[:16]
    for offset, signature in _MAGIC_SIGNATURES:
        if header[offset : offset + len(signature)] == signature:
            return ErrorAndSuccessCodes.SUCCESS

    return ErrorAndSuccessCodes.CORRUPT_FILE_HEADER


def validate_video_is_decodable(content: bytes, suffix: str = ".mp4") -> ErrorAndSuccessCodes:
    """
    Confirma que OpenCV puede abrir el video y leer al menos el primer frame.

    Atrapa lo que los magic bytes no ven: un archivo truncado tiene encabezado
    de MP4 perfectamente válido pero no tiene los datos que promete (el clásico
    "moov atom not found"). Sin esta verificación, ese archivo se subiría al
    almacenamiento y recién explotaría en el worker.

    OJO -- ES BLOQUEANTE (escribe a disco y decodifica). Llamarla siempre con
    `asyncio.to_thread` desde código async, igual que los SDK síncronos de
    almacenamiento (Firebase, Cloud Storage): corre en el proceso de la API,
    donde bloquear el event loop frenaría TODAS las solicitudes concurrentes. Esto NO es como el worker, donde bloquear es
    inocuo porque el loop no tiene nada más que hacer.

    El `suffix` importa: OpenCV elige el demuxer en parte por la extensión.
    """
    temp_path: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as temp_file:
            temp_file.write(content)
            temp_path = temp_file.name

        capture = open_video(temp_path)
        try:
            if not capture.isOpened():
                logger.info("Video rechazado: OpenCV no pudo abrirlo")
                return ErrorAndSuccessCodes.UNDECODABLE_VIDEO

            ok, frame = capture.read()
            if not ok or frame is None:
                logger.info("Video rechazado: se abrió pero no tiene un primer frame legible")
                return ErrorAndSuccessCodes.UNDECODABLE_VIDEO
        finally:
            capture.release()

        return ErrorAndSuccessCodes.SUCCESS

    finally:
        # El temporal se borra pase lo que pase: son hasta MAX_VIDEO_SIZE_MB
        # por solicitud y el disco de Render no es grande.
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)


# Contrato v2 (`docs/DECISIONES_TECNICAS.md` de la plataforma): una lista de ternas
# en vez de una sola. El tope acota el cómputo por job y el tamaño del callback.
MAX_TRIPLETS = 12
CONTRACT_V1 = 1
CONTRACT_V2 = 2


def _is_valid_triplet(points: object) -> bool:
    """Tres índices de landmark de MediaPipe distintos, enteros, en 0-32."""
    if not isinstance(points, list) or len(points) != 3:
        return False
    # `bool` se excluye aparte porque en Python es subclase de `int`: sin esto,
    # un JSON `[true, 26, 28]` pasaba como el landmark 1.
    if any(isinstance(p, bool) or not isinstance(p, int) or not 0 <= p <= 32 for p in points):
        return False
    # Con dos puntos iguales no hay ángulo: el vector entre ellos mide cero.
    # Antes se aceptaba, y el job terminaba en `done` con todos los ángulos
    # nulos, que es un resultado vacío presentado como exitoso.
    return len(set(points)) == 3


def contract_version(points: object) -> int:
    """
    v2 si `points` es una lista no vacía de listas (lista de ternas); si no, v1.

    Solo clasifica la FORMA: que sea válida lo decide `validate_points`. Una
    lista mixta (`[24, [26, 28]]`) cae en v1 y v1 la rechaza.
    """
    if isinstance(points, list) and points and all(isinstance(p, list) for p in points):
        return CONTRACT_V2
    return CONTRACT_V1


def as_triplets(points: list) -> list[list[int]]:
    """Normaliza a lista de ternas: v1 `[a, b, c]` -> `[[a, b, c]]`; v2 queda igual."""
    if contract_version(points) == CONTRACT_V2:
        return [list(triplet) for triplet in points]
    return [list(points)]


def validate_points(points: list) -> ErrorAndSuccessCodes:
    """
    Los `points` que envía la plataforma son índices de landmarks de MediaPipe
    (convención de 33 puntos, ver docs/DECISIONES_TECNICAS.md). Un ángulo se define con 3
    (vecino, vértice, vecino) y se mide en el vértice.

    Dos formas aceptadas:
      - v1: una terna, `[24, 26, 28]` (comportamiento original, intacto).
      - v2: lista de 1 a `MAX_TRIPLETS` ternas, `[[23, 25, 27], [24, 26, 28]]`.
        Cada terna cumple las reglas de v1 y no se repiten ternas (la misma
        terna dos veces duplicaría el cómputo sin aportar nada).
    """
    if contract_version(points) == CONTRACT_V2:
        if len(points) > MAX_TRIPLETS:
            return ErrorAndSuccessCodes.INVALID_POINTS
        if not all(_is_valid_triplet(triplet) for triplet in points):
            return ErrorAndSuccessCodes.INVALID_POINTS
        if len({tuple(triplet) for triplet in points}) != len(points):
            return ErrorAndSuccessCodes.INVALID_POINTS
        return ErrorAndSuccessCodes.SUCCESS

    if not _is_valid_triplet(points):
        return ErrorAndSuccessCodes.INVALID_POINTS
    return ErrorAndSuccessCodes.SUCCESS


def endpoint_origin(url: Optional[str]) -> str:
    """
    Solo esquema y host de un endpoint, para escribirlo en logs y errores.

    POR QUÉ NO LA URL COMPLETA: la ruta y los parámetros del `resultsEndpoint`
    suelen llevar identificadores del paciente o de la sesión -- es la misma
    razón por la que el barrido de retención lo borra de la tabla (ver
    app/core/data_retention.py). Escribirlo entero en el log, en cada entrega,
    conservaba ese dato fuera del alcance de la retención. También se quitan
    las credenciales que pudiera traer (`https://usuario:clave@host/`).

    El host basta para diagnosticar: dice a quién se intentó avisar.
    """
    parts = urlsplit(url or "")
    host = parts.netloc.rsplit("@", 1)[-1]
    return f"{parts.scheme}://{host}" if host else "(sin host)"


def rejected_endpoint_reason(results_endpoint: Optional[str]) -> Optional[str]:
    """
    Motivo por el que NO se acepta enviar resultados a esa dirección, o None si
    se acepta.

    QUÉ SE ESTÁ EVITANDO: el `resultsEndpoint` lo elige quien hace la solicitud.
    Sin restricción, este servicio se convierte en un emisor de peticiones
    firmadas hacia donde le digan -- direcciones internas de la red del
    proveedor, el servicio de metadatos de la nube, o un tercero cualquiera al
    que la política de reintentos le mandaría intentos durante horas. Y con
    `HMAC_ENFORCE` en false, que es como está hoy porque la plataforma todavía
    no firma, eso queda al alcance de cualquiera que llegue a la API.

    EL CONTROL REAL ES LA LISTA `CALLBACK_ALLOWED_HOSTS`. Un host que esté en
    ella se acepta tal cual, incluso si es interno: ponerlo ahí es una decisión
    explícita de quien configura el servicio, y es lo que permite apuntar a
    `localhost` para probar. Lo que sigue es defensa en profundidad para
    mientras la lista esté vacía.

    LÍMITE CONOCIDO: con la lista vacía, las comprobaciones miran la dirección
    tal como viene escrita. Un nombre de dominio que resuelva a una dirección
    interna las pasa. Resolver el DNS acá tampoco lo cerraría -- entre la
    validación y el envío, la resolución puede cambiar -- así que el único
    control que de verdad cierra esto es la lista.
    """
    if not results_endpoint:
        return "vacío"

    parts = urlsplit(results_endpoint)

    if parts.scheme not in ("http", "https"):
        return f"esquema no admitido: {parts.scheme or '(ninguno)'}"

    host = (parts.hostname or "").lower()
    if not host:
        return "sin host"

    # Los resultados son datos de salud: en producción no viajan en claro.
    if settings.ENVIRONMENT == "production" and parts.scheme != "https":
        return "en producción se exige https"

    allowed = settings.callback_allowed_hosts
    if allowed:
        return None if host in allowed else f"host fuera de la lista permitida: {host}"

    # Sin lista: se bloquea al menos lo evidente.
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # No es una IP literal. Un host sin punto no existe en internet: es
        # `localhost`, o el nombre interno de otro servicio del proveedor.
        if "." not in host:
            return f"host interno: {host}"
        return None

    if (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    ):
        return f"dirección interna o reservada: {host}"

    return None


def validate_results_endpoint(results_endpoint: str) -> ErrorAndSuccessCodes:
    """
    El motivo concreto va al LOG, no a la respuesta.

    Quien manda la solicitud recibe siempre el mismo código: distinguir
    "malformado" de "host no permitido" le serviría a un atacante para
    averiguar qué direcciones acepta el servicio, y no le sirve de nada a la
    plataforma, que solo necesita saber que su endpoint no se aceptó.
    """
    reason = rejected_endpoint_reason(results_endpoint)
    if reason is None:
        return ErrorAndSuccessCodes.SUCCESS

    logger.warning("resultsEndpoint rechazado (%s): %s", reason, endpoint_origin(results_endpoint))
    return ErrorAndSuccessCodes.INVALID_RESULTS_ENDPOINT
