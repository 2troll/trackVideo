#!/usr/bin/env python3
"""trackVideo — localiza en el mapa la zona donde se grabó un vídeo de YouTube de Japón.

Uso:
    python trackvideo.py <URL de YouTube> [opciones]
    python trackvideo.py video_local.mp4 [opciones]

Pipeline:
    1. Descarga el vídeo (yt-dlp, máx. 1080p).
    2. Extrae un fotograma cada N segundos (ffmpeg).
    3. OCR japonés+inglés en cada fotograma (tesseract).
    4. Busca pistas geográficas:
         - topónimos de matrículas (なにわ, 品川, 大阪...)  -> muy fiable
         - prefijos telefónicos en carteles (06=Osaka...)   -> fiable
         - códigos postales 〒xxx-xxxx                       -> fiable
         - nombres de tiendas/estaciones -> Nominatim (OSM)  -> orientativo
    5. Genera output/<video>/map.html (mapa Leaflet), report.md y evidence.json.

Todo gratis: sin API keys, sin servicios de pago.
"""

import argparse
import html
import json
import re
import shutil
import subprocess
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

from jp_data import AREA_CODES, PLATE_REGIONS

USER_AGENT = "trackVideo/1.0 (hobby project; https://github.com/)"
NOMINATIM = "https://nominatim.openstreetmap.org/search"

# pesos por tipo de pista al decidir la zona dominante
WEIGHTS = {"matricula": 3.0, "telefono": 3.0, "postal": 4.0, "lugar": 1.0}


def die(msg: str) -> None:
    print(f"[ERROR] {msg}", file=sys.stderr)
    sys.exit(1)


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


# ---------------------------------------------------------------- descarga

def download_video(url: str, workdir: Path) -> tuple[Path, dict]:
    """Descarga el vídeo con yt-dlp y devuelve (ruta, metadatos)."""
    try:
        import yt_dlp
    except ImportError:
        die("Falta yt-dlp. Ejecuta: ./venv/bin/pip install yt-dlp")

    print(f"[1/5] Descargando vídeo: {url}")
    opts = {
        "format": "bv*[height<=1080][ext=mp4]/bv*[height<=1080]/b[height<=1080]/b",
        "outtmpl": str(workdir / "%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    path = Path(ydl.prepare_filename(info))
    if not path.exists():
        # yt-dlp puede haber fusionado a otro contenedor
        candidates = list(workdir.glob(f"{info['id']}.*"))
        if not candidates:
            die("yt-dlp terminó pero no encuentro el fichero descargado.")
        path = candidates[0]
    meta = {
        "id": info.get("id", "video"),
        "title": info.get("title", ""),
        "duration": info.get("duration") or 0,
        "url": info.get("webpage_url", url),
        "uploader": info.get("uploader", ""),
    }
    print(f"      «{meta['title']}» — {meta['duration']}s")
    return path, meta


def probe_duration(path: Path) -> float:
    r = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)])
    try:
        return float(r.stdout.strip())
    except ValueError:
        die(f"No pude leer la duración de {path}: {r.stderr}")


# ---------------------------------------------------------------- fotogramas

def extract_frames(video: Path, frames_dir: Path, duration: float,
                   interval: float, max_frames: int) -> list[dict]:
    """Extrae un fotograma cada `interval` s. Devuelve [{file, t}]."""
    if duration > 0 and duration / interval > max_frames:
        interval = duration / max_frames
        print(f"      Vídeo largo: subo el intervalo a {interval:.1f}s "
              f"para no pasar de {max_frames} fotogramas.")
    frames_dir.mkdir(parents=True, exist_ok=True)
    print(f"[2/5] Extrayendo 1 fotograma cada {interval:.1f}s con ffmpeg…")
    r = run(["ffmpeg", "-y", "-i", str(video),
             "-vf", f"fps=1/{interval}", "-q:v", "2",
             str(frames_dir / "f_%05d.jpg")])
    if r.returncode != 0:
        die(f"ffmpeg falló:\n{r.stderr[-2000:]}")
    frames = sorted(frames_dir.glob("f_*.jpg"))
    if not frames:
        die("ffmpeg no produjo ningún fotograma.")
    result = [{"file": f, "t": round(i * interval, 1)} for i, f in enumerate(frames)]
    print(f"      {len(result)} fotogramas extraídos.")
    return result


