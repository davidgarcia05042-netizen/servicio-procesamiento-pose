"""
El puerto de la cola: cómo la parte común le pasa trabajo al worker.

La parte común solo sabe que hay DOS tipos de trabajo que encolar, cada uno
identificado por el `job_id` (el resto se lee de la fila):

  - procesar un video (lo encola la API al aceptar uno);
  - un intento de entrega del aviso a la plataforma (ver callback_queue.py).

Cómo viajan lo decide la plataforma activa (ver app/platforms/__init__.py):
en Render, tareas de Celery por Redis. Este módulo no sabe nada de eso.

Las dos funciones LANZAN si no pudieron encolar: quien llama decide qué
hacer (la API marca el job como fallido; callback_queue lo registra y deja la
fila para que la rescaten).
"""

from app import platforms


def enqueue_video_processing(job_id: str) -> None:
    platforms.current().enqueue_video_processing(job_id)


def enqueue_callback(job_id: str) -> None:
    platforms.current().enqueue_callback(job_id)
