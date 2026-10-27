"""
Prueba la orquestación de `process_video()` (app/core/video_processing_service.py),
el paso 1-5 documentado en su docstring: validar -> validar bytes en memoria ->
crear el job con la ruta del video -> subir -> encolar.

No toca PostgreSQL, el almacenamiento ni la cola reales: se mockean
`insert_job`, `update_job`, `new_video_path` y `upload_video` en el namespace
del módulo bajo prueba (se importan con `from ... import ...`, así que hay que
parchear ahí, no en su origen), y se inyecta un `TaskQueue` falso en vez del
real (`process_video` está diseñado para eso, ver su parámetro `queue`).
"""

from unittest.mock import AsyncMock, patch

from app.constants.job_constants import JobStatus
from app.core import video_processing_service as svc
from app.core.config import settings
from app.dtos.error_success_code import ErrorAndSuccessCodes

# Header MP4 válido para `validate_video_bytes`: "ftyp" en el offset 4.
VALID_MP4_HEADER = b"\x00\x00\x00\x20ftypmp42" + b"\x00" * 8
VALID_POINTS = [24, 26, 28]  # cadera, rodilla, tobillo derechos (convención MediaPipe)
VALID_ENDPOINT = "https://platform.example.com/results"
PATH = "videos/abc.mp4"


class FakeUploadFile:
    """Sustituto mínimo de fastapi.UploadFile: solo lo que usa process_video."""

    def __init__(self, content: bytes, filename: str = "video.mp4", content_type: str = "video/mp4"):
        self._content = content
        self.filename = filename
        self.content_type = content_type

    async def read(self) -> bytes:
        return self._content


class FakeQueue:
    """Sustituto de TaskQueue: registra los job_id encolados sin tocar Celery."""

    def __init__(self) -> None:
        self.queued_job_ids: list[str] = []

    def process_video(self, job_id: str) -> None:
        self.queued_job_ids.append(job_id)


async def test_process_video_happy_path_queues_job(real_video_bytes):
    queue = FakeQueue()
    video = FakeUploadFile(real_video_bytes)

    with (
        patch.object(svc, "new_video_path", return_value=PATH),
        patch.object(svc, "insert_job", new=AsyncMock()) as mock_insert,
        patch.object(svc, "update_job", new=AsyncMock()) as mock_update,
        patch.object(svc, "upload_video", new=AsyncMock()) as mock_upload,
    ):
        result = await svc.process_video(
            video_file=video,
            points=VALID_POINTS,
            exercise="knee_flexion",
            results_endpoint=VALID_ENDPOINT,
            queue=queue,
        )

    assert result["status"] == ErrorAndSuccessCodes.SUCCESS
    job_id = result["job_id"]
    assert job_id is not None

    # El modelo se guarda POR FILA: la config global cambia con el tiempo y sin
    # esto un job viejo quedaría atribuido al modelo que esté configurado hoy.
    # La ruta del video y los puntos van en la MISMA escritura que crea el job.
    mock_insert.assert_awaited_once_with(
        job_id=job_id,
        exercise="knee_flexion",
        results_endpoint=VALID_ENDPOINT,
        pose_model=settings.POSE_MODEL,
        video_url=PATH,
        results={"requested_points": VALID_POINTS},
    )
    mock_upload.assert_awaited_once_with(real_video_bytes, PATH, "video/mp4")
    mock_update.assert_not_awaited()
    assert queue.queued_job_ids == [job_id]


async def test_queueing_does_not_block_the_api_event_loop(real_video_bytes):
    """
    Encolar es E/S de red bloqueante. Con el broker caído Celery reintenta
    durante segundos: en el hilo del event loop, eso congelaría todas las
    solicitudes de la API, no solo esta.
    """
    import threading

    class ThreadRecordingQueue:
        def process_video(self, job_id: str) -> None:
            self.thread = threading.current_thread()

    queue = ThreadRecordingQueue()

    with (
        patch.object(svc, "insert_job", new=AsyncMock()),
        patch.object(svc, "update_job", new=AsyncMock()),
        patch.object(svc, "upload_video", new=AsyncMock()),
    ):
        result = await svc.process_video(
            video_file=FakeUploadFile(real_video_bytes),
            points=VALID_POINTS,
            exercise="knee_flexion",
            results_endpoint=VALID_ENDPOINT,
            queue=queue,
        )

    assert result["status"] == ErrorAndSuccessCodes.SUCCESS
    assert queue.thread is not threading.current_thread()


