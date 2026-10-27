"""
Cálculo de ángulos articulares con MediaPipe sobre un video ya descargado.

Usa los modelos ya validados en el repo hermano `entorno-pruebas`
(MediaPipe Heavy/Lite). La lógica de comparación contra gold standard NO vive
acá -- acá solo se produce la salida que espera la plataforma.

Convención de puntos: índices de landmarks de MediaPipe (0-32), que es la
convención que usa la plataforma de telefisioterapia (ver docs/DECISIONES_TECNICAS.md). Los 3
puntos recibidos se interpretan como (proximal, vértice, distal) y el ángulo
se mide en el vértice.

`analyze_video` es el contrato v1 (una terna). `analyze_video_v2` es el
contrato v2: varias ternas en una sola pasada, con los 33 landmarks por frame.
Las dos miden el 2D con la misma `_angle_deg`, que no se modifica.
"""

import math
from typing import Any, Optional

import cv2
import mediapipe as mp

from app.constants.pose_models import MODEL_COMPLEXITY
from app.core.config import settings
from app.utils.logger import get_logger
from app.utils.video_io import open_video

logger = get_logger("pose_analysis")



def _angle_deg(a: tuple[float, float], vertex: tuple[float, float], c: tuple[float, float]) -> float:
    """Ángulo en grados formado en `vertex` por los segmentos vertex->a y vertex->c."""
    v1 = (a[0] - vertex[0], a[1] - vertex[1])
    v2 = (c[0] - vertex[0], c[1] - vertex[1])

    mag1 = math.hypot(*v1)
    mag2 = math.hypot(*v2)
    if mag1 == 0 or mag2 == 0:
        return float("nan")

    cos_angle = (v1[0] * v2[0] + v1[1] * v2[1]) / (mag1 * mag2)
    return math.degrees(math.acos(max(-1.0, min(1.0, cos_angle))))


