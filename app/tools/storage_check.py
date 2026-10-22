"""
Prueba de humo del almacenamiento configurado, contra el servicio real.

Sube un archivo pequeño (sin datos de nadie), lo descarga, compara y lo
borra dos veces: la segunda tiene que responder "ya no estaba". Sirve para
confirmar credenciales y bucket ANTES de desplegar, en vez de descubrirlo con
el primer video.

Uso (PowerShell, desde la raíz del proyecto):

    # Render con el video en la base (STORAGE_BACKEND=postgres, el de
    # producción): usa DATABASE_URL, con las migraciones ya corridas
    python -m app.tools.storage_check

    # Render con Supabase Storage
    $env:STORAGE_BACKEND = "supabase"
    $env:SUPABASE_URL = "https://<ref>.supabase.co"
    $env:SUPABASE_SECRET_KEY = "sb_secret_..."
    python -m app.tools.storage_check

    # Google Cloud Storage (con `gcloud auth application-default login` hecho)
    $env:GCS_BUCKET = "<bucket>"
    python -m app.tools.storage_check --plataforma gcp

Sin --plataforma usa la de PLATFORM. Con --plataforma se puede probar el
almacenamiento de una plataforma que todavía no se puede desplegar entera
(Google Cloud, mientras se construye por etapas).

Devuelve 0 si todo salió bien y 1 si algo falló.
"""

import argparse
import asyncio
import os
import sys
import tempfile
from importlib import import_module
from pathlib import Path
from types import ModuleType

from app.core.config import settings
from app.utils.db_connect import database
from app.utils.video_storage import new_video_path

_PROBE = b"prueba de almacenamiento del servicio de pose, sin datos personales\n" * 64

# Los paquetes de app/platforms/, incluidos los que están en construcción.
_PLATFORMS = sorted(
    path.parent.name
    for path in (Path(__file__).resolve().parent.parent / "platforms").glob("*/__init__.py")
)


async def run_check(backend: ModuleType) -> list[str]:
    """Devuelve los problemas encontrados; vacía si todo funcionó."""
    problems: list[str] = []
    # El almacenamiento puede ser la propia base (STORAGE_BACKEND=postgres):
    # en el servicio la conectan la API y el worker al arrancar; acá, esto.
    if not getattr(backend, "USES_DATABASE", False):
        return await _round_trip(backend, problems)
    await database.connect()
    try:
        return await _round_trip(backend, problems)
    finally:
        await database.disconnect()


async def _round_trip(backend: ModuleType, problems: list[str]) -> list[str]:
    path = new_video_path("prueba_almacenamiento.mp4")
    await backend.upload_video(_PROBE, path, "video/mp4")
    print(f"  subido:     {path}")

    with tempfile.TemporaryDirectory() as work_dir:
        destination = os.path.join(work_dir, "bajado.mp4")
        await backend.download_video(path, destination)
        with open(destination, "rb") as file:
            same = file.read() == _PROBE
    print(f"  descargado: {'idéntico' if same else 'DISTINTO'}")
    if not same:
        problems.append("el archivo descargado no coincide con el subido")

    first = await backend.delete_video(path)
    second = await backend.delete_video(path)
    print(f"  borrado:    {first} (debe ser True); repetido: {second} (debe ser False)")
    if first is not True:
        problems.append("el primer borrado no reportó haber borrado el archivo")
    if second is not False:
        problems.append("borrar un archivo que ya no está debería devolver False")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--plataforma", choices=_PLATFORMS, default=settings.PLATFORM)
    args = parser.parse_args(argv)

    try:
        backend = import_module(f"app.platforms.{args.plataforma}").video_storage()
        print(f"Plataforma: {args.plataforma}; almacenamiento: {backend.__name__.rsplit('.', 1)[-1]}")
        backend.ensure_configured()
        problems = asyncio.run(run_check(backend))
    except Exception as exc:  # noqa: BLE001 -- es una herramienta de diagnóstico
        print(f"\nFALLÓ: {exc}")
        return 1
    if problems:
        print("\nFALLÓ: " + "; ".join(problems))
        return 1
    print("\nOK: el almacenamiento funciona.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
