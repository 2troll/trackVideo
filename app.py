#!/usr/bin/env python3
"""trackVideo web — pega un link de YouTube desde el móvil y recibe el mapa.

Arranque:
    ./venv/bin/python app.py
Luego abre en el teléfono (misma Wi-Fi) la dirección que imprime, p. ej.
http://192.168.1.23:8756 — se puede "Añadir a pantalla de inicio".

Los trabajos se procesan de uno en uno en un hilo; la página de progreso
muestra el log en vivo y redirige al mapa al terminar.
"""

import json
import socket
import threading
import uuid
from pathlib import Path
from queue import Queue

from flask import (Flask, abort, jsonify, redirect, render_template_string,
                   request, send_from_directory, url_for)

import trackvideo

BASE = Path(__file__).resolve().parent
OUTPUT = BASE / "output"

app = Flask(__name__)
JOBS: dict[str, dict] = {}          # job_id -> estado
JOB_QUEUE: Queue = Queue()


def worker() -> None:
    while True:
        job_id = JOB_QUEUE.get()
        job = JOBS[job_id]
        job["status"] = "corriendo"

        def log(msg: str) -> None:
            print(msg)
            job["log"].append(str(msg))

        try:
            # keep_video: reanalizar el mismo vídeo no debe re-descargarlo
            # (YouTube devuelve 403 si se repite la bajada varias veces)
            res = trackvideo.analyze(job["url"], frames_budget=job["frames"],
                                     keep_video=True, log=log)
            job["status"] = "terminado"
            job["video_id"] = res["meta"]["id"]
            job["area"] = res["area"]
        except trackvideo.TrackError as e:
            job["status"] = "error"
            job["log"].append(f"[ERROR] {e}")
        except Exception as e:  # noqa: BLE001 — un fallo no debe matar el worker
            job["status"] = "error"
            job["log"].append(f"[ERROR inesperado] {type(e).__name__}: {e}")
        finally:
            JOB_QUEUE.task_done()


threading.Thread(target=worker, daemon=True).start()


def past_analyses() -> list[dict]:
    """Análisis ya terminados en output/, para listarlos en la portada."""
    items = []
    for ev in sorted(OUTPUT.glob("*/evidence.json"),
                     key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            d = json.loads(ev.read_text(encoding="utf-8"))
            items.append({"id": d["video"]["id"], "title": d["video"]["title"],
                          "zone": (d["area"] or {}).get("sample_zone"),
                          "n": len(d["evidence"])})
        except (json.JSONDecodeError, KeyError):
            continue
    return items


PAGE = """<!DOCTYPE html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="apple-mobile-web-app-capable" content="yes">
<title>trackVideo 🗾</title>
<style>
 body{font-family:system-ui,sans-serif;max-width:640px;margin:0 auto;padding:16px;
      background:#fafafa;color:#222}
 h1{font-size:1.5em}
 form{display:flex;gap:8px;flex-wrap:wrap}
 input[type=url]{flex:1;min-width:220px;padding:12px;font-size:16px;
      border:1px solid #bbb;border-radius:10px}
 select{padding:12px;font-size:16px;border:1px solid #bbb;border-radius:10px}
 button{padding:12px 20px;font-size:16px;border:0;border-radius:10px;
      background:#2e7d32;color:#fff;font-weight:600}
 .card{background:#fff;border:1px solid #e0e0e0;border-radius:12px;
      padding:12px 16px;margin:10px 0}
 .zone{color:#2e7d32;font-weight:600}
 .muted{color:#888;font-size:.9em}
 pre{background:#111;color:#9f9;padding:12px;border-radius:10px;overflow-x:auto;
     font-size:12px;min-height:120px;white-space:pre-wrap}
 a{color:#1565c0}
</style></head><body>
<h1>🗾 trackVideo</h1>
{{ body }}
</body></html>"""

INDEX_BODY = """
<p>Pega un vídeo de YouTube de Japón y te digo <b>en qué zona se grabó</b>,
con mapa y fotos de las pistas.</p>
<form action="{{ url_for('start') }}" method="post">
  <input type="url" name="url" placeholder="https://www.youtube.com/watch?v=…" required>
  <select name="frames">
    <option value="24" selected>rápido (24 fotos)</option>
    <option value="48">normal (48 fotos)</option>
    <option value="96">a fondo (96 fotos)</option>
  </select>
  <button>Analizar</button>
</form>
{% if analyses %}<h2>Análisis anteriores</h2>
{% for a in analyses %}
<div class="card">
  <a href="/out/{{ a.id }}/map.html"><b>{{ a.title }}</b></a><br>
  {% if a.zone %}<span class="zone">📍 {{ a.zone }}</span><br>{% endif %}
  <span class="muted">{{ a.n }} pistas · <a href="/out/{{ a.id }}/report.md">informe</a></span>
</div>
{% endfor %}{% endif %}
"""

JOB_BODY = """
<p id="st">⏳ {{ job.status }} — la página se actualiza sola. Un vídeo de 10 min
tarda ~2-4 minutos.</p>
<pre id="log"></pre>
<p><a href="/">← volver</a></p>
<script>
async function tick(){
  const r = await fetch("/api/job/{{ job_id }}");
  const j = await r.json();
  document.getElementById("log").textContent = j.log.join("\\n");
  if (j.status === "terminado") location.href = "/out/" + j.video_id + "/map.html";
  else if (j.status === "error") document.getElementById("st").textContent =
    "❌ Falló el análisis (mira el log)";
  else setTimeout(tick, 2000);
}
tick();
</script>
"""


@app.get("/")
def index():
    from markupsafe import Markup
    body = render_template_string(INDEX_BODY, analyses=past_analyses())
    return render_template_string(PAGE, body=Markup(body))


@app.post("/analyze")
def start():
    url = (request.form.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        abort(400, "URL no válida")
    try:
        frames = max(8, min(200, int(request.form.get("frames", 24))))
    except ValueError:
        frames = 24
    job_id = uuid.uuid4().hex[:10]
    JOBS[job_id] = {"status": "en cola", "log": [], "url": url,
                    "frames": frames, "video_id": None, "area": None}
    JOB_QUEUE.put(job_id)
    return redirect(url_for("job_page", job_id=job_id))


@app.get("/job/<job_id>")
def job_page(job_id):
    from markupsafe import Markup
    job = JOBS.get(job_id) or abort(404)
    body = render_template_string(JOB_BODY, job=job, job_id=job_id)
    return render_template_string(PAGE, body=Markup(body))


@app.get("/api/job/<job_id>")
def job_status(job_id):
    job = JOBS.get(job_id) or abort(404)
    return jsonify({"status": job["status"], "log": job["log"][-60:],
                    "video_id": job["video_id"]})


@app.get("/out/<path:path>")
def out_files(path):
    return send_from_directory(OUTPUT, path)


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


if __name__ == "__main__":
    port = 8756
    print(f"\n🗾 trackVideo web")
    print(f"   En este Mac:      http://localhost:{port}")
    print(f"   Desde el móvil:   http://{lan_ip()}:{port}  (misma Wi-Fi)\n")
    app.run(host="0.0.0.0", port=port, debug=False)
