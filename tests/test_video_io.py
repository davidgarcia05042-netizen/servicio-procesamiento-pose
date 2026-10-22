"""
Orientación de los videos grabados con celular (app/utils/video_io.py).

El video de prueba está guardado 64x48 "acostado" con un metadato de 90°, así
que con la rotación aplicada cada frame sale 48 de ancho por 64 de alto.
"""

import re
from contextlib import contextmanager
from pathlib import Path

import cv2

from app.core import pose_analysis
from app.dtos.error_success_code import ErrorAndSuccessCodes
from app.tools import benchmark_pipeline
from app.utils.request_validations import validate_video_is_decodable
from app.utils.video_io import open_video

PORTRAIT_SHAPE = (64, 48, 3)  # (alto, ancho, canales) una vez rotado


def _first_frame(capture: cv2.VideoCapture):
    try:
        ok, frame = capture.read()
        assert ok
        return frame
    finally:
        capture.release()


def test_the_fixture_really_carries_a_rotation_that_opencv_ignores(portrait_phone_video):
    """
    La premisa de todo el módulo: OpenCV LEE el metadato pero no lo aplica. Si
    esto deja de fallar, el fixture dejó de reproducir un video de celular y
    los demás tests pasarían sin probar nada.
    """
    raw = cv2.VideoCapture(str(portrait_phone_video))
    assert raw.get(cv2.CAP_PROP_ORIENTATION_META) == 90
    assert _first_frame(raw).shape == (48, 64, 3)


def test_open_video_applies_the_rotation(portrait_phone_video):
    frame = _first_frame(open_video(str(portrait_phone_video)))

    assert frame.shape == PORTRAIT_SHAPE
    # La columna blanca del video acostado queda como la fila de arriba: el
    # giro es de 90° y no un simple cambio de tamaño.
    assert frame[:8].mean() > 200 and frame[8:].mean() < 50


def test_open_video_leaves_a_video_without_rotation_as_it_is(tmp_path, real_video_bytes):
    path = tmp_path / "horizontal.mp4"
    path.write_bytes(real_video_bytes)

    assert _first_frame(open_video(str(path))).shape == (48, 64, 3)


def test_open_video_does_not_raise_for_a_missing_file(tmp_path):
    capture = open_video(str(tmp_path / "no_existe.mp4"))
    assert not capture.isOpened()


def test_the_analysis_sees_the_person_upright(portrait_phone_video, monkeypatch):
    """
    Lo que importa: el modelo recibe el frame rotado. Las coordenadas x/y que
    viajan a la plataforma se calculan con el tamaño de ESE frame, así que
    quedan en el marco del video que ve el fisioterapeuta.
    """
    seen_shapes = []

    class _FakePose:
        def process(self, rgb_frame):
            seen_shapes.append(rgb_frame.shape)
            return type("Result", (), {"pose_landmarks": None})()

    @contextmanager
    def _fake_pose_factory(**_kwargs):
        yield _FakePose()

    monkeypatch.setattr(pose_analysis.mp.solutions.pose, "Pose", _fake_pose_factory)

    pose_analysis.analyze_video(str(portrait_phone_video), [24, 26, 28])

    assert seen_shapes and set(seen_shapes) == {PORTRAIT_SHAPE}


def test_the_request_validation_accepts_a_phone_video(portrait_phone_video):
    content = Path(portrait_phone_video).read_bytes()
    assert validate_video_is_decodable(content, ".mp4") is ErrorAndSuccessCodes.SUCCESS


def test_the_benchmark_reports_the_upright_size(portrait_phone_video):
    _fps, width, height = benchmark_pipeline._read_video_info(Path(portrait_phone_video))
    assert (width, height) == (48, 64)


def test_no_video_is_opened_outside_video_io():
    """Un `cv2.VideoCapture(` nuevo en app/ volvería a ignorar la rotación."""
    app_dir = Path(__file__).resolve().parent.parent / "app"
    offenders = [
        str(path.relative_to(app_dir))
        for path in app_dir.rglob("*.py")
        if path.name != "video_io.py"
        and re.search(r"VideoCapture\(", path.read_text(encoding="utf-8"))
    ]
    assert offenders == []