# ---------------------------------------------------------------- OCR

def check_tesseract() -> None:
    if not shutil.which("tesseract"):
        die("Falta tesseract. Instálalo con: brew install tesseract tesseract-lang")
    langs = run(["tesseract", "--list-langs"]).stdout
    for need in ("jpn", "jpn_vert"):
        if need not in langs:
            die(f"Falta el idioma '{need}' de tesseract. "
                "Instálalo con: brew install tesseract-lang")


def ocr_frame(image: Path) -> list[str]:
    """OCR de un fotograma. Devuelve líneas de texto con confianza aceptable."""
    lines: dict[tuple, list[str]] = {}
    confs: dict[tuple, list[float]] = {}
    for lang, psm in (("jpn+eng", "11"), ("jpn_vert", "5")):
        r = run(["tesseract", str(image), "stdout", "-l", lang,
                 "--psm", psm, "tsv"])
        if r.returncode != 0:
            continue
        for row in r.stdout.splitlines()[1:]:
            cols = row.split("\t")
            if len(cols) < 12 or not cols[11].strip():
                continue
            try:
                conf = float(cols[10])
            except ValueError:
                continue
            if conf < 55:
                continue
            key = (lang, cols[1], cols[2], cols[3], cols[4])  # page/block/par/line
            lines.setdefault(key, []).append(cols[11].strip())
            confs.setdefault(key, []).append(conf)
    out = []
    for key, words in lines.items():
        text = "".join(words) if "jpn" in key[0] else " ".join(words)
        text = unicodedata.normalize("NFKC", text).strip()
        if len(text) >= 2:
            out.append(text)
    return out


# ---------------------------------------------------------------- pistas

KANJI_KANA = re.compile(r"[぀-ヿ一-鿿]")
PHONE_RE = re.compile(r"(0\d{1,3})[-‐−ー\s()（）]\d{1,4}[-‐−ー\s()（）]?\d{3,4}")
POSTAL_RE = re.compile(r"〒?\s*(\d{3})[-‐−ー](\d{4})")
# sufijos que suelen indicar un topónimo real
PLACE_SUFFIX = re.compile(r"(駅|通り|通|商店街|市場|温泉|神社|寺|城|公園|橋|港|空港)$")

# ruido típico del OCR que no aporta nada
STOPWORDS = {"営業中", "駐車場", "禁煙", "無料", "有料", "案内", "注意", "出口", "入口",
             "本日", "年中無休", "終日", "電話", "受付", "募集", "テナント"}


def find_signals(text_lines: list[str]) -> list[dict]:
    """Busca pistas geográficas directas (matrícula / teléfono / postal)."""
    signals = []
    for line in text_lines:
        for name, (zone, lat, lon) in PLATE_REGIONS.items():
            if name in line:
                signals.append({"type": "matricula", "match": name, "zone": zone,
                                "lat": lat, "lon": lon, "text": line})
        for m in PHONE_RE.finditer(line):
            code = m.group(1)
            # probar prefijos de más largo a más corto (0742 antes que 07)
            for length in (4, 3, 2):
                if code[:length] in AREA_CODES:
                    zone, lat, lon = AREA_CODES[code[:length]]
                    signals.append({"type": "telefono", "match": m.group(0),
                                    "zone": f"tel. {code[:length]} = {zone}",
                                    "lat": lat, "lon": lon, "text": line})
                    break
        for m in POSTAL_RE.finditer(line):
            if "〒" in line:  # sin el símbolo hay demasiados falsos positivos
                signals.append({"type": "postal", "match": f"{m.group(1)}-{m.group(2)}",
                                "zone": None, "lat": None, "lon": None, "text": line})
    return signals


def candidate_places(text_lines: list[str]) -> list[str]:
    """Extrae textos que parecen nombres de sitios, para geocodificar."""
    cands = []
    for line in text_lines:
        line = line.strip("・.,:;|/*-—〜~ 　")
        if not (2 <= len(line) <= 20):
            continue
        if not KANJI_KANA.search(line):
            continue
        if line in STOPWORDS or line in PLATE_REGIONS:
            continue
        # prioridad a lo que acaba en 駅/通り/神社... o tiene ≥2 kanji
        kanji = len(re.findall(r"[一-鿿]", line))
        if PLACE_SUFFIX.search(line) or kanji >= 2:
            cands.append(line)
    return cands


