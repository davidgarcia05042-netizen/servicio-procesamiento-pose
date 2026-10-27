"""
Receptor de demostración: hace el papel de la plataforma de telefisioterapia.

SOLO PARA LA DEMO LOCAL (ver docker-compose.demo.yml y la sección "Demo local"
del README). Recibe el callback que envía el servicio al terminar un video, le
verifica la firma HMAC igual que tendría que hacerlo la plataforma real, y
muestra en http://localhost:9099 la curva del ángulo articular por frame.

POR QUÉ LA RUTA LLEVA UNA ETIQUETA (`/resultados/{etiqueta}`): el payload del
contrato es `{error, results: {points}, max_angle, min_angle}` y NO incluye el
job_id. La plataforma sabe a qué sesión corresponde un resultado por la URL que
ella misma eligió como `resultsEndpoint`. Acá se imita eso: la etiqueta la pone
quien envía el video (app/tools/demo_send.py).

Para mostrar los reintentos en vivo se puede simular una caída:
    POST /control/caida?activa=true   -> responde 503 a todo callback
    POST /control/caida?activa=false  -> vuelve a aceptar

Uso (fuera de Docker):
    python -m app.tools.demo_receiver
"""

import html
import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse

from app.utils.hmac_auth import is_valid_signature

app = FastAPI(title="Receptor de demostración (plataforma simulada)")


@dataclass
class Attempt:
    """Un intento de entrega que llegó al receptor, aceptado o no."""

    label: str
    received_at: float
    http_status: int
    signature_valid: bool
    payload: Optional[dict[str, Any]] = None


@dataclass
class _State:
    attempts: list[Attempt] = field(default_factory=list)
    down: bool = False


state = _State()


@app.post("/resultados/{label}")
async def receive_results(label: str, request: Request) -> Response:
    """
    Lo mismo que tendría que hacer la plataforma real, en este orden: si está
    "caída" responde 503 (reintentable); si la firma no es válida, 401 (no
    reintentable: el servicio lo registra como `rejected`); si todo está bien,
    guarda el resultado y responde 200.
    """
    body = await request.body()
    signature_valid = is_valid_signature(
        request.headers.get("X-Timestamp", ""), request.headers.get("X-Signature", ""), body
    )

    if state.down:
        status = 503
    elif not signature_valid:
        status = 401
    else:
        status = 200

    payload = None
    if status == 200:
        try:
            payload = json.loads(body)
        except ValueError:
            status = 400

    state.attempts.append(Attempt(label, time.time(), status, signature_valid, payload))
    return Response(status_code=status)


@app.post("/control/caida")
async def set_down(activa: bool) -> dict[str, bool]:
    state.down = activa
    return {"caida": state.down}


@app.get("/api/recibidos")
async def list_received() -> list[dict[str, Any]]:
    """Los intentos en JSON, del más reciente al más antiguo."""
    return [
        {
            "etiqueta": a.label,
            "recibido": datetime.fromtimestamp(a.received_at).isoformat(timespec="seconds"),
            "http": a.http_status,
            "firma_valida": a.signature_valid,
            "payload": a.payload,
        }
        for a in reversed(state.attempts)
    ]


# --- Página ---


def angle_chart_svg(points: list[dict[str, Any]], width: int = 760, height: int = 260) -> str:
    """
    Curva del ángulo por frame, en SVG armado a mano para no sumar una
    dependencia de gráficos a la imagen del servicio.

    Los frames donde MediaPipe no detectó la pose traen `angle: null`: se
    dejan como hueco en la línea en vez de unirlos, porque unirlos inventaría
    un movimiento que nadie midió.
    """
    series = [
        (p.get("timestamp_ms", 0) / 1000, p["angle"])
        for p in points
        if isinstance(p.get("angle"), (int, float))
    ]
    if not series:
        return '<p class="muted">Sin ángulos válidos en este video.</p>'

    pad_l, pad_r, pad_t, pad_b = 48, 12, 12, 30
    t_min = min(p.get("timestamp_ms", 0) for p in points) / 1000
    t_max = max(p.get("timestamp_ms", 0) for p in points) / 1000
    a_min = min(a for _, a in series)
    a_max = max(a for _, a in series)
    t_span = (t_max - t_min) or 1.0
    a_span = (a_max - a_min) or 1.0

    def x(t: float) -> float:
        return pad_l + (t - t_min) / t_span * (width - pad_l - pad_r)

    def y(a: float) -> float:
        return pad_t + (a_max - a) / a_span * (height - pad_t - pad_b)

    segments: list[list[str]] = [[]]
    for p in points:
        angle = p.get("angle")
        if isinstance(angle, (int, float)):
            segments[-1].append(f"{x(p.get('timestamp_ms', 0) / 1000):.1f},{y(angle):.1f}")
        elif segments[-1]:
            segments.append([])

    lines = "".join(
        f'<polyline class="curve" points="{" ".join(seg)}"/>' for seg in segments if seg
    )
    guides = "".join(
        f'<line class="guide" x1="{pad_l}" x2="{width - pad_r}" y1="{y(v):.1f}" y2="{y(v):.1f}"/>'
        f'<text class="tick" x="{pad_l - 6}" y="{y(v) + 4:.1f}" text-anchor="end">{v:.0f}°</text>'
        for v in (a_max, a_min)
    )
    axis = (
        f'<text class="tick" x="{pad_l}" y="{height - 8}">{t_min:.1f} s</text>'
        f'<text class="tick" x="{width - pad_r}" y="{height - 8}" text-anchor="end">{t_max:.1f} s</text>'
    )
    return (
        f'<svg viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="Ángulo por frame entre {a_min:.0f} y {a_max:.0f} grados">'
        f"{guides}{lines}{axis}</svg>"
    )


