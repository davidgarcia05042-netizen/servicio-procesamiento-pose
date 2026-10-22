"""
Envía un video al servicio como lo haría la plataforma, y sigue el job hasta
que el resultado llega al receptor.

SOLO PARA LA DEMO LOCAL (ver docker-compose.demo.yml y la sección "Demo local"
del README).

Lo que muestra, en orden, es lo que distingue a este servicio de uno síncrono:
el POST responde en segundos con un job_id aunque el video tarde más de medio
minuto en procesarse; el job pasa por `queued` -> `processing` -> `done`; y el
resultado le llega a la plataforma sola, por webhook, sin que nadie lo pida.

La solicitud va FIRMADA con HMAC, igual que tendría que firmarla la plataforma
(la demo corre con HMAC_ENFORCE=true). `--sin-firma` sirve para mostrar el 401.

Uso:
    python -m app.tools.demo_send ruta\\al\\video.mp4
    python -m app.tools.demo_send VIDEO --endpoint http://169.254.169.254/robado
"""

import argparse
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Optional

import httpx

from app.utils.hmac_auth import compute_signature

# El mismo valor que HMAC_SECRET en docker-compose.demo.yml (hay un test que
# lo comprueba). Es público a propósito: solo sirve para la demo local.
DEMO_SECRET = "demo-secreto-solo-local"

# El worker corre DENTRO de la red del compose, así que el endpoint es el
# nombre del servicio `receptor`, no localhost.
DEMO_RECEIVER = "http://receptor:9099/resultados"

_FINAL_CALLBACK = {"delivered", "rejected", "exhausted"}


def signed_headers(body: bytes, secret: Optional[str]) -> dict[str, str]:
    """Los headers que agrega la plataforma. Sin secreto, ninguno."""
    if not secret:
        return {}
    timestamp = str(int(time.time()))
    return {"X-Timestamp": timestamp, "X-Signature": compute_signature(timestamp, body, secret)}


def send_video(
    client: httpx.Client,
    video: Path,
    points: list[int],
    exercise: str,
    endpoint: str,
    secret: Optional[str],
) -> httpx.Response:
    """
    El POST /video/ multipart del contrato.

    Se arma la solicitud primero y se firma después porque hay que firmar
    EXACTAMENTE los bytes que viajan: el límite del multipart lo genera httpx
    al armarla y no se conoce antes.
    """
    request = client.build_request(
        "POST",
        "/video/",
        files={"video": (video.name, video.read_bytes(), "video/mp4")},
        data={"points": str(points), "exercise": exercise, "resultsEndpoint": endpoint},
    )
    request.headers.update(signed_headers(request.read(), secret))
    return client.send(request)


def get_job(client: httpx.Client, job_id: str, secret: Optional[str]) -> httpx.Response:
    return client.get(f"/jobs/{job_id}", headers=signed_headers(b"", secret))


def _say(started: float, message: str) -> None:
    print(f"  [{time.monotonic() - started:6.1f} s] {message}", flush=True)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("video", type=Path)
    parser.add_argument("--points", default="24,26,28", help="proximal,vértice,distal (MediaPipe)")
    parser.add_argument("--exercise", default="marcha")
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--endpoint", help="resultsEndpoint; por defecto, el receptor de la demo")
    parser.add_argument("--secret", default=os.environ.get("HMAC_SECRET", DEMO_SECRET))
    parser.add_argument("--sin-firma", action="store_true", help="envía sin HMAC (espera 401)")
    parser.add_argument("--timeout", type=float, default=600, help="segundos máximos de espera")
    args = parser.parse_args(argv)

    if not args.video.is_file():
        print(f"No existe el video: {args.video}", file=sys.stderr)
        return 2

    points = [int(p) for p in args.points.split(",")]
    label = f"{args.exercise}-{uuid.uuid4().hex[:6]}"
    endpoint = args.endpoint or f"{DEMO_RECEIVER}/{label}"
    secret = None if args.sin_firma else args.secret

    size_mb = args.video.stat().st_size / 1e6
    print(f"\nVideo: {args.video.name} ({size_mb:.1f} MB) · puntos {points} · {args.exercise}")
    print(f"resultsEndpoint: {endpoint}\n")

    started = time.monotonic()
    with httpx.Client(base_url=args.api, timeout=120) as client:
        response = send_video(client, args.video, points, args.exercise, endpoint, secret)
        if response.status_code != 200:
            _say(started, f"El servicio respondió {response.status_code}: {response.text}")
            return 1

        answer = response.json()
        if not answer.get("job_id"):
            _say(started, f"Solicitud rechazada: {answer.get('internal_status_code')}")
            return 1

        job_id = answer["job_id"]
        _say(started, f"Respuesta inmediata: job {job_id} (el video sigue en segundo plano)")

        last: Optional[tuple] = None
        while time.monotonic() - started < args.timeout:
            job = get_job(client, job_id, secret).json()
            current = (job["status"], job.get("callback_status"))
            if current != last:
                callback = f" · callback: {current[1]}" if current[1] else ""
                _say(started, f"estado: {current[0]}{callback}")
                last = current

            if current[1] in _FINAL_CALLBACK or (
                current[0] == "failed" and not current[1]
            ):
                break
            time.sleep(1)
        else:
            _say(started, "Se agotó la espera. El job sigue su curso en el servicio.")
            return 1

    print()
    if job["status"] == "done":
        print(f"  Procesamiento: {job['processing_seconds']} s con {job['pose_model']}")
        print(f"  Ángulo máximo: {job['max_angle']}° · mínimo: {job['min_angle']}°")
    else:
        print(f"  El job terminó en {job['status']}: {job.get('error')}")
    print(f"  Callback: {job.get('callback_status')} ({job.get('callback_attempts')} intento/s)")
    if not args.endpoint:
        print("\n  Resultado en la plataforma simulada: http://localhost:9099\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
