# Una etapa por plataforma de despliegue, sobre una base común (ver
# app/platforms/__init__.py). Cada etapa instala SOLO las dependencias de su
# plataforma: la imagen de una no carga las de otra.
#
#   docker build --target render .   -> Render (Celery + Redis)
#   docker build --target gcp .      -> Google Cloud (Cloud Run + Cloud Tasks)
#
# LA ETAPA DE RENDER VA AL FINAL, Y NO ES ESTÉTICO. Render construye la imagen
# desde el repositorio y su blueprint no permite elegir la etapa: construye la
# última. Si `gcp` quedara al final, Render desplegaría la imagen de Google
# (PLATFORM=gcp, sin Celery) y nada arrancaría. Un test lo comprueba.
#
# Dentro de una plataforma, la misma imagen corre todos los procesos cambiando
# solo el comando. En Render:
#   API:    (CMD por defecto) uvicorn app.main:app ...
#   Worker: celery -A app.platforms.render.worker.celery worker --concurrency=4
#   Beat:   celery -A app.platforms.render.worker.celery beat
# En Google Cloud (dos servicios de Cloud Run):
#   API:    (CMD por defecto) uvicorn app.main:app ...
#   Worker: uvicorn app.platforms.gcp.worker:app ...

# Debian 13 (trixie) y no 12 (bookworm): medido con Trivy sobre esta misma
# imagen, bookworm arrastraba 7 CRITICAL + 75 HIGH en paquetes del sistema y
# trixie 1 CRITICAL + 54 HIGH. MediaPipe y OpenCV funcionan igual en ambas.
FROM python:3.12-slim-trixie AS base

# upgrade: la imagen base se publica cada cierto tiempo, y entre publicaciones
# Debian saca parches. Sin esto, un parche ya disponible no entra hasta que se
# republique la base.
#
# libgl1 y libglib2.0-0t64: mediapipe depende de opencv-contrib-python (la
# variante NO headless), que al importarse carga libGL.so.1 y libgthread. Sin
# ellas el worker muere en el primer `import cv2`. (En trixie el paquete de
# glib se llama con sufijo t64 por la transición de time_t a 64 bits.)
RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends libgl1 libglib2.0-0t64 \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencias antes que el código: así un cambio en app/ no invalida la capa
# de pip, que es la lenta (mediapipe, opencv, jax).
COPY requirements/ ./requirements/
RUN pip install -r requirements/base.txt

# MediaPipe NO trae incluidos los modelos Lite ni Heavy (solo el "full"): los
# descarga dentro de site-packages la primera vez que se instancia Pose. Como
# el contenedor corre sin root (ver abajo), esa descarga fallaría con
# PermissionError en el primer video procesado -- y mediapipe_heavy es el
# modelo por defecto. Se descargan acá, todavía como root, con la misma
# función que usa MediaPipe, para que queden en la ruta exacta que espera.
RUN python -c "from mediapipe.python.solutions.pose import _download_oss_pose_landmark_model as dl; dl(0); dl(2)"

# Sin root: si alguien explota el parseo de un video, no hereda privilegios de
# administrador dentro del contenedor.
RUN useradd --create-home --uid 10001 appuser


# --- Google Cloud: Cloud Run (API y worker) + Cloud Tasks ------------------
FROM base AS gcp

RUN pip install -r requirements/gcp.txt

COPY alembic.ini .
COPY alembic ./alembic
COPY app ./app

ENV PLATFORM=gcp

USER appuser

# Cloud Run indica en $PORT dónde escuchar (8080 por defecto). La forma con sh
# hace falta para expandir la variable, y `exec` para que uvicorn reciba él
# mismo el SIGTERM con el que Cloud Run apaga una instancia: sin `exec` lo
# recibiría sh, y el apagado ordenado (cerrar la base) no llegaría a correr.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}"]


# --- Render: Celery + Redis + Celery Beat ---------------------------------
FROM base AS render

RUN pip install -r requirements/render.txt

COPY alembic.ini .
COPY alembic ./alembic
COPY app ./app

# Directorio de estado para Celery Beat. /app es de root y el proceso corre sin
# privilegios, así que Beat no puede guardar ahí su archivo de agenda: arranca y
# muere con "Permission denied". Y sin Beat no corre ni el barrido de jobs
# colgados ni el resumen de estado, mientras el resto del servicio sigue
# funcionando -- o sea que el fallo no se nota.
RUN mkdir -p /var/lib/celery && chown appuser:appuser /var/lib/celery
ENV BEAT_SCHEDULE_PATH=/var/lib/celery/beat-schedule

ENV PLATFORM=render

USER appuser

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
