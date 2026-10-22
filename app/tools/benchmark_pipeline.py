"""
Medición de tiempo, CPU y memoria del procesamiento de un video (T16).

Ejecuta el MISMO código que corre en producción (`validate_video_is_decodable`
y `analyze_video`) sobre uno o más videos reales y reporta lo necesario para
fijar con datos tres números que hoy están puestos a ojo:

  1. La latencia de la validación con OpenCV (camino síncrono del request).
  2. El umbral para dar un job por atascado (STUCK_JOB_TIMEOUT_MINUTES).
  3. Que el visibility_timeout de Redis (VISIBILITY_TIMEOUT_SECONDS en
     app/platforms/render/worker.py, una hora) supere al video más lento: si no, Redis
     re-entrega una tarea que todavía se está procesando.

Uso (desde la raíz del proyecto, como módulo):

    python -m app.tools.benchmark_pipeline videos/corto.mp4 videos/largo.mp4
    python -m app.tools.benchmark_pipeline "videos/*.mp4" --runs 5 --json medidas.json
    python -m app.tools.benchmark_pipeline video.mp4 --models mediapipe_lite mediapipe_heavy

Para que las cifras valgan hay que correrlo en el mismo hardware que
producción (Render: worker en 4 CPU y 8 GB) y con videos representativos, incluido el más
largo y pesado que la plataforma pueda enviar (hasta MAX_VIDEO_SIZE_MB). Correrlo
en un portátil sirve para probar el script, no para decidir.

Límites que conviene tener presentes al leer los resultados:

- Mide UN job a la vez. Con `--concurrency N` en el worker, los jobs compiten
  por CPU y cada uno tarda más. Para simularlo, abrir N terminales y lanzar el
  script a la vez en todas.
- La memoria se muestrea cada 50 ms con `psutil` (si está instalado): un pico
  más corto que eso puede quedar subestimado. Sin `psutil`, en Linux se usa el
  pico acumulado del proceso, que no distingue entre corridas.
- `os.cpu_count()` dentro de un contenedor puede reportar los núcleos del host
  y no el límite asignado; el dato útil es "núcleos usados", no ese número.
"""

import argparse
import glob
import json
import os
import platform
import statistics
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import cv2
import mediapipe as mp

from app.core.config import settings
from app.constants.pose_models import MODEL_COMPLEXITY
from app.core.pose_analysis import analyze_video
from app.dtos.error_success_code import ErrorAndSuccessCodes
from app.utils.request_validations import validate_points, validate_video_is_decodable
from app.utils.video_io import open_video

try:  # opcional: da el pico de RAM por corrida
    import psutil
except ImportError:  # pragma: no cover -- depende del entorno
    psutil = None  # type: ignore[assignment]

try:  # solo existe en Unix
    import resource
except ImportError:  # pragma: no cover -- depende del entorno
    resource = None  # type: ignore[assignment]

_MB = 1024 * 1024
_DEFAULT_POINTS = [24, 26, 28]  # cadera, rodilla y tobillo derechos


class _PeakMemorySampler:
    """Muestrea el RSS del proceso en un hilo aparte mientras dura el bloque `with`."""

    def __init__(self, interval_s: float = 0.05) -> None:
        self._interval = interval_s
        self._stop = threading.Event()
        self._peak = 0
        self._thread: Optional[threading.Thread] = None
        self._process = psutil.Process() if psutil is not None else None

    def __enter__(self) -> "_PeakMemorySampler":
        if self._process is not None:
            self._peak = self._process.memory_info().rss
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        return self

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            self._peak = max(self._peak, self._process.memory_info().rss)

    def __exit__(self, *exc_info: object) -> bool:
        if self._thread is not None:
            self._stop.set()
            self._thread.join()
            self._peak = max(self._peak, self._process.memory_info().rss)
        return False

    @property
    def peak_mb(self) -> Optional[float]:
        if self._process is not None:
            return self._peak / _MB
        if resource is not None:
            raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            # Linux entrega KB; macOS entrega bytes.
            return raw / _MB if sys.platform == "darwin" else raw / 1024
        return None


@dataclass
class _RunResult:
    wall_s: float
    cpu_s: float
    peak_rss_mb: Optional[float]
    frames: int


def _measure_run(video_path: Path, points: list[int]) -> _RunResult:
    """Una corrida de `analyze_video`, igual que la hace el worker."""
    sampler = _PeakMemorySampler()
    cpu_start = time.process_time()
    wall_start = time.perf_counter()
    with sampler:
        result = analyze_video(str(video_path), points)
    wall = time.perf_counter() - wall_start
    # process_time suma la CPU de todos los hilos del proceso (incluye los de
    # TFLite), así que cpu/wall aproxima cuántos núcleos se usaron.
    cpu = time.process_time() - cpu_start
    return _RunResult(wall, cpu, sampler.peak_mb, len(result["points"]))


