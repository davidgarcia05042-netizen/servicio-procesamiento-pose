"""
Imprime los comandos de gcloud que crean los trabajos de Cloud Scheduler, uno
por cada tarea periódica de app/platforms/gcp/schedule.py.

Se generan desde esa tabla para que los trabajos creados en Google no se
desalineen del código: si cambia una frecuencia o se agrega una tarea, basta
con volver a correr esto.

Uso (PowerShell, desde la raíz del proyecto):

    python -m app.tools.gcp_scheduler --proyecto <id> `
        --worker-url https://pose-worker-<...>.run.app `
        --cuenta pose-tasks@<id>.iam.gserviceaccount.com

Solo imprime: no ejecuta nada ni necesita credenciales. Los comandos se
revisan y se pegan en una terminal con `gcloud` configurado.
"""

import argparse
import sys

from app.platforms.gcp.schedule import scheduled_jobs

JOB_PREFIX = "pose-"


def commands(project: str, region: str, worker_url: str, account: str, time_zone: str) -> list[str]:
    worker_url = worker_url.rstrip("/")
    lines = [f"gcloud services enable cloudscheduler.googleapis.com --project={project}"]
    for job in scheduled_jobs():
        lines.append(
            " `\n    ".join(
                [
                    f"gcloud scheduler jobs create http {JOB_PREFIX}{job.name}",
                    f"--project={project} --location={region}",
                    f'--schedule="{job.cron}" --time-zone="{time_zone}"',
                    f"--uri={worker_url}{job.path} --http-method=POST",
                    f"--oidc-service-account-email={account} --oidc-token-audience={worker_url}",
                    f"--attempt-deadline={job.attempt_deadline_seconds}s --max-retry-attempts=0",
                ]
            )
        )
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--proyecto", required=True)
    parser.add_argument("--worker-url", required=True, help="URL del worker en Cloud Run (https)")
    parser.add_argument("--cuenta", required=True, help="cuenta de servicio que firma (TASKS_SERVICE_ACCOUNT)")
    parser.add_argument("--region", default="us-central1")
    parser.add_argument("--zona-horaria", default="America/Bogota")
    args = parser.parse_args(argv)

    if not args.worker_url.startswith("https://"):
        print("--worker-url tiene que ser la URL https del worker en Cloud Run", file=sys.stderr)
        return 1
    for line in commands(args.proyecto, args.region, args.worker_url, args.cuenta, args.zona_horaria):
        print(line + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
