"""RasterKit web app (Flask).

Run with:  python web/app.py
"""

from __future__ import annotations

import io
import os
import re
import sys
import zipfile
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from flask import Flask, abort, jsonify, render_template, request, send_file, send_from_directory  # noqa: E402

from src import config  # noqa: E402
from src.fields import (  # noqa: E402
    GeometryInputError,
    demo_geojson,
    fields_to_geojson,
    get_bbox,
    parse_geojson,
    parse_upload_bytes,
)
from src.indices import BANDS, INDICES  # noqa: E402
from src.jobs import JobManager  # noqa: E402
from src.pipeline import OUTPUT_FORMATS, JobParams, JobParamsError  # noqa: E402
from src.sentinel_download import live_dependencies_available  # noqa: E402

app = Flask(__name__, template_folder=str(ROOT / "web" / "templates"), static_folder=str(ROOT / "web" / "static"))
app.config["MAX_CONTENT_LENGTH"] = config.MAX_UPLOAD_MB * 1024 * 1024
app.json.sort_keys = False

jobs = JobManager(workers=2)

PREVIEW_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]+\.png$")
DOWNLOADS = {
    "csv": ("results.csv", "text/csv"),
    "sqlite": ("results.sqlite", "application/vnd.sqlite3"),
}


def _error(message: str, status: int = 400):
    return jsonify({"error": message}), status


@app.after_request
def _headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    return resp


@app.errorhandler(413)
def _too_large(_exc):
    return _error(f"File too large (limit {config.MAX_UPLOAD_MB} MB).", 413)


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/healthz")
def healthz():
    return jsonify({"status": "ok"})


@app.get("/api/config")
def api_config():
    today = date.today()
    return jsonify(
        {
            "app": "RasterKit",
            "demo_mode_forced": config.DEMO_MODE,
            "live_available": live_dependencies_available(),
            "indices": [
                {"key": d.key, "label": d.label, "expression": d.expression, "description": d.description}
                for d in INDICES.values()
            ],
            "bands": list(BANDS),
            "outputs": list(OUTPUT_FORMATS),
            "defaults": {
                "collection": config.DEFAULT_COLLECTION,
                "max_cloud": config.DEFAULT_MAX_CLOUD_COVER,
                "start": (today - timedelta(days=60)).isoformat(),
                "end": today.isoformat(),
            },
            "limits": {
                "max_upload_mb": config.MAX_UPLOAD_MB,
                "max_features": config.MAX_FEATURES,
                "max_scenes": config.MAX_SCENES,
            },
        }
    )


@app.get("/api/demo-geometry")
def api_demo_geometry():
    return jsonify(demo_geojson())


@app.post("/api/validate")
def api_validate():
    """Parse + validate GeoJSON or a ZIP of GeoJSON files; return normalized GeoJSON."""
    try:
        if "file" in request.files:
            upload = request.files["file"]
            raw = upload.read()
            max_bytes = config.MAX_UPLOAD_MB * 1024 * 1024
            if len(raw) > max_bytes:
                return _error(f"Upload exceeds {config.MAX_UPLOAD_MB} MB limit.", 413)
            fields = parse_upload_bytes(raw, filename=upload.filename or "")
        else:
            payload = request.get_json(silent=True)
            if payload is None:
                return _error("Send GeoJSON as JSON body or multipart file (.geojson / .zip).")
            fields = parse_geojson(payload)
    except GeometryInputError as exc:
        return _error(str(exc), 422)
    return jsonify(
        {
            "geojson": fields_to_geojson(fields),
            "count": len(fields),
            "bbox": get_bbox(fields, pad=0.0),
        }
    )


@app.post("/api/jobs")
def api_create_job():
    body = request.get_json(silent=True) or {}
    try:
        fields = parse_geojson(body.get("geojson"))
        if config.DEMO_MODE:
            body["demo"] = True
        params = JobParams.from_dict(body.get("params") or {})
    except (GeometryInputError, JobParamsError) as exc:
        return _error(str(exc), 422)
    job = jobs.submit(params, fields)
    return jsonify({"job_id": job.job_id}), 202


@app.get("/api/jobs")
def api_list_jobs():
    return jsonify(
        [
            {
                "job_id": j.job_id,
                "status": j.status,
                "created": j.created,
                "index": j.params.label,
                "demo": bool(j.result.synthetic) if j.result else j.params.demo,
            }
            for j in jobs.list()
        ]
    )


def _get_job_or_404(job_id: str):
    job = jobs.get(job_id)
    if job is None:
        abort(404, description="Unknown job.")
    return job


@app.errorhandler(404)
def _not_found(exc):
    if request.path.startswith("/api/"):
        return _error(getattr(exc, "description", "Not found."), 404)
    return exc


@app.get("/api/jobs/<job_id>")
def api_job(job_id: str):
    job = _get_job_or_404(job_id)
    since = request.args.get("since", default=0, type=int) or 0
    return jsonify(job.snapshot(max(since, 0)))


@app.get("/api/jobs/<job_id>/results")
def api_job_results(job_id: str):
    job = _get_job_or_404(job_id)
    if job.result is None:
        return _error("Results are not available yet.", 409)
    return jsonify(job.result.rows)


@app.get("/api/jobs/<job_id>/download/<fmt>")
def api_download(job_id: str, fmt: str):
    job = _get_job_or_404(job_id)
    if job.result is None:
        return _error("Results are not available yet.", 409)
    job_dir = jobs.job_dir(job.job_id)
    if fmt in DOWNLOADS:
        name, mime = DOWNLOADS[fmt]
        if not (job_dir / name).exists():
            return _error(f"Format '{fmt}' was not selected for this job.", 404)
        return send_from_directory(job_dir, name, mimetype=mime, as_attachment=True, download_name=f"rasterkit_{job.job_id}_{name}")
    if fmt == "png":
        preview_dir = job_dir / "previews"
        files = sorted(preview_dir.glob("*.png")) if preview_dir.exists() else []
        if not files:
            return _error("PNG previews were not selected for this job.", 404)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
            for f in files:
                zf.write(f, f.name)
        buf.seek(0)
        return send_file(buf, mimetype="application/zip", as_attachment=True, download_name=f"rasterkit_{job.job_id}_previews.zip")
    return _error("Unknown download format.", 404)


@app.get("/api/jobs/<job_id>/preview/<name>")
def api_preview(job_id: str, name: str):
    job = _get_job_or_404(job_id)
    if not PREVIEW_NAME_RE.match(name):
        abort(404)
    return send_from_directory(jobs.job_dir(job.job_id) / "previews", name, mimetype="image/png", max_age=3600)


def main() -> None:
    debug = os.environ.get("FLASK_DEBUG", "").lower() in {"1", "true"}
    mode = "DEMO_MODE (forced offline)" if config.DEMO_MODE else "live + optional demo"
    print(f"RasterKit running at http://{config.HOST}:{config.PORT}  [{mode}]")
    app.run(host=config.HOST, port=config.PORT, debug=debug, use_reloader=False, threaded=True)


if __name__ == "__main__":
    main()