def _measure_validation(content: bytes, suffix: str, runs: int) -> list[float]:
    """Latencia de `validate_video_is_decodable` (incluye escribir el temporal a disco)."""
    times: list[float] = []
    for _ in range(runs):
        start = time.perf_counter()
        code = validate_video_is_decodable(content, suffix)
        times.append(time.perf_counter() - start)
        if code != ErrorAndSuccessCodes.SUCCESS:
            raise ValueError(f"OpenCV no pudo decodificar el video (código: {code.value})")
    return times


def _measure_model_init(model: str) -> float:
    """Costo aproximado de construir el modelo, que el worker paga en cada job."""
    complexity = MODEL_COMPLEXITY[model]
    start = time.perf_counter()
    with mp.solutions.pose.Pose(model_complexity=complexity):
        pass
    return time.perf_counter() - start


def _read_video_info(video_path: Path) -> tuple[float, int, int]:
    """(fps, ancho, alto) del video, ya con la rotación de sus metadatos aplicada."""
    capture = open_video(str(video_path))
    try:
        if not capture.isOpened():
            raise ValueError(f"No se pudo abrir el video: {video_path}")
        fps = capture.get(cv2.CAP_PROP_FPS) or 0.0
        width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        return fps, width, height
    finally:
        capture.release()


def summarize(values: list[float]) -> dict[str, float]:
    return {"min": min(values), "median": statistics.median(values), "max": max(values)}


def benchmark_video(video_path: Path, points: list[int], model: str, runs: int) -> dict[str, Any]:
    """Mide un video con un modelo y devuelve un diccionario listo para imprimir o guardar."""
    content = video_path.read_bytes()
    fps, width, height = _read_video_info(video_path)

    original_model = settings.POSE_MODEL
    settings.POSE_MODEL = model  # analyze_video lo lee en cada llamada
    try:
        validation = _measure_validation(content, video_path.suffix or ".mp4", runs)
        model_init_s = _measure_model_init(model)
        results = [_measure_run(video_path, points) for _ in range(runs)]
    finally:
        settings.POSE_MODEL = original_model

    frames = results[0].frames
    duration_s = frames / fps if fps else 0.0
    walls = [r.wall_s for r in results]
    analysis = summarize(walls)
    cpu_ratios = [r.cpu_s / r.wall_s for r in results if r.wall_s > 0]
    peaks = [r.peak_rss_mb for r in results if r.peak_rss_mb is not None]

    return {
        "video": video_path.name,
        "size_mb": round(len(content) / _MB, 2),
        "resolution": f"{width}x{height}",
        "fps": round(fps, 2),
        "frames": frames,
        "duration_s": round(duration_s, 2),
        "model": model,
        "runs": runs,
        "validation_s": {k: round(v, 4) for k, v in summarize(validation).items()},
        "model_init_s": round(model_init_s, 3),
        "first_run_s": round(walls[0], 3),
        "analysis_s": {k: round(v, 3) for k, v in analysis.items()},
        "ms_per_frame": round(analysis["median"] / frames * 1000, 2) if frames else None,
        "realtime_factor": round(analysis["median"] / duration_s, 2) if duration_s else None,
        "cores_used": round(statistics.median(cpu_ratios), 2) if cpu_ratios else None,
        "peak_rss_mb": round(max(peaks), 1) if peaks else None,
        # Sin psutil el pico sale de getrusage, que es acumulado de TODO el
        # proceso: el mismo valor para cada corrida, e inflado por lo que ya
        # se midió antes. Se marca para que no se lea como un pico por corrida.
        "peak_rss_per_run": psutil is not None,
    }


def format_report(entry: dict[str, Any]) -> str:
    """Bloque legible para la terminal de un video con un modelo."""
    realtime = entry["realtime_factor"]
    lines = [
        f"video: {entry['video']} ({entry['size_mb']} MB, {entry['resolution']}, "
        f"{entry['fps']} fps, {entry['frames']} frames, {entry['duration_s']} s)",
        f"modelo: {entry['model']}   corridas: {entry['runs']}",
        f"  validación OpenCV (mediana / máx): {entry['validation_s']['median']} s / "
        f"{entry['validation_s']['max']} s",
        f"  carga del modelo (aprox.):         {entry['model_init_s']} s",
        f"  análisis, primera corrida:         {entry['first_run_s']} s",
        f"  análisis, mediana / máx:           {entry['analysis_s']['median']} s / "
        f"{entry['analysis_s']['max']} s",
        f"  por frame:                         {entry['ms_per_frame']} ms",
        "  frente al tiempo real:             "
        + (f"{realtime}x (tarda {realtime} veces lo que dura el video)" if realtime else "n/d"),
        f"  núcleos usados (CPU / reloj):      {entry['cores_used']}",
        "  RAM pico del proceso:              "
        + (
            "n/d (instala psutil para medirlo)"
            if entry["peak_rss_mb"] is None
            else f"{entry['peak_rss_mb']} MB"
            + ("" if entry["peak_rss_per_run"] else "  [acumulado del proceso, sin psutil]")
        ),
    ]
    return "\n".join(lines)