# ---------------------------------------------------------------- geocoding

def nominatim(query: str, extra: dict | None = None) -> dict | None:
    """Consulta Nominatim (OSM, gratis). Respeta 1 petición/segundo."""
    params = {"format": "jsonv2", "limit": "1", "countrycodes": "jp",
              "accept-language": "es"}
    if extra:
        params.update(extra)
    else:
        params["q"] = query
    url = NOMINATIM + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        print(f"      [aviso] Nominatim falló para «{query}»: {e}")
        return None
    finally:
        time.sleep(1.1)  # política de uso de Nominatim
    if not data:
        return None
    hit = data[0]
    return {"lat": float(hit["lat"]), "lon": float(hit["lon"]),
            "display": hit.get("display_name", ""),
            "importance": float(hit.get("importance") or 0)}


def geocode_signals(per_frame: list[dict], max_queries: int) -> list[dict]:
    """Geocodifica postales y nombres candidatos. Devuelve lista de evidencias."""
    evidence = []
    queries_done = 0

    # 1) señales directas (matrícula/teléfono ya traen coordenadas)
    for fr in per_frame:
        for sig in fr["signals"]:
            if sig["type"] == "postal" and queries_done < max_queries:
                hit = nominatim("", {"postalcode": sig["match"]})
                queries_done += 1
                if hit:
                    sig.update(lat=hit["lat"], lon=hit["lon"], zone=hit["display"])
            if sig["lat"] is not None:
                evidence.append({**sig, "t": fr["t"], "frame": fr["frame"]})

    # 2) nombres de sitios: geocodificar los más repetidos primero
    counter: Counter = Counter()
    first_seen: dict[str, dict] = {}
    for fr in per_frame:
        for cand in fr["candidates"]:
            counter[cand] += 1
            first_seen.setdefault(cand, fr)
    for cand, n in counter.most_common():
        if queries_done >= max_queries:
            print(f"      Límite de {max_queries} consultas a Nominatim alcanzado.")
            break
        hit = nominatim(cand)
        queries_done += 1
        if hit and hit["importance"] > 0.05:
            fr = first_seen[cand]
            evidence.append({"type": "lugar", "match": cand, "zone": hit["display"],
                             "lat": hit["lat"], "lon": hit["lon"], "text": cand,
                             "t": fr["t"], "frame": fr["frame"]})
    return evidence


def dominant_area(evidence: list[dict]) -> dict | None:
    """Zona dominante: el punto con más peso de vecinos en un radio de ~60 km."""
    pts = [e for e in evidence if e["lat"] is not None]
    if not pts:
        return None
    best, best_score = None, -1.0
    for p in pts:
        score = 0.0
        for q in pts:
            d2 = (p["lat"] - q["lat"]) ** 2 + ((p["lon"] - q["lon"]) * 0.82) ** 2
            if d2 < 0.55 ** 2:  # ~60 km
                score += WEIGHTS.get(q["type"], 1.0)
        if score > best_score:
            best, best_score = p, score
    near = [q for q in pts
            if (best["lat"] - q["lat"]) ** 2 + ((best["lon"] - q["lon"]) * 0.82) ** 2 < 0.55 ** 2]
    lat = sum(q["lat"] * WEIGHTS.get(q["type"], 1) for q in near) / \
        sum(WEIGHTS.get(q["type"], 1) for q in near)
    lon = sum(q["lon"] * WEIGHTS.get(q["type"], 1) for q in near) / \
        sum(WEIGHTS.get(q["type"], 1) for q in near)
    return {"lat": lat, "lon": lon, "score": best_score,
            "n_evidence": len(near), "sample_zone": best.get("zone") or best["match"]}


# ---------------------------------------------------------------- salida

def hhmmss(t: float) -> str:
    t = int(t)
    return f"{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}"


