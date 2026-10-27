"""
Contrato v2 con la plataforma (docs/DECISIONES_TECNICAS.md de la plataforma): una solicitud con una
LISTA de ternas recibe un solo job, una pasada de MediaPipe y el callback v2.

Lo que se protege, además de la forma nueva:
  - v1 sigue intacto: una terna sola produce el JSONB, el análisis y el
    callback de siempre (las pruebas de v1 no se tocaron).
  - El ángulo 2D de v2 es el MISMO `_angle_deg` sobre píxeles que v1, sin
    tocar la fórmula validada; el 3D es una función aparte.
  - La retención (Ley 1581) alcanza las claves nuevas del JSONB.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import numpy as np
import pytest
from sqlalchemy.dialects import postgresql

from app.constants.job_constants import JobStatus
from app.core import callback_delivery, pose_analysis
from app.core import video_processing_service as svc
from app.core.callback_queue import payload_from_job
from app.core.config import settings
from app.dtos.error_success_code import ErrorAndSuccessCodes
from app.utils.hmac_auth import compute_signature
from app.utils.request_validations import (
    CONTRACT_V1,
    CONTRACT_V2,
    MAX_TRIPLETS,
    as_triplets,
    contract_version,
    validate_points,
)
from tests.fakes import FakeSession
from tests.test_video_processing_service import (
    PATH,
    VALID_ENDPOINT,
    FakeQueue,
    FakeUploadFile,
)

OK = ErrorAndSuccessCodes.SUCCESS
INVALID = ErrorAndSuccessCodes.INVALID_POINTS
TRIPLETS = [[11, 13, 15], [12, 14, 16]]


# --- 1. Validación y normalización de `points` ---


def test_v2_accepts_a_list_of_valid_triplets():
    assert validate_points([[24, 26, 28]]) == OK
    assert validate_points(TRIPLETS) == OK
    many = [[a, a + 1, a + 2] for a in range(MAX_TRIPLETS)]
    assert validate_points(many) == OK


def test_v2_rejects_too_many_or_repeated_triplets():
    too_many = [[a, a + 1, a + 2] for a in range(MAX_TRIPLETS + 1)]
    assert validate_points(too_many) == INVALID
    assert validate_points([[24, 26, 28], [24, 26, 28]]) == INVALID


@pytest.mark.parametrize(
    "bad_triplet",
    [[24, 26], [24, 26, 28, 30], [26, 26, 28], [True, 26, 28], [24, 26, 99], [24.0, 26, 28], ["24", 26, 28], []],
)
def test_each_v2_triplet_follows_the_v1_rules(bad_triplet):
    assert validate_points([[11, 13, 15], bad_triplet]) == INVALID


def test_mixed_or_empty_shapes_are_rejected():
    assert validate_points([]) == INVALID
    assert validate_points([24, [26, 28, 30]]) == INVALID
    assert validate_points([[24, 26, 28], 30]) == INVALID


def test_contract_version_and_normalization():
    assert contract_version([24, 26, 28]) == CONTRACT_V1
    assert contract_version([[24, 26, 28]]) == CONTRACT_V2
    assert contract_version([]) == CONTRACT_V1
    assert as_triplets([24, 26, 28]) == [[24, 26, 28]]
    assert as_triplets(TRIPLETS) == TRIPLETS


# --- 2. Recepción: el JSONB marca la versión solo en v2 ---


async def test_a_v2_request_is_stored_as_triplets_with_its_version(real_video_bytes):
    queue = FakeQueue()
    with (
        patch.object(svc, "new_video_path", return_value=PATH),
        patch.object(svc, "insert_job", new=AsyncMock()) as mock_insert,
        patch.object(svc, "update_job", new=AsyncMock()),
        patch.object(svc, "upload_video", new=AsyncMock()),
    ):
        result = await svc.process_video(
            video_file=FakeUploadFile(real_video_bytes),
            points=TRIPLETS,
            exercise="exercise:12",
            results_endpoint=VALID_ENDPOINT,
            queue=queue,
        )

    assert result["status"] == OK
    stored = mock_insert.await_args.kwargs["results"]
    assert stored == {"requested_points": TRIPLETS, "contract_version": CONTRACT_V2}
    assert queue.queued_job_ids == [result["job_id"]]


async def test_an_invalid_v2_request_is_rejected_before_storing(real_video_bytes):
    with patch.object(svc, "insert_job", new=AsyncMock()) as mock_insert:
        result = await svc.process_video(
            video_file=FakeUploadFile(real_video_bytes),
            points=[[11, 13, 15], [11, 13, 15]],
            exercise="exercise:12",
            results_endpoint=VALID_ENDPOINT,
            queue=FakeQueue(),
        )
    assert result["status"] == INVALID
    mock_insert.assert_not_awaited()


# --- 3. Análisis v2 con MediaPipe simulado ---

WIDTH, HEIGHT = 200, 100


def _landmarks(overrides):
    """33 landmarks distintos entre sí, con los índices de `overrides` fijados."""
    points = [
        SimpleNamespace(x=0.02 * i + 0.01, y=0.015 * i + 0.02, z=-0.01 * i, visibility=0.9)
        for i in range(33)
    ]
    for index, (x, y) in overrides.items():
        points[index] = SimpleNamespace(x=x, y=y, z=-0.1, visibility=0.95)
    return SimpleNamespace(landmark=points)


def _world(overrides):
    points = [SimpleNamespace(x=0.01 * i, y=-0.02 * i, z=0.005 * i) for i in range(33)]
    for index, (x, y, z) in overrides.items():
        points[index] = SimpleNamespace(x=x, y=y, z=z)
    return SimpleNamespace(landmark=points)


# Codo izquierdo (13) entre hombro (11) y muñeca (15). En píxeles (200x100)
# da 116.57°; con coordenadas normalizadas daría 135°: así la prueba distingue
# que el 2D se mide sobre píxeles, como en v1.
POSE_2D = _landmarks({11: (0.5, 0.2), 13: (0.5, 0.5), 15: (0.6, 0.6)})
POSE_3D = _world({11: (0.0, 0.3, 0.0), 13: (0.0, 0.0, 0.0), 15: (0.3, 0.0, 0.1)})

FRAME_RESULTS = [
    SimpleNamespace(pose_landmarks=POSE_2D, pose_world_landmarks=POSE_3D),  # con pose
    SimpleNamespace(pose_landmarks=None, pose_world_landmarks=None),        # sin pose
    SimpleNamespace(pose_landmarks=POSE_2D, pose_world_landmarks=None),     # sin 3D
]


class _FakeCapture:
    def __init__(self, count):
        self._left = count

    def isOpened(self):  # noqa: N802 -- API de OpenCV
        return True

    def get(self, _prop):
        return 30.0

    def read(self):
        if self._left == 0:
            return False, None
        self._left -= 1
        return True, np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint8)

    def release(self):
        pass


class _FakePose:
    def __init__(self, *args, **kwargs):
        self._results = iter(FRAME_RESULTS)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def process(self, _image):
        return next(self._results)


@pytest.fixture
def simulated_mediapipe():
    with (
        patch.object(pose_analysis, "open_video", side_effect=lambda _p: _FakeCapture(len(FRAME_RESULTS))),
        patch.object(pose_analysis.mp.solutions.pose, "Pose", _FakePose),
    ):
        yield


def test_v2_output_shape(simulated_mediapipe):
    out = pose_analysis.analyze_video_v2("video.mp4", TRIPLETS)

    assert set(out) == {"fps", "duration_s", "joints", "frames"}
    assert out["fps"] == 30.0
    assert out["duration_s"] == round(3 / 30.0, 3)
    assert [j["points"] for j in out["joints"]] == TRIPLETS
    assert [j["vertex"] for j in out["joints"]] == [13, 14]

    with_pose, without_pose, without_world = out["frames"]
    assert [f["t"] for f in out["frames"]] == [0.0, 0.0333, 0.0667]

    assert len(with_pose["landmarks"]) == 33
    assert all(len(p) == 4 for p in with_pose["landmarks"])
    assert with_pose["landmarks"][13] == [0.5, 0.5, -0.1, 0.95]
    assert len(with_pose["world_landmarks"]) == 33
    assert all(len(p) == 3 for p in with_pose["world_landmarks"])
    assert len(with_pose["angles_2d"]) == len(with_pose["angles_3d"]) == len(TRIPLETS)

    assert without_pose == {
        "t": 0.0333, "landmarks": None, "world_landmarks": None,
        "angles_2d": [None, None], "angles_3d": [None, None],
    }
    assert without_world["world_landmarks"] is None
    assert without_world["angles_3d"] == [None, None]
    assert without_world["angles_2d"] == with_pose["angles_2d"]


def test_v2_2d_angle_is_the_validated_pixel_angle(simulated_mediapipe):
    out = pose_analysis.analyze_video_v2("video.mp4", TRIPLETS)

    expected = pose_analysis._angle_deg((100.0, 20.0), (100.0, 50.0), (120.0, 60.0))
    assert round(expected, 2) == 116.57
    assert out["frames"][0]["angles_2d"][0] == 116.57

    elbow = out["joints"][0]
    assert elbow["max_angle"] == elbow["min_angle"] == 116.57
    assert elbow["valid_frames"] == 2  # el frame sin pose no cuenta


def test_v2_3d_angle_uses_world_landmarks(simulated_mediapipe):
    out = pose_analysis.analyze_video_v2("video.mp4", TRIPLETS)
    expected = pose_analysis._angle_deg_3d((0.0, 0.3, 0.0), (0.0, 0.0, 0.0), (0.3, 0.0, 0.1))
    assert out["frames"][0]["angles_3d"][0] == round(expected, 2)
    # Max/min salen del 2D (medición clínica), no del 3D.
    assert out["joints"][0]["max_angle"] != out["frames"][0]["angles_3d"][0]


def test_v1_output_is_unchanged_and_agrees_with_v2(simulated_mediapipe):
    v1 = pose_analysis.analyze_video("video.mp4", [11, 13, 15])
    v2 = pose_analysis.analyze_video_v2("video.mp4", [[11, 13, 15]])

    assert set(v1) == {"points", "max_angle", "min_angle"}
    assert set(v1["points"][0]) == {"frame", "timestamp_ms", "landmarks", "angle"}
    assert [f["angle"] for f in v1["points"]] == [f["angles_2d"][0] for f in v2["frames"]]
    assert (v1["max_angle"], v1["min_angle"]) == (
        v2["joints"][0]["max_angle"], v2["joints"][0]["min_angle"],
    )


def test_angle_3d_degenerate_vector_is_nan():
    import math

    assert math.isnan(pose_analysis._angle_deg_3d((0, 0, 0), (0, 0, 0), (1, 0, 0)))
    assert round(pose_analysis._angle_deg_3d((1, 0, 0), (0, 0, 0), (0, 0, 1)), 2) == 90.0


# --- 4. Worker: el job v2 guarda el análisis v2 y deja max/min en NULL ---

V2_ANALYSIS = {
    "fps": 30.0,
    "duration_s": 0.1,
    "joints": [
        {"points": [11, 13, 15], "vertex": 13, "max_angle": 120.0, "min_angle": 40.0, "valid_frames": 3},
        {"points": [12, 14, 16], "vertex": 14, "max_angle": 110.0, "min_angle": 30.0, "valid_frames": 3},
    ],
    "frames": [{"t": 0.0, "landmarks": None, "world_landmarks": None,
                "angles_2d": [None, None], "angles_3d": [None, None]}],
}


async def test_the_worker_runs_one_v2_pass_for_a_v2_job():
    from app.core import video_job

    job = SimpleNamespace(
        id="job-2", status=JobStatus.QUEUED.value, video_url="videos/x.mp4",
        results_endpoint="https://platform.example.com/r",
        results={"requested_points": TRIPLETS, "contract_version": CONTRACT_V2},
    )
    with (
        patch.object(video_job, "get_job", new=AsyncMock(return_value=job)),
        patch.object(video_job, "update_open_job", new=AsyncMock(return_value=True)) as update,
        patch.object(video_job, "download_video", new=AsyncMock()),
        patch.object(video_job, "delete_video", new=AsyncMock()),
        patch.object(video_job, "analyze_video_v2", return_value=V2_ANALYSIS) as v2,
        patch.object(video_job, "analyze_video") as v1,
        patch.object(video_job, "enqueue_callback") as enqueue,
    ):
        assert await video_job.process_video_job("job-2") == JobStatus.DONE.value

    v1.assert_not_called()
    v2.assert_called_once()
    assert v2.call_args.args[1] == TRIPLETS
    closing = update.await_args_list[-1].kwargs
    assert closing["results"] == {
        "requested_points": TRIPLETS, "contract_version": CONTRACT_V2, **V2_ANALYSIS,
    }
    assert closing["max_angle"] is None and closing["min_angle"] is None
    enqueue.assert_called_once_with("job-2")


# --- 5. Callback v2 ---


def _row(results, error=None, max_angle=None, min_angle=None):
    return SimpleNamespace(results=results, error=error, max_angle=max_angle, min_angle=min_angle)


def test_v2_payload_on_success():
    results = {"requested_points": TRIPLETS, "contract_version": CONTRACT_V2, **V2_ANALYSIS}
    payload = payload_from_job(_row(results)).model_dump()

    assert payload == {
        "error": None,
        "contract_version": 2,
        "max_angle": None,
        "min_angle": None,
        "results": {
            "points": [],
            "fps": 30.0,
            "duration_s": 0.1,
            "joints": V2_ANALYSIS["joints"],
            "frames": V2_ANALYSIS["frames"],
        },
    }
    assert "requested_points" not in json.dumps(payload)


def test_v2_payload_on_error_has_empty_joints_and_frames():
    results = {"requested_points": TRIPLETS, "contract_version": CONTRACT_V2}
    payload = payload_from_job(_row(results, error="processing_timeout")).model_dump()

    assert payload["error"] == "processing_timeout"
    assert payload["contract_version"] == 2
    assert payload["results"]["joints"] == [] and payload["results"]["frames"] == []
    assert payload["max_angle"] is None and payload["min_angle"] is None


def test_a_v1_job_still_gets_exactly_the_v1_payload():
    results = {"requested_points": [24, 26, 28], "points": [{"frame": 0, "angle": 90.0}]}
    payload = payload_from_job(_row(results, max_angle=90.0, min_angle=90.0)).model_dump()

    assert payload == {
        "error": None,
        "results": {"points": [{"frame": 0, "angle": 90.0}]},
        "max_angle": 90.0,
        "min_angle": 90.0,
    }


class _CapturingClient:
    calls: list = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, content=None, headers=None):
        _CapturingClient.calls.append(SimpleNamespace(url=url, content=content, headers=headers))
        import httpx

        return httpx.Response(200)


async def test_the_v2_callback_is_signed_over_the_exact_body(monkeypatch):
    monkeypatch.setattr(settings, "HMAC_SECRET", "secreto-de-prueba")
    _CapturingClient.calls = []
    results = {"requested_points": TRIPLETS, "contract_version": CONTRACT_V2, **V2_ANALYSIS}
    payload = payload_from_job(_row(results)).model_dump()

    with patch.object(callback_delivery.httpx, "AsyncClient", _CapturingClient):
        await callback_delivery.deliver_callback("https://platform.example.com/results", payload)

    sent = _CapturingClient.calls[0]
    expected = compute_signature(sent.headers["X-Timestamp"], sent.content, "secreto-de-prueba")
    assert sent.headers["X-Signature"] == expected
    body = json.loads(sent.content)
    assert body["contract_version"] == 2
    assert body["results"]["joints"][0]["points"] == [11, 13, 15]


# --- 6. Retención (Ley 1581): las claves nuevas del JSONB también se minimizan ---


async def test_retention_clears_the_whole_results_jsonb_including_v2_keys():
    """
    `frames` (33 landmarks por frame: biométrico) y `joints` viven dentro del
    JSONB `results`. La minimización lo pone en NULL de SQL COMPLETO, no clave
    por clave, así que alcanza cualquier clave nueva sin enumerarlas.
    """
    from app.utils import db_connect

    session = FakeSession([("job-2",)])
    with patch.object(db_connect.database, "session", return_value=session):
        await db_connect.anonymize_jobs(["job-2"])

    sql = str(
        session.statements[0].compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert "results=NULL" in sql
    # Ninguna minimización parcial por clave que pudiera dejar `frames` o `joints`.
    for key in ("frames", "joints", "landmarks"):
        assert key not in sql


def test_v2_data_lives_only_in_the_results_column():
    """
    Si alguien moviera `frames` o `joints` a columnas propias, la minimización
    (que limpia `results`) dejaría de alcanzarlos: esta prueba lo haría notar.
    """
    from app.utils.db_connect import Job

    columns = {column.name for column in Job.__table__.columns}
    assert "results" in columns
    assert not columns & {"frames", "joints", "landmarks", "world_landmarks"}
