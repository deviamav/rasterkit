# RasterKit

![Python](https://img.shields.io/badge/Python-3-3776AB?logo=python&logoColor=white)
![Flask](https://img.shields.io/badge/Flask-web-000000?logo=flask)
![GIS](https://img.shields.io/badge/GIS-STAC%20%2F%20Leaflet-2E8B57)
![License](https://img.shields.io/badge/license-MIT-green)

**Geo / raster converter panel** — upload polygons, pick a spectral index (or custom band math), pull scenes via STAC, and export zonal statistics with a live map.

Started as a Sentinel-2 NDVI helper for field polygons; rebuilt into a small, focused GIS tool with an offline demo mode.

## Why reviewers care

- Real **Python geospatial** pipeline (not only a map widget)
- Safe formula sandbox (`ast` whitelist — never `eval`)
- **DEMO_MODE** for fully offline portfolio runs
- Clean Flask API + Leaflet UI without CDN lock-in

## Features

- GeoJSON or ZIP-of-GeoJSON upload (many parcels)
- Indices: NDVI, NDWI, EVI, NDMI + custom band math
- STAC search (default Sentinel-2 L2A on Planetary Computer)
- Exports: CSV, SQLite, preview PNGs
- Map + table + time series + live job log
- Dark / light theme

## Stack

`Python` · `Flask` · `Shapely` · `rasterio` / numpy stack · `Leaflet` · `STAC`

## Quick start (offline demo)

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
pip install -r requirements.txt
python web/app.py
```

Open http://127.0.0.1:5000 → **Load demo polygons** → keep **Demo mode** on → **Run job**.

```powershell
$env:DEMO_MODE = "1"; python web/app.py
```

## License

MIT — portfolio / demonstration use. Demo rasters are synthetic and not for scientific conclusions.
