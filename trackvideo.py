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
WEIGHTS = {"matricula": 3.0, "telefono": 3.0, "postal": 4.0, "titulo": 2.5,
           "capitulo": 2.5, "mencion": 1.2, "lugar": 1.0}


class TrackError(Exception):
    """Error de pipeline con mensaje pensado para el usuario."""


def die(msg: str) -> None:
    raise TrackError(msg)


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


# ---------------------------------------------------------------- descarga

def download_video(url: str, workdir: Path, log=print) -> tuple[Path, dict]:
    """Descarga el vídeo con yt-dlp y devuelve (ruta, metadatos)."""
    try:
        import yt_dlp
    except ImportError:
        die("Falta yt-dlp. Ejecuta: ./venv/bin/pip install yt-dlp")

    opts = {
        "format": "bv*[height<=1080][ext=mp4]/bv*[height<=1080]/b[height<=1080]/b",
        "outtmpl": str(workdir / "%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }
    # caché: si ya bajamos este vídeo, no volver a molestar a YouTube
    # (repetir descargas provoca bloqueos 403 temporales)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        cached = [p for p in workdir.glob(f"{info['id']}.*")
                  if p.suffix != ".part"]
    if cached:
        log(f"[1/5] Vídeo ya descargado, usando caché: {cached[0].name}")
        path = cached[0]
    else:
        log(f"[1/5] Descargando vídeo: {url}")
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
        "description": info.get("description") or "",
        "chapters": [{"t": c.get("start_time") or 0, "title": c.get("title", "")}
                     for c in info.get("chapters") or []],
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

def _quick_text_score(image: Path) -> int:
    """Puntuación rápida: ¿cuánto texto legible hay en el fotograma?

    Una sola pasada de tesseract sobre el fotograma; cuenta caracteres con
    confianza decente. Sirve para elegir entre candidatos, no para leer.
    """
    r = run(["tesseract", str(image), "stdout", "-l", "jpn+eng",
             "--psm", "11", "tsv"])
    if r.returncode != 0:
        return 0
    score = 0
    for row in r.stdout.splitlines()[1:]:
        cols = row.split("\t")
        if len(cols) < 12:
            continue
        try:
            conf = float(cols[10])
        except ValueError:
            continue
        if conf >= 60:
            score += len(cols[11].strip())
    return score


def _bucket_bounds(n: int, duration: float, base_fps: float,
                   budget: int) -> list[tuple[int, int]]:
    """Reparte el presupuesto de fotogramas: más denso al principio.

    Los canales suelen enseñar el entorno (calle, tiendas, coches) en los
    primeros minutos; el 60% del presupuesto va a los primeros 3 minutos
    (si el vídeo es largo) y el resto se reparte uniforme.
    """
    dense_end_t = min(180.0, duration * 0.5)
    dense_end = min(n, int(dense_end_t * base_fps))
    if duration <= 360 or dense_end <= 0:
        # vídeo corto: reparto uniforme
        return [(b * n // budget, (b + 1) * n // budget) for b in range(budget)]
    b1 = max(1, int(budget * 0.6))
    b2 = budget - b1
    bounds = [(b * dense_end // b1, (b + 1) * dense_end // b1) for b in range(b1)]
    rest = n - dense_end
    bounds += [(dense_end + b * rest // b2, dense_end + (b + 1) * rest // b2)
               for b in range(b2)]
    return bounds


def smart_frames(video: Path, frames_dir: Path, duration: float,
                 budget: int, log=print) -> list[dict]:
    """Elige los `budget` fotogramas con más texto legible.

    1. Extrae fotogramas baratos con ffmpeg (denso: ~1 cada 2 s).
    2. Reparte el presupuesto en tramos, con más peso al principio del vídeo.
    3. De cada tramo coge los 2 más nítidos (mayor JPEG = menos desenfoque)
       y se queda con el que MÁS TEXTO contiene según una pasada rápida de
       OCR — la nitidez sola engaña: la vegetación es nítida pero no dice nada.
    """
    base_fps = min(0.5, 600.0 / max(duration, 1.0))  # tope ~600 extraídos
    frames_dir.mkdir(parents=True, exist_ok=True)
    log(f"[2/5] Extrayendo fotogramas (muestreo a {base_fps:.2f} fps)…")
    r = run(["ffmpeg", "-y", "-i", str(video),
             "-vf", f"fps={base_fps}", "-q:v", "3",
             str(frames_dir / "f_%05d.jpg")])
    if r.returncode != 0:
        die(f"ffmpeg falló:\n{r.stderr[-2000:]}")
    frames = sorted(frames_dir.glob("f_*.jpg"))
    if not frames:
        die("ffmpeg no produjo ningún fotograma.")

    n = len(frames)
    if n <= budget:
        selected = list(range(n))
    else:
        log(f"      {n} muestreados; eligiendo los {budget} con más texto "
            f"(esto tarda un poco)…")
        selected = []
        for k, (lo, hi) in enumerate(_bucket_bounds(n, duration, base_fps,
                                                    budget), 1):
            if lo >= hi:
                continue
            cands = sorted(range(lo, hi),
                           key=lambda i: frames[i].stat().st_size,
                           reverse=True)[:2]
            if len(cands) == 2:
                scores = {i: _quick_text_score(frames[i]) for i in cands}
                # con texto gana el que más tiene; sin texto, el más nítido
                cands.sort(key=lambda i: (scores[i], frames[i].stat().st_size),
                           reverse=True)
            selected.append(cands[0])
            if k % 12 == 0:
                log(f"      … {k}/{budget} tramos revisados")
    selected = sorted(set(selected))
    keep = {frames[i] for i in selected}
    for f in frames:  # borrar los descartados para no comer disco
        if f not in keep:
            f.unlink()
    result = [{"file": frames[i], "t": round(i / base_fps, 1)} for i in selected]
    log(f"      {len(result)} fotogramas seleccionados para lectura a fondo.")
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


_READER = None  # EasyOCR se carga una sola vez (tarda ~25 s la primera)


def _reader():
    global _READER
    if _READER is None:
        import easyocr
        _READER = easyocr.Reader(["ja", "en"], gpu=False, verbose=False)
    return _READER


def ocr_frame(image: Path) -> list[str]:
    """OCR de escena con EasyOCR: lee carteles, tiendas y matrículas de calle.

    EasyOCR detecta y lee texto en fotos reales mucho mejor que Tesseract
    (que es para documentos escaneados). Devuelve las líneas legibles.
    """
    try:
        results = _reader().readtext(str(image), detail=1, paragraph=False)
    except Exception:  # noqa: BLE001 — un frame ilegible no debe parar el lote
        return []
    out, seen = [], set()
    for _box, text, conf in results:
        if conf < 0.35:
            continue
        text = unicodedata.normalize("NFKC", text).strip()
        if len(text) >= 2 and text not in seen:
            seen.add(text)
            out.append(text)
    return out


# ---------------------------------------------------------------- pistas

KANJI_KANA = re.compile(r"[぀-ヿ一-鿿]")
PHONE_RE = re.compile(r"(0\d{1,3})[-‐−ー\s()（）]\d{1,4}[-‐−ー\s()（）]?\d{3,4}")
POSTAL_RE = re.compile(r"〒?\s*(\d{3})[-‐−ー](\d{4})")
# sufijos que suelen indicar un topónimo real
PLACE_SUFFIX = re.compile(r"(駅|通り|通|商店街|市場|温泉|神社|寺|城|公園|橋|港|空港)$")

# ruido típico del OCR que no aporta nada: texto genérico de carteles y
# marcas nacionales de vallas publicitarias (geocodifican a su sede, no al sitio)
STOPWORDS = {"営業中", "駐車場", "禁煙", "無料", "有料", "案内", "注意", "出口", "入口",
             "本日", "年中無休", "終日", "電話", "受付", "募集", "テナント",
             "入口専用", "出口専用", "駐車禁止", "立入禁止", "営業時間", "準備中",
             "龍角散", "サロンパス", "楽天", "ドコモ", "ソフトバンク"}


def find_signals(text_lines: list[str]) -> list[dict]:
    """Busca pistas geográficas directas (matrícula / teléfono / postal)."""
    signals = []
    for line in text_lines:
        for name, (zone, lat, lon) in PLATE_REGIONS.items():
            if name in line:
                # una matrícula real lleva números (ej. 大阪 500 あ 12-34);
                # sin ellos es solo una mención (tienda "沖縄料理", agencia…).
                # Los dígitos de un teléfono no cuentan como números de placa.
                sin_tel = PHONE_RE.sub("", line)
                kind = "matricula" if re.search(r"\d{2}", sin_tel) else "mencion"
                signals.append({"type": kind, "match": name, "zone": zone,
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


# basura típica del OCR: kanji numéricos, rayas, símbolos repetidos
GARBAGE_RE = re.compile(r"^[一二三四五六七八九十〇口日目王三=|｜ー・、。]+$")


def candidate_places(text_lines: list[str]) -> list[str]:
    """Extrae textos que parecen nombres de sitios, para geocodificar."""
    cands = []
    for line in text_lines:
        line = line.strip("・.,:;|/*-—〜~=＝ 　")
        if not (3 <= len(line) <= 20):
            continue
        if not KANJI_KANA.search(line):
            continue
        if line in STOPWORDS or line in PLATE_REGIONS:
            continue
        if PHONE_RE.search(line) or POSTAL_RE.search(line):
            continue  # ya se trató como señal directa
        if GARBAGE_RE.match(line) or len(set(line)) <= 2:
            continue
        # solo lo que de verdad parece un topónimo: sufijo típico o ≥3 kanji distintos
        kanji_distintos = len(set(re.findall(r"[一-鿿]", line)))
        if PLACE_SUFFIX.search(line) or kanji_distintos >= 3:
            cands.append(line)
    return cands


def title_places(meta: dict) -> list[str]:
    """Saca posibles topónimos del título del vídeo (los canales suelen ponerlo)."""
    runs = re.findall(r"[぀-ヿ一-鿿]{3,}", meta.get("title", ""))
    return [r for r in runs if not GARBAGE_RE.match(r)][:5]


# línea de descripción tipo "12:34 天神橋筋商店街" (con horas opcionales)
TIMESTAMP_LINE = re.compile(r"^\s*(?:(\d{1,2}):)?(\d{1,2}):(\d{2})\s*[-–—・:]?\s*(.+)$")


def chapter_places(meta: dict) -> list[tuple[float, str]]:
    """Pistas (segundo, topónimo) de los capítulos y la descripción del vídeo.

    Muchos canales de paseos ponen marcas de tiempo con el nombre del sitio;
    son pistas con minuto exacto y sin coste de OCR.
    """
    entries: list[tuple[float, str]] = []
    for ch in meta.get("chapters") or []:
        entries.append((float(ch["t"]), ch["title"]))
    for line in (meta.get("description") or "").splitlines():
        m = TIMESTAMP_LINE.match(line.strip())
        if m:
            hh, mm, ss = int(m.group(1) or 0), int(m.group(2)), int(m.group(3))
            entries.append((hh * 3600 + mm * 60 + ss, m.group(4)))
    no_lugares = {"イントロ", "スタート", "オープニング", "エンディング",
                  "おわり", "まとめ", "ハイライト"}
    out, seen = [], set()
    for t, text in entries:
        for run_ in re.findall(r"[぀-ヿ一-鿿]{3,}", text):
            if run_ not in seen and run_ not in no_lugares \
                    and not GARBAGE_RE.match(run_):
                seen.add(run_)
                out.append((t, run_))
    return out[:12]


# ---------------------------------------------------------------- geocoding

def nominatim(query: str, extra: dict | None = None) -> dict | None:
    """Consulta Nominatim (OSM, gratis). Respeta 1 petición/segundo."""
    params = {"format": "jsonv2", "limit": "1", "countrycodes": "jp",
              "accept-language": "ja"}  # en japonés para poder verificar la coincidencia
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


def geocode_signals(per_frame: list[dict], title_cands: list[str],
                    chapter_cands: list[tuple[float, str]],
                    max_queries: int, log=print) -> list[dict]:
    """Geocodifica postales, título, capítulos y candidatos de OCR."""
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

    # 2) topónimos del título del vídeo (muy a menudo dicen el sitio exacto)
    for cand in title_cands:
        if queries_done >= max_queries:
            break
        hit = nominatim(cand)
        queries_done += 1
        if hit and cand in hit["display"]:
            evidence.append({"type": "titulo", "match": cand, "zone": hit["display"],
                             "lat": hit["lat"], "lon": hit["lon"], "text": cand,
                             "t": 0.0, "frame": "titulo"})

    # 3) capítulos / marcas de tiempo de la descripción: sitio + minuto exacto
    for t, cand in chapter_cands:
        if queries_done >= max_queries:
            break
        hit = nominatim(cand)
        queries_done += 1
        if hit and cand in hit["display"]:
            evidence.append({"type": "capitulo", "match": cand, "zone": hit["display"],
                             "lat": hit["lat"], "lon": hit["lon"], "text": cand,
                             "t": float(t), "frame": "capitulo"})

    # 4) nombres de sitios leídos por OCR: los más repetidos primero.
    #    Solo se acepta el resultado si el nombre de OSM CONTIENE el texto leído
    #    (sin esto, Nominatim "encuentra" cualquier basura de OCR en algún pueblo).
    counter: Counter = Counter()
    first_seen: dict[str, dict] = {}
    for fr in per_frame:
        for cand in fr["candidates"]:
            counter[cand] += 1
            first_seen.setdefault(cand, fr)
    for cand, n in counter.most_common():
        if queries_done >= max_queries:
            log(f"      Límite de {max_queries} consultas a Nominatim alcanzado.")
            break
        hit = nominatim(cand)
        queries_done += 1
        if hit and cand in hit["display"]:
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
    # una sola pista débil no basta para afirmar una zona
    if len(near) < 2 and WEIGHTS.get(best["type"], 1) < 2:
        return None
    lat = sum(q["lat"] * WEIGHTS.get(q["type"], 1) for q in near) / \
        sum(WEIGHTS.get(q["type"], 1) for q in near)
    lon = sum(q["lon"] * WEIGHTS.get(q["type"], 1) for q in near) / \
        sum(WEIGHTS.get(q["type"], 1) for q in near)
    # la etiqueta sale de la evidencia más fiable del grupo, no de la primera
    # (un cartel "dirección Nara" no debe dar nombre a un vídeo de Osaka)
    rep = max(near, key=lambda q: WEIGHTS.get(q["type"], 1))
    return {"lat": lat, "lon": lon, "score": best_score,
            "n_evidence": len(near), "sample_zone": rep.get("zone") or rep["match"]}


# ---------------------------------------------------------------- salida

def hhmmss(t: float) -> str:
    t = int(t)
    return f"{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}"


MAP_TEMPLATE = """<!DOCTYPE html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#2e7d32">
<title>trackVideo — __TITLE__</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  html,body{margin:0;height:100%;font-family:system-ui,sans-serif}
  #wrap{display:flex;flex-direction:column;height:100%}
  header{background:#2e7d32;color:#fff;padding:10px 14px;z-index:1200;
         box-shadow:0 1px 6px #0004}
  header .row{display:flex;align-items:center;gap:10px}
  header a.back{color:#fff;text-decoration:none;font-size:24px;line-height:1}
  header .tt{font-weight:600;font-size:15px;white-space:nowrap;overflow:hidden;
             text-overflow:ellipsis;flex:1}
  .chip{display:inline-block;background:#fff2;border:1px solid #fff5;
        border-radius:99px;padding:3px 12px;font-size:13px;margin-top:6px;
        max-width:100%;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  #map{flex:1}
  #cards{position:absolute;bottom:0;left:0;right:0;z-index:1100;
         display:flex;gap:10px;overflow-x:auto;padding:10px 12px;
         -webkit-overflow-scrolling:touch;scroll-snap-type:x mandatory}
  .card{scroll-snap-align:start;flex:0 0 220px;background:#fff;border-radius:14px;
        box-shadow:0 2px 10px #0003;overflow:hidden;cursor:pointer}
  .card img{width:100%;height:110px;object-fit:cover;display:block;background:#eee}
  .card .noimg{width:100%;height:110px;display:flex;align-items:center;
        justify-content:center;font-size:40px;background:#e8f5e9}
  .card .b{padding:8px 10px 10px}
  .card .ty{display:inline-block;color:#fff;border-radius:99px;padding:1px 9px;
        font-size:11px;font-weight:600;margin-bottom:4px}
  .card .m{font-weight:600;font-size:14px}
  .card .z{font-size:11.5px;color:#666;height:2.6em;overflow:hidden}
  .card .t{font-size:12px;color:#2e7d32;font-weight:600;margin-top:3px}
  .pop img{max-width:260px;border-radius:8px;display:block;margin-top:6px}
  .pop .t{font-size:12px;color:#555}
  .empty{position:absolute;inset:0;display:flex;align-items:center;
        justify-content:center;z-index:1100;pointer-events:none}
  .empty>div{background:#fffe;border-radius:14px;padding:18px 22px;max-width:340px;
        text-align:center;box-shadow:0 2px 12px #0003;pointer-events:auto}
</style></head><body>
<div id="wrap">
<header>
  <div class="row"><a class="back" href="/">‹</a><div class="tt">__TITLE__</div></div>
  <div id="zone"></div>
</header>
<div id="map" style="position:relative"></div>
</div>
<script>
const EV = __EVIDENCE__;
const AREA = __AREA__;
const COLORS = {matricula:"#d32f2f", telefono:"#f57c00", postal:"#7b1fa2",
                lugar:"#1976d2", titulo:"#00838f", capitulo:"#c2185b",
                mencion:"#78909c"};
const NAMES = {matricula:"Matrícula", telefono:"Teléfono", postal:"C. postal",
               lugar:"Lugar", titulo:"Título", capitulo:"Capítulo",
               mencion:"Mención"};
const zoneEl = document.getElementById("zone");
if (AREA) {
  const short = AREA.sample_zone.split(", ").slice(0,4).join(", ");
  zoneEl.innerHTML = `<span class="chip">📍 ${short}</span>`;
} else {
  zoneEl.innerHTML = `<span class="chip">Sin zona clara — mira las pistas</span>`;
}
const map = L.map("map", {zoomControl:false});
L.control.zoom({position:"topright"}).addTo(map);
L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png",
  {attribution:"&copy; OpenStreetMap"}).addTo(map);
const located = EV.filter(e => e.lat !== null);
const bounds = [], markers = [];
for (const e of located) {
  const c = L.circleMarker([e.lat, e.lon], {radius:9, weight:2.5, color:"#fff",
    fillColor:COLORS[e.type], fillOpacity:.95});
  const hasImg = e.frame && e.frame.startsWith("f_");
  const img = hasImg
    ? `<a href="frames/${e.frame}" target="_blank"><img src="frames/${e.frame}"></a>` : "";
  const when = e.frame === "titulo" ? "título del vídeo"
    : `min ${e.hhmmss} — <a href="${e.yt}" target="_blank">ver en YouTube ▶</a>`;
  c.bindPopup(`<div class="pop"><b>${NAMES[e.type]}: ${e.match}</b>
    <div class="t">${e.zone ?? ""}</div>
    <div class="t">${when}</div>${img}</div>`);
  c.addTo(map);
  markers.push(c);
  bounds.push([e.lat, e.lon]);
}
const route = located.filter(e => e.frame !== "titulo")
                     .sort((a,b) => a.t - b.t).map(e => [e.lat, e.lon]);
if (route.length >= 2)
  L.polyline(route, {color:"#2e7d32", weight:2.5, opacity:.7,
                     dashArray:"6 8"}).addTo(map);
if (AREA) {
  L.circle([AREA.lat, AREA.lon], {radius:30000, color:"#2e7d32", fillOpacity:.08})
    .addTo(map);
  bounds.push([AREA.lat, AREA.lon]);
}
if (bounds.length) map.fitBounds(bounds, {padding:[40,40], maxZoom: 14});
else map.setView([36.2, 138.25], 5);

// tarjetas táctiles: toca una y el mapa vuela a su punto
if (located.length) {
  const cards = document.createElement("div");
  cards.id = "cards";
  located.sort((a,b) => a.t - b.t).forEach((e, i) => {
    const hasImg = e.frame && e.frame.startsWith("f_");
    const media = hasImg ? `<img loading="lazy" src="frames/${e.frame}">`
                         : `<div class="noimg">${e.type==="capitulo"?"🔖":"🎬"}</div>`;
    const when = e.frame === "titulo" ? "del título" : "min " + e.hhmmss;
    const d = document.createElement("div");
    d.className = "card";
    d.innerHTML = `${media}<div class="b">
      <span class="ty" style="background:${COLORS[e.type]}">${NAMES[e.type]}</span>
      <div class="m">${e.match}</div>
      <div class="z">${(e.zone ?? "").split(", ").slice(0,4).join(", ")}</div>
      <div class="t">${when}</div></div>`;
    d.onclick = () => {
      const k = located.indexOf(e);
      map.flyTo([e.lat, e.lon], Math.max(map.getZoom(), 14));
      markers[k].openPopup();
    };
    cards.appendChild(d);
  });
  document.getElementById("map").appendChild(cards);
} else {
  const d = document.createElement("div");
  d.className = "empty";
  d.innerHTML = `<div><b>Sin pistas geolocalizables 😔</b><br>
    <span style="font-size:13.5px;color:#555">El vídeo no muestra carteles,
    matrículas ni nombres legibles. Prueba el modo «a fondo» o con un vídeo
    de paseo por calles.</span></div>`;
  document.getElementById("map").appendChild(d);
}
</script></body></html>
"""


def write_outputs(outdir: Path, meta: dict, evidence: list[dict],
                  area: dict | None, n_frames: int, log=print) -> None:
    log("[5/5] Generando mapa e informe…")
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
    route = [e for e in sorted(evidence, key=lambda x: x["t"])
             if e["lat"] is not None and e["frame"] != "titulo"]
    if len(route) >= 2:
        lines.append("## Recorrido (zonas por las que pasa)")
        for e in route:
            short = ", ".join((e["zone"] or e["match"]).split(", ")[:3])
            lines.append(f"- **{e['hhmmss']}** — {short}")
        lines.append("")
    lines.append("## Pistas (orden cronológico)")
    lines.append("| Minuto | Tipo | Pista | Zona | Ver |")
    lines.append("|---|---|---|---|---|")
    for e in sorted(evidence, key=lambda x: x["t"]):
        zone = (e["zone"] or "")[:70]
        if e["frame"] == "titulo":
            see = "título"
        elif str(e["frame"]).startswith("f_"):
            see = f"[{e['frame']}](frames/{e['frame']}) · [YouTube]({e['yt']})"
        else:
            see = f"[YouTube]({e['yt']})"
        lines.append(f"| {e['hhmmss']} | {e['type']} | {e['match']} | {zone} | {see} |")
    (outdir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    log(f"      {outdir / 'map.html'}")
    log(f"      {outdir / 'report.md'}")


# ---------------------------------------------------------------- pipeline

def analyze(source: str, frames_budget: int = 24, max_queries: int = 25,
            no_geocode: bool = False, keep_video: bool = False,
            log=print) -> dict:
    """Pipeline completo. Devuelve {'outdir', 'meta', 'area', 'n_evidence'}."""
    if not shutil.which("ffmpeg"):
        die("Falta ffmpeg. Instálalo con: brew install ffmpeg")
    check_tesseract()

    base = Path(__file__).resolve().parent
    if re.match(r"https?://", source):
        tmp = base / "output" / "_downloads"
        tmp.mkdir(parents=True, exist_ok=True)
        video, meta = download_video(source, tmp, log)
    else:
        video = Path(source)
        if not video.exists():
            die(f"No existe el fichero {video}")
        meta = {"id": video.stem, "title": video.stem, "duration": 0,
                "url": str(video), "uploader": ""}
        log(f"[1/5] Usando vídeo local: {video}")

    meta["duration"] = meta["duration"] or probe_duration(video)
    outdir = base / "output" / meta["id"]
    frames_dir = outdir / "frames"
    if frames_dir.exists():
        shutil.rmtree(frames_dir)
    frames = smart_frames(video, frames_dir, meta["duration"], frames_budget, log)

    log(f"[3/5] Leyendo carteles y matrículas (EasyOCR) en {len(frames)} "
        f"fotogramas…")
    per_frame = []
    for i, fr in enumerate(frames, 1):
        text = ocr_frame(fr["file"])
        signals = find_signals(text)
        cands = candidate_places(text)
        if signals or cands:
            per_frame.append({"frame": fr["file"], "t": fr["t"],
                              "signals": signals, "candidates": cands})
        if i % 8 == 0 or i == len(frames):
            log(f"      {i}/{len(frames)} — {len(per_frame)} fotogramas con texto útil")

    n_direct = sum(len(f["signals"]) for f in per_frame)
    n_cand = sum(len(f["candidates"]) for f in per_frame)
    log(f"[4/5] Pistas directas: {n_direct} · candidatos a geocodificar: {n_cand}")
    if no_geocode:
        evidence = [{**s, "t": f["t"], "frame": f["frame"]}
                    for f in per_frame for s in f["signals"] if s["lat"] is not None]
    else:
        evidence = geocode_signals(per_frame, title_places(meta),
                                   chapter_places(meta), max_queries, log)

    area = dominant_area(evidence)
    write_outputs(outdir, meta, evidence, area, len(frames), log)

    if area:
        log(f"✅ Zona estimada del vídeo: {area['sample_zone']} "
            f"({area['lat']:.3f}, {area['lon']:.3f}) — "
            f"{area['n_evidence']} pistas coincidentes.")
    else:
        log("⚠️ No pude estimar la zona: el vídeo no muestra texto geolocalizable "
            "o el OCR no lo leyó. Prueba a subir el número de fotogramas.")

    if not keep_video and video.parent.name == "_downloads":
        video.unlink(missing_ok=True)
    return {"outdir": outdir, "meta": meta, "area": area,
            "n_evidence": len(evidence)}


def main() -> None:
    ap = argparse.ArgumentParser(description="Geolocaliza vídeos de YouTube de Japón por sus pistas visuales.")
    ap.add_argument("source", help="URL de YouTube o ruta a un vídeo local")
    ap.add_argument("--frames", type=int, default=24,
                    help="fotogramas nítidos a analizar (def. 24)")
    ap.add_argument("--max-queries", type=int, default=25, help="máx. consultas a Nominatim (def. 25)")
    ap.add_argument("--no-geocode", action="store_true", help="no consultar Nominatim (solo matrículas/teléfonos)")
    ap.add_argument("--keep-video", action="store_true", help="no borrar el vídeo descargado")
    args = ap.parse_args()
    try:
        res = analyze(args.source, args.frames, args.max_queries,
                      args.no_geocode, args.keep_video)
    except TrackError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        sys.exit(1)
    print(f"Abre el mapa:  open {res['outdir'] / 'map.html'}")


if __name__ == "__main__":
    main()