MAP_TEMPLATE = """<!DOCTYPE html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>trackVideo — __TITLE__</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  html,body{margin:0;height:100%;font-family:system-ui,sans-serif}
  #map{height:100%}
  .lg{position:absolute;z-index:1000;bottom:12px;left:12px;background:#fffd;
      padding:8px 12px;border-radius:8px;font-size:13px;line-height:1.7}
  .dot{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:5px}
  .pop img{max-width:280px;border-radius:6px;display:block;margin-top:6px}
  .pop .t{font-size:12px;color:#555}
</style></head><body>
<div id="map"></div>
<div class="lg"><b>__TITLE__</b><br>
<span class="dot" style="background:#d32f2f"></span>Matrícula
<span class="dot" style="background:#f57c00"></span>Teléfono
<span class="dot" style="background:#7b1fa2"></span>Código postal
<span class="dot" style="background:#1976d2"></span>Lugar (OCR+OSM)<br>
<span class="dot" style="background:#2e7d32"></span>Zona estimada del vídeo</div>
<script>
const EV = __EVIDENCE__;
const AREA = __AREA__;
const COLORS = {matricula:"#d32f2f", telefono:"#f57c00", postal:"#7b1fa2", lugar:"#1976d2"};
const map = L.map("map");
L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png",
  {attribution:"&copy; OpenStreetMap"}).addTo(map);
const bounds = [];
for (const e of EV) {
  if (e.lat === null) continue;
  const c = L.circleMarker([e.lat, e.lon], {radius:8, color:COLORS[e.type],
    fillColor:COLORS[e.type], fillOpacity:.75});
  c.bindPopup(`<div class="pop"><b>${e.type}: ${e.match}</b>
    <div class="t">${e.zone ?? ""}</div>
    <div class="t">min ${e.hhmmss} — <a href="${e.yt}" target="_blank">ver en YouTube</a></div>
    <a href="frames/${e.frame}" target="_blank"><img src="frames/${e.frame}"></a></div>`);
  c.addTo(map);
  bounds.push([e.lat, e.lon]);
}
if (AREA) {
  L.circle([AREA.lat, AREA.lon], {radius:30000, color:"#2e7d32", fillOpacity:.08})
    .bindPopup(`<b>Zona estimada</b><br>${AREA.sample_zone}<br>` +
               `${AREA.n_evidence} pistas coincidentes`).addTo(map);
  bounds.push([AREA.lat, AREA.lon]);
}
if (bounds.length) map.fitBounds(bounds, {padding:[40,40], maxZoom: 14});
else { map.setView([36.2, 138.25], 5); }
</script></body></html>
"""


def write_outputs(outdir: Path, meta: dict, evidence: list[dict],
                  area: dict | None, n_frames: int) -> None:
    print("[5/5] Generando mapa e informe…")
    vid = meta["id"]
    for e in evidence:
        e["frame"] = e["frame"].name if isinstance(e["frame"], Path) else e["frame"]
        e["hhmmss"] = hhmmss(e["t"])
        e["yt"] = (f"https://youtu.be/{vid}?t={int(e['t'])}"
                   if meta.get("url", "").startswith("http") else meta.get("url", ""))

    (outdir / "evidence.json").write_text(
        json.dumps({"video": meta, "area": area, "evidence": evidence},
                   ensure_ascii=False, indent=2), encoding="utf-8")

    page = (MAP_TEMPLATE
            .replace("__TITLE__", html.escape(meta["title"] or vid))
            .replace("__EVIDENCE__", json.dumps(evidence, ensure_ascii=False))
            .replace("__AREA__", json.dumps(area, ensure_ascii=False)))
    (outdir / "map.html").write_text(page, encoding="utf-8")

    lines = [f"# trackVideo — {meta['title']}", "",
             f"- Vídeo: {meta['url']}",
             f"- Duración: {hhmmss(meta['duration'])} — {n_frames} fotogramas analizados",
             f"- Pistas geográficas encontradas: {len(evidence)}", ""]
    if area:
        lines += [f"## Zona estimada: {area['sample_zone']}",
                  f"Centro aproximado: {area['lat']:.4f}, {area['lon']:.4f} "
                  f"({area['n_evidence']} pistas coincidentes)", ""]
    else:
        lines += ["## Sin zona estimada",
                  "No se encontraron suficientes pistas geolocalizables.", ""]
    lines.append("## Pistas (orden cronológico)")
    lines.append("| Minuto | Tipo | Pista | Zona | Ver |")
    lines.append("|---|---|---|---|---|")
    for e in sorted(evidence, key=lambda x: x["t"]):
        zone = (e["zone"] or "")[:70]
        lines.append(f"| {e['hhmmss']} | {e['type']} | {e['match']} | {zone} "
                     f"| [{e['frame']}](frames/{e['frame']}) · [YouTube]({e['yt']}) |")
    (outdir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"      {outdir / 'map.html'}")
    print(f"      {outdir / 'report.md'}")


