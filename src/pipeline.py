"""End-to-end job pipeline: geometries -> scenes -> index -> zonal stats -> exports."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd

from src import demo
from src.config import DEFAULT_COLLECTION, DEFAULT_MAX_CLOUD_COVER, MAX_SCENES
from src.db import RESULT_COLUMNS, save_csv, save_sqlite
from src.fields import Field, get_bbox
from src.indices import (
    FormulaError,
    bands_in_formula,
    compute_index,
    index_range,
    resolve_expression,
)
from src.previews import save_preview_png
from src.raster import Grid, Scene, reproject_geometry, zonal_stats

OUTPUT_FORMATS = ("csv", "sqlite", "png")
MAX_RANGE_DAYS = 731

Log = Callable[[str], None]
Progress = Callable[[float], None]


class JobParamsError(ValueError):
    """Invalid job parameters (user-facing message)."""


@dataclass
class JobParams:
    index: str = "NDVI"
    custom_formula: str = ""
    start: str = ""
    end: str = ""
    max_cloud: float = DEFAULT_MAX_CLOUD_COVER
    collection: str = DEFAULT_COLLECTION
    outputs: list[str] = field(default_factory=lambda: ["csv"])
    demo: bool = False
    fallback_to_demo: bool = True

    @classmethod
    def from_dict(cls, data: dict) -> "JobParams":
        try:
            p = cls(
                index=str(data.get("index", "NDVI")).upper(),
                custom_formula=str(data.get("custom_formula") or "").strip(),
                start=str(data.get("start") or ""),
                end=str(data.get("end") or ""),
                max_cloud=float(data.get("max_cloud", DEFAULT_MAX_CLOUD_COVER)),
                collection=str(data.get("collection") or DEFAULT_COLLECTION).strip(),
                outputs=[str(o).lower() for o in (data.get("outputs") or [])],
                demo=bool(data.get("demo", False)),
                fallback_to_demo=bool(data.get("fallback_to_demo", True)),
            )
        except (TypeError, ValueError) as exc:
            raise JobParamsError(f"Invalid parameter value: {exc}") from exc
        p.validate()
        return p

    def validate(self) -> None:
        try:
            self.start_date, self.end_date = date.fromisoformat(self.start), date.fromisoformat(self.end)
        except ValueError as exc:
            raise JobParamsError("Dates must be in YYYY-MM-DD format.") from exc
        if self.end_date < self.start_date:
            raise JobParamsError("End date must not be before start date.")
        if (self.end_date - self.start_date).days > MAX_RANGE_DAYS:
            raise JobParamsError(f"Date range is too long (max {MAX_RANGE_DAYS} days).")
        if not 0 <= self.max_cloud <= 100:
            raise JobParamsError("Cloud cover must be between 0 and 100 %.")
        if not self.collection or len(self.collection) > 80 or not all(c.isalnum() or c in "-_." for c in self.collection):
            raise JobParamsError("Invalid STAC collection id.")
        self.outputs = [o for o in dict.fromkeys(self.outputs)]
        bad = [o for o in self.outputs if o not in OUTPUT_FORMATS]
        if bad:
            raise JobParamsError(f"Unsupported output format: {', '.join(bad)}.")
        try:
            self.label, self.expression = resolve_expression(self.index, self.custom_formula)
            self.bands = bands_in_formula(self.expression)
        except FormulaError as exc:
            raise JobParamsError(str(exc)) from exc

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "label": self.label,
            "expression": self.expression,
            "bands": self.bands,
            "start": self.start,
            "end": self.end,
            "max_cloud": self.max_cloud,
            "collection": self.collection,
            "outputs": self.outputs,
            "demo": self.demo,
            "fallback_to_demo": self.fallback_to_demo,
        }


@dataclass
class JobResult:
    rows: list[dict]
    scenes: list[dict]
    stats: dict
    files: dict[str, str]
    overlays: list[dict]
    synthetic: bool


def _find_scenes(params: JobParams, bbox: list[float], log: Log) -> tuple[list[Scene], bool]:
    """Return (scenes, synthetic). Handles demo mode and the optional offline fallback."""
    if params.demo:
        log("DEMO mode: generating synthetic scenes (no network access).")
        scenes = demo.synth_scenes(params.start_date, params.end_date, params.max_cloud, MAX_SCENES, params.collection)
        return scenes, True

    from src import sentinel_download as sd

    log(f"Searching STAC collection '{params.collection}' ...")
    try:
        scenes = sd.search_scenes(
            bbox, params.start, params.end, params.collection, params.max_cloud, MAX_SCENES
        )
        return scenes, False
    except sd.NoScenesFound:
        raise
    except Exception as exc:  # network errors, missing deps, API failures
        if not params.fallback_to_demo:
            raise
        log(f"WARNING: live search unavailable ({type(exc).__name__}: {exc}).")
        log("Falling back to DEMO data - results below are SYNTHETIC.")
        scenes = demo.synth_scenes(params.start_date, params.end_date, params.max_cloud, MAX_SCENES, params.collection)
        return scenes, True


def run_job(
    params: JobParams,
    fields: list[Field],
    out_dir: Path,
    log: Log,
    progress: Progress | None = None,
) -> JobResult:
    progress = progress or (lambda _f: None)
    out_dir.mkdir(parents=True, exist_ok=True)

    log(f"Index: {params.label} = {params.expression}")
    log(f"Input: {len(fields)} polygon(s); bands required: {', '.join(params.bands)}")
    bbox = get_bbox(fields)
    log(f"Area of interest (lon/lat): {', '.join(f'{v:.4f}' for v in bbox)}")

    scenes, synthetic = _find_scenes(params, bbox, log)
    if not scenes:
        raise JobParamsError(
            f"No scenes matched the filters ({params.start}..{params.end}, cloud < {params.max_cloud}%)."
        )
    log(f"Scenes to process: {len(scenes)}")

    vmin, vmax = index_range(params.index)
    want_png = "png" in params.outputs
    rows: list[dict] = []
    overlays: list[dict] = []
    preview_dir = out_dir / "previews"

    for n, scene in enumerate(scenes, start=1):
        log(f"[{n}/{len(scenes)}] {scene.image_date}  {scene.scene_id}  (cloud {scene.cloud_cover}%)")
        if synthetic:
            grid = demo.demo_grid(bbox)
            bands = demo.synth_bands(scene, fields, grid, params.bands)
        else:
            from src import sentinel_download as sd

            bands, grid = sd.load_bands(scene, bbox, params.bands, params.collection)

        values = compute_index(params.expression, bands)
        n_valid = int(np.isfinite(values).sum())
        log(f"    raster {grid.width}x{grid.height}px, valid pixels: {n_valid}")

        for f in fields:
            geom = reproject_geometry(f.geometry, "EPSG:4326", grid.crs)
            s = zonal_stats(values, grid, geom)
            rows.append(
                {
                    "field_id": f.field_id,
                    "image_date": scene.image_date,
                    "index_name": params.label,
                    "mean_value": s["mean"],
                    "min_value": s["min"],
                    "max_value": s["max"],
                    "std_value": s["std"],
                    "pixel_count": s["pixel_count"],
                    "scene_id": scene.scene_id,
                    "cloud_cover": scene.cloud_cover,
                }
            )

        if want_png:
            png = save_preview_png(values, vmin, vmax, preview_dir / f"{scene.scene_id}.png")
            overlays.append(
                {
                    "scene_id": scene.scene_id,
                    "image_date": scene.image_date,
                    "cloud_cover": scene.cloud_cover,
                    "file": png.name,
                    "bounds": _lonlat_bounds(grid),
                    "vmin": vmin,
                    "vmax": vmax,
                }
            )
        progress(n / len(scenes))

    results = pd.DataFrame(rows, columns=RESULT_COLUMNS)
    scenes_df = pd.DataFrame(
        [
            {
                "scene_id": s.scene_id,
                "image_date": s.image_date,
                "cloud_cover": s.cloud_cover,
                "mgrs_tile": s.extra.get("mgrs_tile"),
                "synthetic": synthetic,
            }
            for s in scenes
        ]
    )

    files: dict[str, str] = {}
    meta = {**params.to_dict(), "synthetic": synthetic, "polygons": len(fields)}
    if "csv" in params.outputs:
        files["csv"] = save_csv(results, out_dir / "results.csv").name
        log("Wrote results.csv")
    if "sqlite" in params.outputs:
        files["sqlite"] = save_sqlite(results, scenes_df, meta, out_dir / "results.sqlite").name
        log("Wrote results.sqlite (tables: results, scenes, job_meta)")
    if want_png:
        files["png"] = "previews"
        log(f"Wrote {len(overlays)} preview PNG(s)")

    valid = results["mean_value"].dropna()
    stats = {
        "rows": int(len(results)),
        "fields": int(results["field_id"].nunique()),
        "scenes": int(len(scenes)),
        "mean": round(float(valid.mean()), 4) if len(valid) else None,
        "min": round(float(valid.min()), 4) if len(valid) else None,
        "max": round(float(valid.max()), 4) if len(valid) else None,
    }
    records = results.astype(object).where(results.notna(), None).to_dict("records")
    return JobResult(
        rows=records,
        scenes=scenes_df.to_dict("records"),
        stats=stats,
        files=files,
        overlays=overlays,
        synthetic=synthetic,
    )


def _lonlat_bounds(grid: Grid) -> list[list[float]]:
    """Leaflet-style [[south, west], [north, east]] for the raster extent."""
    minx, miny, maxx, maxy = grid.bounds
    if grid.crs.upper() not in {"EPSG:4326", "OGC:CRS84"}:
        from pyproj import Transformer

        tr = Transformer.from_crs(grid.crs, "EPSG:4326", always_xy=True)
        (minx, maxx), (miny, maxy) = tr.transform([minx, maxx], [miny, maxy])
    return [[miny, minx], [maxy, maxx]]
