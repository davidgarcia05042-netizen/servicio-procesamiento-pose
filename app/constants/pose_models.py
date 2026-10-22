"""
Modelos de pose que este servicio sabe correr, con su `model_complexity` de
MediaPipe.

Vive en `constants` y no en app/core/pose_analysis.py porque la necesita
también la configuración (para rechazar un POSE_MODEL desconocido al
arrancar), y la configuración no debe importar MediaPipe: cargaría el modelo
en cada proceso que solo lee `settings`, incluida la API.

YOLO26-nano, el tercer candidato de docs/DECISIONES_TECNICAS.md, no está acá: todavía no hay
código que lo sirva (bloqueador #4).
"""

MODEL_COMPLEXITY: dict[str, int] = {
    "mediapipe_lite": 0,
    "mediapipe_full": 1,
    "mediapipe_heavy": 2,
}
