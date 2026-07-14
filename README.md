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

## Uso desde el teléfono (recomendado)

```bash
./venv/bin/python app.py
```

Imprime dos direcciones; abre la de `http://192.168.x.x:8756` en el móvil
(misma Wi-Fi que el Mac). Pega el link de YouTube, elige rápido/normal/a fondo
y espera: verás el progreso en vivo y al terminar salta al mapa. En iPhone
puedes "Añadir a pantalla de inicio" para tenerlo como app. La portada lista
todos los análisis anteriores.

## Uso por línea de comandos

```bash
./venv/bin/python trackvideo.py "https://www.youtube.com/watch?v=XXXX"
open output/XXXX/map.html
```

También funciona con un vídeo local: `./venv/bin/python trackvideo.py video.mp4`

| Opción | Por defecto | Qué hace |
|---|---|---|
| `--frames N` | 24 | fotogramas nítidos a analizar |
| `--max-queries N` | 25 | tope de consultas a Nominatim (1/segundo, su política) |
| `--no-geocode` | — | solo pistas directas (matrículas/teléfonos), sin internet extra |
| `--keep-video` | — | conserva el .mp4 en `output/_downloads/` (la web siempre lo hace) |

## Cómo funciona

1. Descarga el vídeo (máx. 1080p). Si ya se descargó antes, usa la caché —
   repetir bajadas provoca bloqueos 403 temporales de YouTube.
2. **Selección inteligente**: muestrea ~1 fotograma/segundo (barato), divide el
   vídeo en N tramos y de cada tramo elige el fotograma más nítido (mayor
   tamaño JPEG = más detalle = menos desenfoque de movimiento). Solo esos ~24
   pasan al OCR, que es lo lento.
3. OCR japonés+inglés (horizontal y vertical) con Tesseract.
4. Pistas, de más a menos fiables:
   - **código postal** 〒xxx-xxxx (peso 4)
   - **matrícula** — topónimo + números de placa (peso 3)
   - **teléfono** de cartel — 06=Osaka, 075=Kioto… (peso 3)
   - **título del vídeo** geocodificado (peso 2.5)
   - **mención** de un topónimo sin pinta de matrícula (peso 1.2)
   - **lugar** leído por OCR y confirmado en OpenStreetMap (peso 1) — solo se
     acepta si el nombre en OSM contiene literalmente el texto leído
5. La **zona estimada** es el grupo de pistas con más peso en un radio de
   ~60 km; una sola pista débil no basta.

## Qué genera

En `output/<id_del_video>/`:

- **`map.html`** — mapa Leaflet: cada pista es un punto de color con la foto
  del fotograma y enlace al minuto exacto en YouTube; círculo verde = zona
  estimada. Sirve también para ver por qué zonas pasa el vídeo.
- **`report.md`** — tabla cronológica de pistas.
- **`evidence.json`** — datos en crudo.
- **`frames/`** — los fotogramas analizados.

## Límites honestos

- El OCR de texto pequeño o en movimiento falla; con 24 fotos se capturan los
  carteles grandes. Si salen pocas pistas, usa "a fondo" (96 fotos).
- Las matrículas se leen mejor en vídeos de conducción (salen de frente).
- Si el vídeo no muestra texto (naturaleza, interiores), no hay magia posible.
- `output/_downloads/` acumula los vídeos descargados: bórralo cuando ocupe
  mucho.