async def test_the_job_names_its_video_before_the_video_is_stored(real_video_bytes):
    """
    Si el proceso muere entre las dos escrituras, no puede quedar un video
    guardado sin una fila que lo nombre: nada lo encontraría para borrarlo.
    """
    order: list[str] = []

    async def insert(**kwargs):
        order.append(f"job:{kwargs['video_url']}")

    async def upload(content, path, content_type):
        order.append(f"video:{path}")

    with (
        patch.object(svc, "new_video_path", return_value=PATH),
        patch.object(svc, "insert_job", new=insert),
        patch.object(svc, "upload_video", new=upload),
    ):
        await svc.process_video(
            video_file=FakeUploadFile(real_video_bytes),
            points=VALID_POINTS,
            exercise="knee_flexion",
            results_endpoint=VALID_ENDPOINT,
            queue=FakeQueue(),
        )

    assert order == [f"job:{PATH}", f"video:{PATH}"]


async def test_the_path_comes_from_the_client_filename_only_through_the_port(real_video_bytes):
    """La ruta la arma `new_video_path`, que solo conserva la extensión."""
    with (
        patch.object(svc, "insert_job", new=AsyncMock()) as mock_insert,
        patch.object(svc, "upload_video", new=AsyncMock()),
    ):
        await svc.process_video(
            video_file=FakeUploadFile(real_video_bytes, filename="marcha_ana_perez.mp4"),
            points=VALID_POINTS,
            exercise="knee_flexion",
            results_endpoint=VALID_ENDPOINT,
            queue=FakeQueue(),
        )

    assert "ana" not in mock_insert.call_args.kwargs["video_url"]


async def test_process_video_rejects_invalid_points_without_touching_storage():
    """Rechazo en validación de request: no debe crear job ni subir nada (ver docstring del módulo)."""
    queue = FakeQueue()
    video = FakeUploadFile(VALID_MP4_HEADER)

    with (
        patch.object(svc, "insert_job", new=AsyncMock()) as mock_insert,
        patch.object(svc, "upload_video", new=AsyncMock()) as mock_upload,
    ):
        result = await svc.process_video(
            video_file=video,
            points=[1, 2],  # se necesitan exactamente 3 (proximal, vértice, distal)
            exercise="knee_flexion",
            results_endpoint=VALID_ENDPOINT,
            queue=queue,
        )

    assert result == {"status": ErrorAndSuccessCodes.INVALID_POINTS}
    mock_insert.assert_not_awaited()
    mock_upload.assert_not_awaited()
    assert queue.queued_job_ids == []


async def test_process_video_rejects_corrupt_video_header_without_touching_storage():
    """Rechazo en validación de bytes (paso 2, después de validar el request): tampoco toca Firebase."""
    queue = FakeQueue()
    video = FakeUploadFile(b"esto no es un video")

    with (
        patch.object(svc, "insert_job", new=AsyncMock()) as mock_insert,
        patch.object(svc, "upload_video", new=AsyncMock()) as mock_upload,
    ):
        result = await svc.process_video(
            video_file=video,
            points=VALID_POINTS,
            exercise="knee_flexion",
            results_endpoint=VALID_ENDPOINT,
            queue=queue,
        )

    assert result == {"status": ErrorAndSuccessCodes.CORRUPT_FILE_HEADER}
    mock_insert.assert_not_awaited()
    mock_upload.assert_not_awaited()
    assert queue.queued_job_ids == []


async def test_process_video_rejects_undecodable_video_without_touching_storage():
    """
    Encabezado válido pero stream ilegible: lo atrapa la verificación con
    OpenCV, no los magic bytes. Debe cortarse antes de crear el job y antes de
    gastar la subida a Firebase.
    """
    queue = FakeQueue()
    video = FakeUploadFile(VALID_MP4_HEADER)  # pasa el magic byte, no se puede decodificar

    with (
        patch.object(svc, "insert_job", new=AsyncMock()) as mock_insert,
        patch.object(svc, "upload_video", new=AsyncMock()) as mock_upload,
    ):
        result = await svc.process_video(
            video_file=video,
            points=VALID_POINTS,
            exercise="knee_flexion",
            results_endpoint=VALID_ENDPOINT,
            queue=queue,
        )

    assert result == {"status": ErrorAndSuccessCodes.UNDECODABLE_VIDEO}
    mock_insert.assert_not_awaited()
    mock_upload.assert_not_awaited()
    assert queue.queued_job_ids == []


