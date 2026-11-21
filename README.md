# RasterKit

> **CV theme:** Geospatial / raster conversion pipeline (Python)  
> **Repo:** [deviamav/rasterkit](https://github.com/deviamav/rasterkit)

**A universal converter panel for geospatial / raster workflows.**
Upload polygons, pick a spectral index (or write your own band math), choose a date range and STAC collection, and get per-polygon zonal statistics as CSV, SQLite or preview PNGs - with a live map, results table and time-series chart.

RasterKit started life as a Sentinel-2 NDVI script for agricultural fields and was rebuilt into a small, focused GIS tool: a Flask backend on the Python geospatial stack, plus a dependency-light single-page UI.

<!-- Add a screenshot at docs/screenshot.png and uncomment:
![RasterKit UI](docs/screenshot.png)
-->

## Features

- **Converter panel** - GeoJSON or ZIP of GeoJSON files (many fields / parcels), drag & drop, or demo polygons.
- **Index formulas** - NDVI, NDWI, EVI, NDMI, or **custom band math** such as `(B08 - B04) / (B08 + B04)`.
  Formulas are parsed with Python's `ast` module against a strict whitelist (bands, numbers, `+ - * / **`, `sqrt/abs/log/exp`). `eval` is never used.
- **Scene search** - date range, max cloud cover, STAC collection (default `sentinel-2-l2a`) on Microsoft Planetary Computer. No API key is needed for public data.
- **Outputs** - CSV, SQLite (`results`, `scenes`, `job_meta` tables) and colorized preview PNGs (also shown as a map overlay).
- **Interactive results** - Leaflet map coloured per polygon for the selected scene, sortable results table, time-series chart, live job log.
- **DEMO_MODE** - deterministic synthetic scenes and rasters. The whole app works offline, no satellite data required.
- **Dark / light theme**, no CDN dependencies (Leaflet is vendored; only the optional OSM basemap tiles use the network).

## Quick start (demo, offline)

```bash
cd portfolio/apps/rasterkit
python -m venv .venv
# Windows:  .venv\Scripts\activate        macOS/Linux:  source .venv/bin/activate
pip install -r requirements.txt

python web/app.py
```

Open <http://127.0.0.1:5000>, click **Load demo polygons**, leave **Demo mode** checked and press **Run job**.

To force offline mode for every job (e.g. when hosting a portfolio demo):

```powershell
$env:DEMO_MODE = "1"; python web/app.py      # PowerShell
```
```bash
DEMO_MODE=1 python web/app.py                # bash / zsh
```

## Live mode (Sentinel-2 via Planetary Computer)

```bash
pip install -r requirements-live.txt
python web/app.py
```

Untick **Demo mode**, upload your own GeoJSON (EPSG:4326 polygons) and run. For each matching scene RasterKit downloads only the bbox crop of the bands your formula needs, computes the index, and aggregates it per polygon.

If the STAC service is unreachable (or the live extras are missing) and **Fall back to demo data** is enabled, the job continues on synthetic data, logs a warning, and the results are clearly flagged as synthetic.

Notes:

- Band names follow Sentinel-2 asset keys (`B02`, `B03`, `B04`, `B05`, `B06`, `B07`, `B08`, `B8A`, `B11`, `B12`). Other collections work only if their asset keys match the band names used in the formula.
- For Sentinel-2 L2A, values are converted to reflectance (`/10000`, with the `-1000` offset for processing baseline >= 04.00). Zero is treated as nodata.
- The `max_scenes` limit (default 12) keeps the least-cloudy scenes in the range.

## Architecture

```
rasterkit/
├── src/
│   ├── config.py            env-driven settings, paths, limits (no hardcoded paths)
│   ├── fields.py            GeoJSON parsing/validation, bbox, demo polygons
│   ├── indices.py           index registry + safe band-math evaluator (AST whitelist)
│   ├── raster.py            Grid helper + zonal statistics (NumPy + Shapely)
│   ├── sentinel_download.py STAC search + bbox band crops (lazy rasterio/pystac imports)
│   ├── demo.py              DEMO_MODE: synthetic scenes and band rasters
│   ├── previews.py          index raster -> RGBA PNG (pure NumPy/zlib encoder)
│   ├── db.py                CSV / SQLite exporters
│   ├── pipeline.py          job parameters + orchestration (search -> index -> zonal stats -> export)
│   └── jobs.py              background job manager (thread pool, per-job logs)
├── web/
│   ├── app.py               Flask JSON API + page
│   ├── templates/index.html
│   └── static/              css/app.css, js/app.js, vendor/leaflet
├── examples/demo_fields.geojson
├── tests/test_rasterkit.py  offline unittest suite
├── requirements.txt         core (Flask, NumPy, pandas, Shapely)
└── requirements-live.txt    + rasterio, pystac-client, planetary-computer, requests, pyproj
```

Data flow:

```
GeoJSON ──► fields.parse_geojson ──┐
                                   ├─► pipeline.run_job ─► scenes (STAC | synthetic)
formula ──► indices (validate) ────┘        │
                                            ├─► bands ─► indices.compute_index ─► raster.zonal_stats
                                            └─► previews.save_preview_png
                                                     │
              CSV / SQLite / PNG  ◄── db.py ◄────────┘      Flask API ◄── jobs.JobManager ◄── UI (Leaflet)
```

The same index and zonal-statistics code runs in live and demo mode; demo mode only swaps the *source* of the band arrays. Because demo rasters are synthetic reflectance bands, custom formulas (e.g. NDMI, `B08 - B11`) also work in demo mode.

### Result schema

| column | description |
|---|---|
| `field_id` | feature name (`name`/`field_name`/`id` property, else `field_N`) |
| `image_date` | acquisition date (`YYYY-MM-DD`) |
| `index_name` | NDVI / NDWI / EVI / NDMI / CUSTOM |
| `mean_value`, `min_value`, `max_value`, `std_value` | zonal statistics of the index |
| `pixel_count` | valid pixels used |
| `scene_id`, `cloud_cover` | STAC item id and its cloud cover (%) |

### HTTP API

| method | path | purpose |
|---|---|---|
| GET | `/api/config` | indices, defaults, limits, mode flags |
| GET | `/api/demo-geometry` | demo polygons (GeoJSON) |
| POST | `/api/validate` | validate/normalize GeoJSON (JSON body or multipart `file`) |
| POST | `/api/jobs` | start a job: `{ "geojson": ..., "params": {...} }` |
| GET | `/api/jobs/<id>?since=N` | status, progress, incremental log, summary |
| GET | `/api/jobs/<id>/results` | result rows (JSON) |
| GET | `/api/jobs/<id>/download/<csv\|sqlite\|png>` | exports (`png` = zip of previews) |
| GET | `/api/jobs/<id>/preview/<name>.png` | single preview PNG |

## Configuration

All optional, via environment variables (see `.env.example`): `DEMO_MODE`, `RASTERKIT_HOST`, `RASTERKIT_PORT`, `RASTERKIT_DATA_DIR`, `RASTERKIT_JOBS_DIR`, `RASTERKIT_MAX_UPLOAD_MB`, `RASTERKIT_MAX_FEATURES`, `RASTERKIT_MAX_SCENES`. Downloaded crops go to `data/cache/`, job outputs to `outputs/jobs/<job_id>/` (both git-ignored).

## Tests

```bash
python -m unittest discover -s tests -v
```

The suite is offline and covers the formula sandbox, GeoJSON validation, zonal statistics, the demo pipeline and the Flask API end to end.

## Limitations

- Jobs run in an in-process thread pool and are kept in memory (last 50) - this is a single-user tool, not a multi-tenant service.
- Input must be WGS84 lon/lat GeoJSON; reprojection of uploads (shapefiles, other CRSs) is not implemented.
- Pixel selection uses pixel centres (falling back to any intersection for sub-pixel polygons); no cloud/SCL masking beyond the STAC cloud-cover filter.
- Demo data is synthetic and must not be used for any real analysis.

## Data & licences

Satellite data is accessed from [Microsoft Planetary Computer](https://planetarycomputer.microsoft.com/) (Sentinel-2 L2A, Copernicus data free and open). Basemap: &copy; OpenStreetMap contributors. [Leaflet](https://leafletjs.com/) 1.9.4 (BSD-2-Clause) is vendored in `web/static/vendor/leaflet`.