def format_summary(entries: list[dict[str, Any]]) -> str:
    """Compara el peor caso medido con los umbrales configurados hoy."""
    worst = max(entry["analysis_s"]["max"] for entry in entries)
    worst_validation = max(entry["validation_s"]["max"] for entry in entries)
    stuck_s = settings.STUCK_JOB_TIMEOUT_MINUTES * 60
    # Import tardío: el módulo del worker arma la app de Celery, y esta
    # herramienta solo necesita el número.
    from app.platforms.render.worker import VISIBILITY_TIMEOUT_SECONDS as visibility_s
    return "\n".join(
        [
            "",
            "Resumen (peor caso medido en esta ejecución):",
            f"  análisis más lento:   {worst} s",
            f"  validación más lenta: {worst_validation} s",
            f"  umbral de atascado:   {stuck_s} s ({settings.STUCK_JOB_TIMEOUT_MINUTES} min); "
            f"el peor análisis ocupa el {worst / stuck_s:.0%} del umbral",
            f"  visibility_timeout:   {visibility_s} s; "
            f"el peor análisis ocupa el {worst / visibility_s:.0%}",
        ]
    )


def _environment() -> dict[str, Any]:
    return {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "cpu_count_reported": os.cpu_count(),
        "mediapipe": getattr(mp, "__version__", "?"),
        "opencv": cv2.__version__,
        "psutil": psutil is not None,
        "default_model": settings.POSE_MODEL,
    }


def _expand_paths(raw: list[Path]) -> list[Path]:
    """PowerShell no expande comodines al llamar a python, así que se expanden acá."""
    expanded: list[Path] = []
    for item in raw:
        text = str(item)
        if any(char in text for char in "*?["):
            expanded.extend(Path(match) for match in sorted(glob.glob(text)))
        else:
            expanded.append(item)
    return expanded


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.tools.benchmark_pipeline",
        description="Mide tiempo, CPU y RAM del procesamiento de video (T16).",
    )
    parser.add_argument("videos", nargs="+", type=Path, help="uno o más videos (admite comodines)")
    parser.add_argument(
        "--points",
        nargs=3,
        type=int,
        default=_DEFAULT_POINTS,
        metavar=("PROXIMAL", "VERTICE", "DISTAL"),
        help="índices de landmark de MediaPipe (por defecto 24 26 28)",
    )
    parser.add_argument(
        "--models",
        nargs="+",
        choices=sorted(MODEL_COMPLEXITY),
        help="modelos a comparar (por defecto solo el de POSE_MODEL)",
    )
    parser.add_argument("--runs", type=int, default=3, help="corridas por video y modelo")
    parser.add_argument("--json", type=Path, dest="json_path", help="guardar los resultados aquí")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.runs < 1:
        parser.error("--runs debe ser 1 o mayor")
    if validate_points(args.points) != ErrorAndSuccessCodes.SUCCESS:
        parser.error("--points debe ser 3 índices de landmark entre 0 y 32")

    videos = _expand_paths(args.videos)
    missing = [str(video) for video in videos if not video.is_file()]
    if not videos or missing:
        print(f"No se encontraron estos videos: {missing or args.videos}", file=sys.stderr)
        return 2

    models = args.models or [settings.POSE_MODEL]
    environment = _environment()
    print("Entorno:", json.dumps(environment, ensure_ascii=False))

    entries: list[dict[str, Any]] = []
    for video in videos:
        for model in models:
            print(f"\nMidiendo {video.name} con {model} ({args.runs} corridas)...", flush=True)
            entry = benchmark_video(video, args.points, model, args.runs)
            entries.append(entry)
            print(format_report(entry))

    print(format_summary(entries))

    if args.json_path:
        payload = {
            "environment": environment,
            "config": {
                "stuck_job_timeout_minutes": settings.STUCK_JOB_TIMEOUT_MINUTES,
                "points": args.points,
            },
            "results": entries,
        }
        args.json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\nResultados guardados en {args.json_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