def _card(attempt: Attempt) -> str:
    when = datetime.fromtimestamp(attempt.received_at).strftime("%H:%M:%S")
    label = html.escape(attempt.label)
    badge = {200: "ok", 503: "warn", 401: "bad", 400: "bad"}.get(attempt.http_status, "bad")
    firma = "firma válida" if attempt.signature_valid else "firma INVÁLIDA"
    head = (
        f'<div class="head"><strong>{label}</strong>'
        f'<span class="badge {badge}">HTTP {attempt.http_status}</span>'
        f'<span class="muted">{when} · {firma}</span></div>'
    )

    if attempt.payload is None:
        reason = {
            503: "Plataforma simulada caída: el servicio va a reintentar.",
            401: "Firma rechazada: el servicio no reintenta y lo registra como rejected.",
        }.get(attempt.http_status, "Cuerpo ilegible.")
        return f'<article class="card">{head}<p class="muted">{reason}</p></article>'

    payload = attempt.payload
    if payload.get("error"):
        return (
            f'<article class="card">{head}<p>El servicio informó un error: '
            f"<code>{html.escape(str(payload['error']))}</code></p></article>"
        )

    points = (payload.get("results") or {}).get("points") or []
    valid = sum(1 for p in points if isinstance(p.get("angle"), (int, float)))
    stats = (
        '<dl class="stats">'
        f"<div><dt>Ángulo máximo</dt><dd>{payload.get('max_angle')}°</dd></div>"
        f"<div><dt>Ángulo mínimo</dt><dd>{payload.get('min_angle')}°</dd></div>"
        f"<div><dt>Frames</dt><dd>{len(points)}</dd></div>"
        f"<div><dt>Con pose detectada</dt><dd>{valid}</dd></div>"
        "</dl>"
    )
    return f'<article class="card">{head}{stats}{angle_chart_svg(points)}</article>'


_PAGE = """<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="3">
<title>Plataforma simulada</title>
<style>
:root {{ --bg:#f6f7f9; --card:#fff; --fg:#1d2330; --muted:#5d6675; --line:#dfe3ea;
        --curve:#2563eb; --ok:#15803d; --warn:#b45309; --bad:#b91c1c; }}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg:#12151b; --card:#1b2029; --fg:#e7eaf0; --muted:#9aa3b2; --line:#2c3340;
          --curve:#60a5fa; --ok:#4ade80; --warn:#fbbf24; --bad:#f87171; }} }}
body {{ margin:0; background:var(--bg); color:var(--fg);
       font:15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }}
main {{ max-width:820px; margin:0 auto; padding:24px 16px; }}
h1 {{ font-size:20px; margin:0 0 4px; }}
.muted {{ color:var(--muted); }}
.card {{ background:var(--card); border:1px solid var(--line); border-radius:10px;
        padding:16px; margin:16px 0; }}
.head {{ display:flex; gap:10px; align-items:baseline; flex-wrap:wrap; }}
.badge {{ font-size:12px; font-weight:600; padding:1px 8px; border-radius:99px; border:1px solid; }}
.ok {{ color:var(--ok); }} .warn {{ color:var(--warn); }} .bad {{ color:var(--bad); }}
.stats {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); gap:8px; margin:12px 0; }}
.stats dt {{ font-size:12px; color:var(--muted); }} .stats dd {{ margin:0; font-size:20px; font-weight:600; }}
svg {{ width:100%; height:auto; display:block; }}
.curve {{ fill:none; stroke:var(--curve); stroke-width:1.6; }}
.guide {{ stroke:var(--line); stroke-dasharray:4 4; }}
.tick {{ fill:var(--muted); font-size:11px; }}
.banner {{ border-color:var(--warn); }}
</style></head><body><main>
<h1>Plataforma de telefisioterapia (simulada)</h1>
<p class="muted">Recibe el callback del servicio de pose. Se actualiza sola cada 3 segundos.</p>
{banner}
{cards}
</main></body></html>"""


@app.get("/", response_class=HTMLResponse)
async def page() -> str:
    banner = (
        '<article class="card banner"><strong class="warn">Caída simulada activa:</strong> '
        "todo callback recibe 503.</article>"
        if state.down
        else ""
    )
    cards = "".join(_card(a) for a in reversed(state.attempts)) or (
        '<p class="muted">Todavía no llegó ningún resultado.</p>'
    )
    return _PAGE.format(banner=banner, cards=cards)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=9099)  # noqa: S104 -- contenedor de demo
