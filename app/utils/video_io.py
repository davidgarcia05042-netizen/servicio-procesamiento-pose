"""
Apertura de videos con la orientación con la que se grabaron.

Los celulares graban en vertical guardando los píxeles "acostados" más un
metadato de rotación (ej. 90°) que los reproductores aplican al mostrar.
OpenCV lo lee pero NO lo aplica por defecto: `CAP_PROP_ORIENTATION_AUTO` vale 0
(verificado el 2026-09-28 con cv2 4.11, backend FFMPEG). Sin esto:

  - El modelo ve a la persona acostada. MediaPipe lo compensa casi del todo
    (BlazePose alinea el cuerpo antes de ubicar los puntos); YOLO no. Medido en
    pose-experimental (TOMA FRONTAL/5EJ6): MediaPipe Heavy 8,03° -> 8,04° de
    error, YOLO26-nano 51,6° -> 44,1° al corregir.
  - Las coordenadas x/y que se devuelven a la plataforma quedan en el marco
    acostado, no en el del video que ve el fisioterapeuta. El ángulo no cambia
    (es invariante a la rotación), los puntos sí.

Se fija explícito en vez de confiar en el valor por defecto, que depende de la
versión y del backend, y el de Render (Linux) no tiene por qué coincidir con el
de Windows.

TODO video del servicio se abre acá. Un test recorre `app/` y falla si aparece
un `cv2.VideoCapture(` fuera de este módulo.
"""

import cv2


def open_video(video_path: str) -> cv2.VideoCapture:
    """
    Abre `video_path` aplicando la rotación declarada en sus metadatos.

    No lanza si no se pudo abrir: cada llamador ya decide qué hacer con un
    `isOpened()` falso (rechazar el request, fallar el job...).
    """
    capture = cv2.VideoCapture(video_path)
    if capture.isOpened():
        capture.set(cv2.CAP_PROP_ORIENTATION_AUTO, 1)
    return capture
