"""Fixtures compartidos entre los archivos de test."""

import struct

import cv2
import numpy as np
import pytest


@pytest.fixture(scope="session")
def real_video_bytes(tmp_path_factory) -> bytes:
    """
    Un MP4 de verdad (unos pocos KB), decodificable por OpenCV.

    Se genera en vez de versionar un archivo binario en el repo. Hace falta
    porque desde que se valida la decodificación, un video "falso" (magic bytes
    correctos + relleno) ya no alcanza para ejercitar el camino feliz.
    """
    path = tmp_path_factory.mktemp("videos") / "real.mp4"

    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 10.0, (64, 48))
    for i in range(5):
        writer.write(np.full((48, 64, 3), i * 40, dtype=np.uint8))
    writer.release()

    return path.read_bytes()


def _write_portrait_phone_video(path) -> None:
    """
    Un MP4 como los de celular: 64x48 "acostado" en disco + metadato de 90°.

    No hay ffmpeg en el entorno de desarrollo, así que el metadato se escribe a
    mano: es la matriz de la caja `tkhd`, que es donde lo guardan los
    celulares y de donde lo lee el demuxer de FFmpeg que usa OpenCV. La matriz
    (0, 1, -1, 0) es una rotación de 90°. La primera columna del video es
    blanca, para poder ver hacia dónde quedó girado.
    """
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 10.0, (64, 48))
    for _ in range(5):
        frame = np.zeros((48, 64, 3), dtype=np.uint8)
        frame[:, :8] = 255
        writer.write(frame)
    writer.release()

    data = bytearray(path.read_bytes())
    box = data.index(b"tkhd")
    version = data[box + 4]
    # version/flags (4) + fechas, id y duración (20 o 32) + reservado, capa,
    # grupo, volumen y reservado (16): ahí empieza la matriz de 9 enteros.
    matrix_offset = box + 4 + 4 + (32 if version == 1 else 20) + 16
    one, minus_one = 0x00010000, 0xFFFF0000
    struct.pack_into(">9I", data, matrix_offset, 0, one, 0, minus_one, 0, 0, 0, 0, 0x40000000)
    path.write_bytes(bytes(data))


@pytest.fixture(scope="session")
def portrait_phone_video(tmp_path_factory):
    """Ruta a un video vertical de celular (ver `_write_portrait_phone_video`)."""
    path = tmp_path_factory.mktemp("videos") / "celular.mp4"
    _write_portrait_phone_video(path)
    return path
