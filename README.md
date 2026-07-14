# trackVideo 🗾

Le das una URL de YouTube (vídeos de paseos/conducción por Japón) y te dice
**en qué zona se grabó**, con un mapa interactivo de las pistas visuales que
encontró: matrículas de coche, teléfonos de carteles, códigos postales y
nombres de tiendas/estaciones.

Todo **gratis y local**: sin API keys ni servicios de pago
(yt-dlp + ffmpeg + Tesseract OCR + Nominatim/OpenStreetMap).

## Instalación (macOS)

```bash
brew install ffmpeg tesseract tesseract-lang
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

## Uso

```bash
./venv/bin/python trackvideo.py "https://www.youtube.com/watch?v=XXXX"
open output/XXXX/map.html
```

También funciona con un vídeo local: `./venv/bin/python trackvideo.py video.mp4`

### Opciones

| Opción | Por defecto | Qué hace |
|---|---|---|
| `--interval N` | 4 | segundos entre fotogramas analizados |
| `--max-frames N` | 240 | tope de fotogramas (sube el intervalo si el vídeo es largo) |
| `--max-queries N` | 25 | tope de consultas a Nominatim (1 por segundo, su política) |
| `--no-geocode` | — | solo pistas directas (matrículas/teléfonos), sin internet extra |
| `--keep-video` | — | conserva el .mp4 descargado en `output/_downloads/` |

## Qué genera

En `output/<id_del_video>/`:

- **`map.html`** — mapa Leaflet con cada pista como punto de color
  (rojo=matrícula, naranja=teléfono, morado=postal, azul=lugar) y un círculo
  verde con la **zona estimada**. Cada punto enseña el fotograma y enlaza al
  minuto exacto del vídeo en YouTube.
- **`report.md`** — tabla cronológica de todas las pistas.
- **`evidence.json`** — los datos en crudo.
- **`frames/`** — los fotogramas extraídos.

## Cómo estima la zona

Cada pista tiene un peso: código postal (4) > matrícula = teléfono (3) >
nombre geocodificado (1, es lo más ruidoso). La zona estimada es el grupo de
pistas más pesado dentro de un radio de ~60 km.

Las matrículas japonesas llevan impreso el nombre de la oficina de tráfico
(なにわ, 品川, 京都…): ver muchas iguales es la señal más fiable de dónde
estás. Los prefijos telefónicos de la publicidad (06 = Osaka, 075 = Kioto…)
son la segunda.

## Límites honestos

- El OCR de texto pequeño/en movimiento falla bastante: a 4 s por fotograma
  se capturan las pistas grandes (carteles, fachadas), no todas.
- Los nombres geocodificados por Nominatim pueden dar falsos positivos
  (cadenas de tiendas existen en todo Japón); por eso pesan poco.
- Si el vídeo no muestra texto (naturaleza, interiores), no hay magia posible.
