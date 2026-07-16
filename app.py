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
import re
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
            thumb = next((e["frame"] for e in d["evidence"]
                          if str(e.get("frame", "")).startswith("f_")), None)
            zone = (d["area"] or {}).get("sample_zone")
            items.append({"id": d["video"]["id"], "title": d["video"]["title"],
                          "zone": ", ".join(zone.split(", ")[:4]) if zone else None,
                          "n": len(d["evidence"]), "thumb": thumb})
        except (json.JSONDecodeError, KeyError):
            continue
    return items


STEPS = ["Descargando el vídeo", "Eligiendo las mejores fotos",
         "Leyendo carteles y matrículas", "Buscando los sitios en el mapa",
         "Preparando tu mapa"]


def parse_progress(job: dict) -> dict:
    """Convierte el log técnico en (paso actual, %) para la barra de progreso."""
    step, sub = 1, 0.0
    for line in job["log"]:
        m = re.match(r"\[(\d)/5\]", line)
        if m:
            step, sub = int(m.group(1)), 0.0
        m = re.search(r"(\d+)/(\d+) —", line)  # avance del OCR
        if m and step == 3:
            sub = int(m.group(1)) / max(1, int(m.group(2)))
    base = {1: 5, 2: 25, 3: 40, 4: 75, 5: 92}
    span = {1: 20, 2: 15, 3: 35, 4: 17, 5: 7}
    pct = base[step] + span[step] * sub
    if job["status"] == "terminado":
        pct = 100
    return {"step": step, "label": STEPS[step - 1], "pct": round(pct)}


PAGE = """<!DOCTYPE html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="theme-color" content="#2e7d32">
<link rel="manifest" href="/manifest.json">
<link rel="icon" href="/static/icon-192.png">
<link rel="apple-touch-icon" href="/static/icon-192.png">
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
 .card{background:#fff;border:1px solid #e0e0e0;border-radius:14px;
      padding:12px 16px;margin:10px 0}
 a.card.link{display:flex;gap:12px;align-items:center;text-decoration:none;
      color:inherit;padding:10px}
 .thumb{width:96px;height:64px;object-fit:cover;border-radius:10px;flex:none;
      background:#eee}
 .cb{min-width:0}
 .cb b{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
 .zone{color:#2e7d32;font-weight:600;font-size:.95em}
 .muted{color:#888;font-size:.9em}
 .prog{background:#fff;border:1px solid #e0e0e0;border-radius:14px;
      padding:16px}
 .pbar{background:#e0e0e0;border-radius:99px;height:10px;overflow:hidden}
 .pbar div{background:#2e7d32;height:100%;transition:width .6s}
 .plabel{font-weight:600;margin:10px 0 4px}
 #steps{list-style:none;padding:0;margin:8px 0;color:#999}
 #steps li{padding:3px 0 3px 26px;position:relative}
 #steps li::before{content:"○";position:absolute;left:4px}
 #steps li.done{color:#2e7d32}
 #steps li.done::before{content:"✔"}
 #steps li.now{color:#111;font-weight:600}
 #steps li.now::before{content:"●";color:#2e7d32}
 pre{background:#111;color:#9f9;padding:12px;border-radius:10px;overflow-x:auto;
     font-size:12px;min-height:80px;white-space:pre-wrap}
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
    <option value="24">rápido (24 fotos)</option>
    <option value="48" selected>normal (48 fotos)</option>
    <option value="96">a fondo (96 fotos)</option>
  </select>
  <button>Analizar</button>
</form>
{% if analyses %}<h2>Análisis anteriores</h2>
{% for a in analyses %}
<a class="card link" href="/out/{{ a.id }}/map.html">
  {% if a.thumb %}<img class="thumb" loading="lazy"
       src="/out/{{ a.id }}/frames/{{ a.thumb }}" alt="">{% endif %}
  <div class="cb">
    <b>{{ a.title }}</b><br>
    {% if a.zone %}<span class="zone">📍 {{ a.zone }}</span><br>
    {% else %}<span class="muted">sin zona clara</span><br>{% endif %}
    <span class="muted">{{ a.n }} pistas</span>
  </div>
</a>
{% endfor %}{% endif %}
"""

JOB_BODY = """
<div class="prog">
  <div class="pbar"><div id="fill" style="width:3%"></div></div>
  <p id="st" class="plabel">⏳ Preparando…</p>
  <ol id="steps">
    {% for s in steps %}<li id="s{{ loop.index }}">{{ s }}</li>{% endfor %}
  </ol>
  <p class="muted">Puedes cerrar esta página: el análisis sigue en el Mac y
  quedará en «Análisis anteriores».</p>
  <details><summary class="muted">detalles técnicos</summary>
    <pre id="log"></pre></details>
</div>
<p><a href="/">← volver</a></p>
<script>
async function tick(){
  const r = await fetch("/api/job/{{ job_id }}");
  const j = await r.json();
  document.getElementById("log").textContent = j.log.join("\\n");
  document.getElementById("fill").style.width = j.progress.pct + "%";
  document.getElementById("st").textContent =
    j.status === "en cola" ? "⏳ En cola…" : "⏳ " + j.progress.label + "…";
  for (let i = 1; i <= 5; i++) {
    const li = document.getElementById("s" + i);
    li.className = i < j.progress.step ? "done" :
                   i === j.progress.step ? "now" : "";
  }
  if (j.status === "terminado") location.href = "/out/" + j.video_id + "/map.html";
  else if (j.status === "error") {
    document.getElementById("st").textContent = "❌ Falló el análisis";
    document.querySelector("details").open = true;
  }
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
    body = render_template_string(JOB_BODY, job=job, job_id=job_id, steps=STEPS)
    return render_template_string(PAGE, body=Markup(body))


@app.get("/api/job/<job_id>")
def job_status(job_id):
    job = JOBS.get(job_id) or abort(404)
    return jsonify({"status": job["status"], "log": job["log"][-60:],
                    "video_id": job["video_id"],
                    "progress": parse_progress(job)})


@app.get("/out/<path:path>")
def out_files(path):
    return send_from_directory(OUTPUT, path)


@app.get("/manifest.json")
def manifest():
    return jsonify({
        "name": "trackVideo",
        "short_name": "trackVideo",
        "description": "¿En qué zona de Japón se grabó este vídeo?",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#fafafa",
        "theme_color": "#2e7d32",
        "icons": [
            {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png"},
        ],
    })


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