def analyze_video(video_path: str, points: list[int]) -> dict[str, Any]:
    """
    Procesa el video frame a frame y devuelve los puntos pedidos por frame más
    el ángulo máximo y mínimo observados.

    Devuelve:
        {
          "points": [ {frame, timestamp_ms, landmarks: {idx: {x, y, visibility}}, angle}, ... ],
          "max_angle": float | None,
          "min_angle": float | None,
        }

    Lanza ValueError si el video no se puede abrir o no tiene frames legibles
    (esta es la verificación real de decodificación que no se puede hacer en
    memoria durante la validación del request).
    """
    # Sin valor por defecto: la configuración ya rechaza un modelo desconocido.
    complexity = MODEL_COMPLEXITY[settings.POSE_MODEL]
    proximal, vertex, distal = points

    capture = open_video(video_path)
    if not capture.isOpened():
        raise ValueError(f"No se pudo abrir el video para procesamiento: {video_path}")

    fps = capture.get(cv2.CAP_PROP_FPS) or 30.0
    frames: list[dict[str, Any]] = []
    angles: list[float] = []

    try:
        with mp.solutions.pose.Pose(model_complexity=complexity) as pose:
            frame_index = 0
            while True:
                ok, frame = capture.read()
                if not ok:
                    break

                result = pose.process(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
                entry: dict[str, Any] = {
                    "frame": frame_index,
                    "timestamp_ms": (frame_index / fps) * 1000,
                    "landmarks": {},
                    "angle": None,
                }

                if result.pose_landmarks:
                    height, width = frame.shape[:2]
                    coords: dict[int, tuple[float, float]] = {}
                    for index in points:
                        landmark = result.pose_landmarks.landmark[index]
                        x_px, y_px = landmark.x * width, landmark.y * height
                        coords[index] = (x_px, y_px)
                        entry["landmarks"][str(index)] = {
                            "x": round(x_px, 2),
                            "y": round(y_px, 2),
                            "visibility": round(landmark.visibility, 4),
                        }

                    angle = _angle_deg(coords[proximal], coords[vertex], coords[distal])
                    if not math.isnan(angle):
                        entry["angle"] = round(angle, 2)
                        angles.append(angle)

                frames.append(entry)
                frame_index += 1
    finally:
        capture.release()

    if not frames:
        raise ValueError(f"El video no tiene frames legibles: {video_path}")

    logger.info("Video procesado: %d frames, %d ángulos válidos", len(frames), len(angles))

    return {
        "points": frames,
        "max_angle": round(max(angles), 2) if angles else None,
        "min_angle": round(min(angles), 2) if angles else None,
    }


def _angle_deg_3d(
    a: tuple[float, float, float],
    vertex: tuple[float, float, float],
    c: tuple[float, float, float],
) -> float:
    """
    Ángulo en grados en `vertex`, en 3D, sobre los `pose_world_landmarks`
    (metros, origen en la cadera).

    Es una función APARTE de `_angle_deg` a propósito: el ángulo 2D sobre
    píxeles es la medición clínica validada contra gold standard y no se toca.
    Este 3D se entrega como visualización, no como medición validada
    (docs/DECISIONES_TECNICAS.md de la plataforma, decisión D3).
    """
    v1 = (a[0] - vertex[0], a[1] - vertex[1], a[2] - vertex[2])
    v2 = (c[0] - vertex[0], c[1] - vertex[1], c[2] - vertex[2])

    mag1 = math.sqrt(v1[0] ** 2 + v1[1] ** 2 + v1[2] ** 2)
    mag2 = math.sqrt(v2[0] ** 2 + v2[1] ** 2 + v2[2] ** 2)
    if mag1 == 0 or mag2 == 0:
        return float("nan")

    cos_angle = (v1[0] * v2[0] + v1[1] * v2[1] + v1[2] * v2[2]) / (mag1 * mag2)
    return math.degrees(math.acos(max(-1.0, min(1.0, cos_angle))))


def _rounded_or_none(angle: float) -> Optional[float]:
    return None if math.isnan(angle) else round(angle, 2)


def analyze_video_v2(video_path: str, triplets: list[list[int]]) -> dict[str, Any]:
    """
    Contrato v2: UNA pasada de MediaPipe por video para todas las ternas
    (opción A del tema 4 de la plataforma: el cómputo pasa de N pasadas a 1).

    Devuelve:
        {
          "fps": float, "duration_s": float,
          "joints": [ {points, vertex, max_angle, min_angle, valid_frames}, ... ],
          "frames": [ {t, landmarks, world_landmarks, angles_2d, angles_3d}, ... ],
        }

    Por frame:
      - `landmarks`: los 33 puntos normalizados `[x, y, z, visibility]` (0-1, 4
        decimales), igual que MediaPipe y que el panel de la plataforma.
      - `world_landmarks`: los 33 `pose_world_landmarks` `[x, y, z]` en metros.
      - `angles_2d` / `angles_3d`: uno por terna, en el orden de `joints`;
        `None` si ese frame no tuvo pose (o el ángulo no se puede calcular).
      - Sin pose: `landmarks` y `world_landmarks` en `None`.

    El ángulo 2D es EXACTAMENTE el de v1: `_angle_deg` sobre las coordenadas
    en píxeles, sin redondear antes de calcular. Así una terna medida en v2 da
    los mismos valores que la misma terna en v1. Max/min por articulación
    salen de los ángulos 2D (la medición clínica), no del 3D.

    Lanza ValueError en los mismos casos que `analyze_video`.
    """
    complexity = MODEL_COMPLEXITY[settings.POSE_MODEL]

    capture = open_video(video_path)
    if not capture.isOpened():
        raise ValueError(f"No se pudo abrir el video para procesamiento: {video_path}")

    fps = capture.get(cv2.CAP_PROP_FPS) or 30.0
    frames: list[dict[str, Any]] = []
    joint_angles: list[list[float]] = [[] for _ in triplets]

    try:
        with mp.solutions.pose.Pose(model_complexity=complexity) as pose:
            frame_index = 0
            while True:
                ok, frame = capture.read()
                if not ok:
                    break

                result = pose.process(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
                entry: dict[str, Any] = {
                    "t": round(frame_index / fps, 4),
                    "landmarks": None,
                    "world_landmarks": None,
                    "angles_2d": [None] * len(triplets),
                    "angles_3d": [None] * len(triplets),
                }

                if result.pose_landmarks:
                    height, width = frame.shape[:2]
                    landmarks = result.pose_landmarks.landmark
                    entry["landmarks"] = [
                        [round(lm.x, 4), round(lm.y, 4), round(lm.z, 4), round(lm.visibility, 4)]
                        for lm in landmarks
                    ]
                    pixels = [(lm.x * width, lm.y * height) for lm in landmarks]

                    world = (
                        result.pose_world_landmarks.landmark
                        if result.pose_world_landmarks
                        else None
                    )
                    if world:
                        entry["world_landmarks"] = [
                            [round(lm.x, 4), round(lm.y, 4), round(lm.z, 4)] for lm in world
                        ]

                    for index, (first, vertex, second) in enumerate(triplets):
                        angle = _angle_deg(pixels[first], pixels[vertex], pixels[second])
                        if not math.isnan(angle):
                            entry["angles_2d"][index] = round(angle, 2)
                            joint_angles[index].append(angle)
                        if world:
                            entry["angles_3d"][index] = _rounded_or_none(
                                _angle_deg_3d(
                                    (world[first].x, world[first].y, world[first].z),
                                    (world[vertex].x, world[vertex].y, world[vertex].z),
                                    (world[second].x, world[second].y, world[second].z),
                                )
                            )

                frames.append(entry)
                frame_index += 1
    finally:
        capture.release()

    if not frames:
        raise ValueError(f"El video no tiene frames legibles: {video_path}")

    joints = [
        {
            "points": list(triplet),
            "vertex": triplet[1],
            "max_angle": round(max(angles), 2) if angles else None,
            "min_angle": round(min(angles), 2) if angles else None,
            "valid_frames": len(angles),
        }
        for triplet, angles in zip(triplets, joint_angles)
    ]
    logger.info(
        "Video procesado (v2): %d frames, %d ternas", len(frames), len(triplets)
    )

    return {
        "fps": round(fps, 3),
        "duration_s": round(len(frames) / fps, 3),
        "joints": joints,
        "frames": frames,
    }
