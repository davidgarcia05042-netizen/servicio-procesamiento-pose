"""Tests de la herramienta de medición (app/tools/benchmark_pipeline.py, T16).

MediaPipe se reemplaza por un doble: acá solo se prueba la lógica de la
herramienta (estadísticas, comodines, JSON, restauración de la configuración),
no el rendimiento real, que se mide con videos reales en el hardware de producción.
"""

import json

import pytest

from app.core.config import settings
from app.tools import benchmark_pipeline as bench


@pytest.fixture
def video_file(tmp_path, real_video_bytes):
    path = tmp_path / "real.mp4"
    path.write_bytes(real_video_bytes)
    return path


@pytest.fixture
def stub_pose(monkeypatch):
    """Sustituye el análisis y la carga del modelo por dobles instantáneos."""
    monkeypatch.setattr(
        bench, "analyze_video", lambda path, points: {"points": [{}] * 5, "max_angle": 90.0}
    )
    monkeypatch.setattr(bench, "_measure_model_init", lambda model: 0.01)


def test_summarize_reports_min_median_and_max():
    assert bench.summarize([3.0, 1.0, 2.0]) == {"min": 1.0, "median": 2.0, "max": 3.0}


def test_wildcards_are_expanded_and_literal_paths_are_left_alone(tmp_path):
    """PowerShell no expande comodines al llamar a python: lo hace la herramienta."""
    (tmp_path / "a.mp4").write_bytes(b"x")
    (tmp_path / "b.mp4").write_bytes(b"x")
    literal = tmp_path / "otro.mp4"

    expanded = bench._expand_paths([tmp_path / "*.mp4", literal])

    assert expanded == [tmp_path / "a.mp4", tmp_path / "b.mp4", literal]


def test_a_measured_video_reports_the_expected_metrics(video_file, stub_pose):
    entry = bench.benchmark_video(video_file, [24, 26, 28], "mediapipe_lite", runs=2)

    assert entry["video"] == "real.mp4"
    assert entry["model"] == "mediapipe_lite"
    assert entry["frames"] == 5
    assert entry["runs"] == 2
    assert entry["resolution"] == "64x48"
    assert entry["validation_s"]["median"] >= 0
    assert entry["analysis_s"]["min"] <= entry["analysis_s"]["median"] <= entry["analysis_s"]["max"]
    assert entry["ms_per_frame"] is not None


def test_the_configured_model_is_restored_even_when_the_run_fails(video_file, monkeypatch):
    """
    La herramienta cambia settings.POSE_MODEL para medir cada modelo. Si no lo
    devolviera a su valor, dejaría la configuración del proceso contaminada.
    """
    original = settings.POSE_MODEL

    def raise_error(path, points):
        raise ValueError("boom")

    monkeypatch.setattr(bench, "analyze_video", raise_error)
    monkeypatch.setattr(bench, "_measure_model_init", lambda model: 0.0)

    with pytest.raises(ValueError, match="boom"):
        bench.benchmark_video(video_file, [24, 26, 28], "mediapipe_lite", runs=1)

    assert settings.POSE_MODEL == original


def test_an_undecodable_video_is_rejected(tmp_path):
    broken = tmp_path / "roto.mp4"
    broken.write_bytes(b"esto no es un video")

    with pytest.raises(ValueError):
        bench.benchmark_video(broken, [24, 26, 28], "mediapipe_lite", runs=1)


def test_results_are_saved_as_json(video_file, stub_pose, tmp_path, capsys):
    output = tmp_path / "medidas.json"

    exit_code = bench.main([str(video_file), "--runs", "2", "--json", str(output)])

    assert exit_code == 0
    saved = json.loads(output.read_text(encoding="utf-8"))
    assert set(saved) == {"environment", "config", "results"}
    assert len(saved["results"]) == 1
    assert saved["config"]["points"] == [24, 26, 28]
    assert "Resumen" in capsys.readouterr().out


def test_several_models_are_measured_in_order(video_file, stub_pose, tmp_path):
    output = tmp_path / "medidas.json"

    bench.main(
        [str(video_file), "--models", "mediapipe_lite", "mediapipe_heavy", "--json", str(output)]
    )

    models = [entry["model"] for entry in json.loads(output.read_text(encoding="utf-8"))["results"]]
    assert models == ["mediapipe_lite", "mediapipe_heavy"]


def test_a_missing_video_is_reported_instead_of_measured(tmp_path, capsys):
    exit_code = bench.main([str(tmp_path / "no_existe.mp4")])

    assert exit_code == 2
    assert "No se encontraron" in capsys.readouterr().err


def test_points_outside_the_landmark_range_are_rejected(video_file):
    """Los índices de MediaPipe van de 0 a 32: el 99 no existe."""
    with pytest.raises(SystemExit) as exit_info:
        bench.main([str(video_file), "--points", "1", "2", "99"])

    assert exit_info.value.code == 2