# ---------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser(description="Geolocaliza vídeos de YouTube de Japón por sus pistas visuales.")
    ap.add_argument("source", help="URL de YouTube o ruta a un vídeo local")
    ap.add_argument("--interval", type=float, default=4.0, help="segundos entre fotogramas (def. 4)")
    ap.add_argument("--max-frames", type=int, default=240, help="máximo de fotogramas (def. 240)")
    ap.add_argument("--max-queries", type=int, default=25, help="máx. consultas a Nominatim (def. 25)")
    ap.add_argument("--no-geocode", action="store_true", help="no consultar Nominatim (solo matrículas/teléfonos)")
    ap.add_argument("--keep-video", action="store_true", help="no borrar el vídeo descargado")
    args = ap.parse_args()

    if not shutil.which("ffmpeg"):
        die("Falta ffmpeg. Instálalo con: brew install ffmpeg")
    check_tesseract()

    base = Path(__file__).resolve().parent
    if re.match(r"https?://", args.source):
        tmp = base / "output" / "_downloads"
        tmp.mkdir(parents=True, exist_ok=True)
        video, meta = download_video(args.source, tmp)
    else:
        video = Path(args.source)
        if not video.exists():
            die(f"No existe el fichero {video}")
        meta = {"id": video.stem, "title": video.stem, "duration": 0,
                "url": str(video), "uploader": ""}
        print(f"[1/5] Usando vídeo local: {video}")

    meta["duration"] = meta["duration"] or probe_duration(video)
    outdir = base / "output" / meta["id"]
    frames_dir = outdir / "frames"
    if frames_dir.exists():
        shutil.rmtree(frames_dir)
    frames = extract_frames(video, frames_dir, meta["duration"],
                            args.interval, args.max_frames)

    print(f"[3/5] OCR (jpn + jpn_vert + eng) en {len(frames)} fotogramas…")
    per_frame = []
    for i, fr in enumerate(frames, 1):
        text = ocr_frame(fr["file"])
        signals = find_signals(text)
        cands = candidate_places(text)
        if signals or cands:
            per_frame.append({"frame": fr["file"], "t": fr["t"],
                              "signals": signals, "candidates": cands})
        if i % 20 == 0 or i == len(frames):
            print(f"      {i}/{len(frames)} — {len(per_frame)} fotogramas con texto útil")

    n_direct = sum(len(f["signals"]) for f in per_frame)
    n_cand = sum(len(f["candidates"]) for f in per_frame)
    print(f"[4/5] Pistas directas: {n_direct} · candidatos a geocodificar: {n_cand}")
    if args.no_geocode:
        evidence = [{**s, "t": f["t"], "frame": f["frame"]}
                    for f in per_frame for s in f["signals"] if s["lat"] is not None]
    else:
        evidence = geocode_signals(per_frame, args.max_queries)

    area = dominant_area(evidence)
    write_outputs(outdir, meta, evidence, area, len(frames))

    if area:
        print(f"\n✅ Zona estimada del vídeo: {area['sample_zone']} "
              f"({area['lat']:.3f}, {area['lon']:.3f}) — "
              f"{area['n_evidence']} pistas coincidentes.")
    else:
        print("\n⚠️  No pude estimar la zona: el vídeo no muestra texto geolocalizable "
              "o el OCR no lo leyó. Prueba con --interval 2 para muestrear más fotogramas.")
    print(f"Abre el mapa:  open {outdir / 'map.html'}")

    if not args.keep_video and video.parent.name == "_downloads":
        video.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