async def test_process_video_marks_job_failed_when_storage_upload_raises(real_video_bytes):
    """El job ya existe en DB (insert_job corrió) cuando falla la subida: debe marcarse FAILED, no perderse."""
    queue = FakeQueue()
    video = FakeUploadFile(real_video_bytes)

    with (
        patch.object(svc, "insert_job", new=AsyncMock()) as mock_insert,
        patch.object(svc, "update_job", new=AsyncMock()) as mock_update,
        patch.object(svc, "upload_video", new=AsyncMock(side_effect=RuntimeError("firebase down"))),
    ):
        result = await svc.process_video(
            video_file=video,
            points=VALID_POINTS,
            exercise="knee_flexion",
            results_endpoint=VALID_ENDPOINT,
            queue=queue,
        )

    assert result == {"status": ErrorAndSuccessCodes.PROCESSING_ERROR}
    mock_insert.assert_awaited_once()
    mock_update.assert_awaited_once()
    assert mock_update.call_args.kwargs == {
        "status": JobStatus.FAILED.value,
        "error": "error en la recepción del video",
    }
    assert queue.queued_job_ids == []


class BrokenQueue:
    """El broker no responde al encolar."""

    def process_video(self, job_id: str) -> None:
        raise ConnectionError("redis caído")


async def test_a_video_uploaded_for_a_job_that_never_got_queued_is_discarded(real_video_bytes):
    """
    El video ya se subió y el encolado falla: el job queda FAILED y nadie va a
    procesar ese video. Antes se quedaba en el almacenamiento hasta que la
    retención lo borrara, 30 días después, sin ninguna finalidad (Ley 1581).
    """
    with (
        patch.object(svc, "insert_job", new=AsyncMock()),
        patch.object(svc, "update_job", new=AsyncMock()),
        patch.object(svc, "new_video_path", return_value=PATH),
        patch.object(svc, "upload_video", new=AsyncMock()),
        patch.object(svc, "delete_video", new=AsyncMock()) as delete,
    ):
        result = await svc.process_video(
            video_file=FakeUploadFile(real_video_bytes),
            points=VALID_POINTS,
            exercise="knee_flexion",
            results_endpoint=VALID_ENDPOINT,
            queue=BrokenQueue(),
        )

    assert result == {"status": ErrorAndSuccessCodes.PROCESSING_ERROR}
    delete.assert_awaited_once_with(PATH)


async def test_the_video_is_discarded_even_if_the_database_is_also_down(real_video_bytes):
    """Si la base tampoco responde, el update revienta; el borrado no puede depender de eso."""
    with (
        patch.object(svc, "insert_job", new=AsyncMock()),
        patch.object(svc, "update_job", new=AsyncMock(side_effect=ConnectionError("db caída"))),
        patch.object(svc, "new_video_path", return_value=PATH),
        patch.object(svc, "upload_video", new=AsyncMock()),
        patch.object(svc, "delete_video", new=AsyncMock()) as delete,
    ):
        try:
            await svc.process_video(
                video_file=FakeUploadFile(real_video_bytes),
                points=VALID_POINTS,
                exercise="knee_flexion",
                results_endpoint=VALID_ENDPOINT,
                queue=BrokenQueue(),
            )
        except ConnectionError:
            pass

    delete.assert_awaited_once_with(PATH)


async def test_a_failed_upload_is_still_deleted(real_video_bytes):
    """
    Una subida puede fallar de este lado (se cortó la respuesta, venció el
    plazo) después de que el almacenamiento ya guardó el video. Borrar algo
    que no está es inocuo; dejar guardado un video sin finalidad, no.
    """
    with (
        patch.object(svc, "new_video_path", return_value=PATH),
        patch.object(svc, "insert_job", new=AsyncMock()),
        patch.object(svc, "update_job", new=AsyncMock()),
        patch.object(svc, "upload_video", new=AsyncMock(side_effect=TimeoutError("sin respuesta"))),
        patch.object(svc, "delete_video", new=AsyncMock()) as delete,
    ):
        await svc.process_video(
            video_file=FakeUploadFile(real_video_bytes),
            points=VALID_POINTS,
            exercise="knee_flexion",
            results_endpoint=VALID_ENDPOINT,
            queue=FakeQueue(),
        )

    delete.assert_awaited_once_with(PATH)


async def test_nothing_is_deleted_when_the_job_could_not_even_be_created(real_video_bytes):
    """El discriminante: sin job no se intentó subir nada."""
    with (
        patch.object(svc, "insert_job", new=AsyncMock(side_effect=ConnectionError("db caída"))),
        patch.object(svc, "update_job", new=AsyncMock()),
        patch.object(svc, "upload_video", new=AsyncMock()) as upload,
        patch.object(svc, "delete_video", new=AsyncMock()) as delete,
    ):
        await svc.process_video(
            video_file=FakeUploadFile(real_video_bytes),
            points=VALID_POINTS,
            exercise="knee_flexion",
            results_endpoint=VALID_ENDPOINT,
            queue=FakeQueue(),
        )

    upload.assert_not_awaited()
    delete.assert_not_awaited()
