"""DEMO_MODE: deterministic synthetic scenes and band rasters.

No network, no satellite data. Synthetic surface reflectance is derived from a
per-pixel "vegetation vigor" field with crop-like seasonal curves, so the real
index/zonal-statistics code runs on it unchanged (including custom formulas).
"""

from __future__ import annotations

import hashlib
from datetime import date, timedelta

import numpy as np
import shapely

from src.fields import Field
from src.raster import Grid, Scene

REVISIT_DAYS = 5  # Sentinel-2 constellation revisit


def _seed(*parts: object) -> int:
    digest = hashlib.sha256("|".join(map(str, parts)).encode()).digest()
    return int.from_bytes(digest[:8], "little")


def synth_scenes(start: date, end: date, max_cloud: float, limit: int, collection: str = "demo") -> list[Scene]:
    """Pseudo-acquisitions every 5 days; filtered by cloud cover, evenly thinned to ``limit``."""
    candidates: list[Scene] = []
    day = start
    while day <= end:
        rng = np.random.default_rng(_seed("scene", day.isoformat()))
        cloud = float(np.round(rng.beta(0.9, 2.2) * 80, 1))
        if cloud < max_cloud:
            candidates.append(
                Scene(
                    scene_id=f"DEMO_{collection.upper().replace('-', '')}_{day:%Y%m%d}",
                    image_date=day.isoformat(),
                    cloud_cover=cloud,
                    extra={"mgrs_tile": "DEMO", "synthetic": True},
                )
            )
        day += timedelta(days=REVISIT_DAYS)
    if len(candidates) > limit:
        idx = np.unique(np.linspace(0, len(candidates) - 1, limit).round().astype(int))
        candidates = [candidates[i] for i in idx]
    return candidates


def demo_grid(bbox: list[float], width: int = 320) -> Grid:
    minx, miny, maxx, maxy = bbox
    lat = (miny + maxy) / 2
    aspect = (maxy - miny) / max((maxx - minx) * np.cos(np.radians(lat)), 1e-9)
    height = int(np.clip(round(width * aspect), 64, 480))
    return Grid.from_bbox(bbox, width, height)


def _smooth_noise(shape: tuple[int, int], rng: np.random.Generator, cell: int = 14) -> np.ndarray:
    """Cheap spatially-correlated noise in [-1, 1] (bilinear upsample of a coarse grid)."""
    h, w = shape
    gh, gw = h // cell + 2, w // cell + 2
    coarse = rng.uniform(-1, 1, (gh, gw))
    ys = np.linspace(0, gh - 1.001, h)
    xs = np.linspace(0, gw - 1.001, w)
    y0, x0 = ys.astype(int), xs.astype(int)
    fy, fx = (ys - y0)[:, None], (xs - x0)[None, :]
    a = coarse[np.ix_(y0, x0)]
    b = coarse[np.ix_(y0, x0 + 1)]
    c = coarse[np.ix_(y0 + 1, x0)]
    d = coarse[np.ix_(y0 + 1, x0 + 1)]
    return a * (1 - fy) * (1 - fx) + b * (1 - fy) * fx + c * fy * (1 - fx) + d * fy * fx


def _seasonal(doy: int, peak: float, width: float) -> float:
    return float(np.exp(-0.5 * ((doy - peak) / width) ** 2))


def synth_vigor(scene: Scene, fields: list[Field], grid: Grid) -> np.ndarray:
    """Per-pixel vegetation vigor in [0, 1] for a scene date."""
    doy = date.fromisoformat(scene.image_date).timetuple().tm_yday
    shape = (grid.height, grid.width)

    bg_rng = np.random.default_rng(_seed("bg", grid.x0, grid.y0))
    vigor = 0.22 + 0.12 * _seasonal(doy, 190, 60) + 0.05 * _smooth_noise(shape, bg_rng, 24)

    xx, yy = grid.centers(slice(0, grid.height), slice(0, grid.width))
    scene_rng = np.random.default_rng(_seed("scene-noise", scene.scene_id))
    for f in fields:
        rng = np.random.default_rng(_seed("field", f.field_id))
        base = rng.uniform(0.12, 0.28)
        amp = rng.uniform(0.40, 0.70)
        peak = rng.uniform(165, 225)
        width = rng.uniform(30, 55)
        level = base + amp * _seasonal(doy, peak, width)

        mask = shapely.contains_xy(f.geometry, xx, yy)
        if not mask.any():
            continue
        texture = 0.07 * _smooth_noise(shape, rng, 10)  # fixed within-field pattern
        wobble = scene_rng.normal(0, 0.012)  # small scene-to-scene variation
        vigor = np.where(mask, level + texture + wobble, vigor)
    return np.clip(vigor, 0.0, 1.0).astype("float32")


def synth_bands(scene: Scene, fields: list[Field], grid: Grid, needed: list[str]) -> dict[str, np.ndarray]:
    """Synthetic surface reflectance (0..1) for the requested bands."""
    v = synth_vigor(scene, fields, grid)
    inv = 1.0 - v
    base = {
        "B02": 0.035 + 0.03 * inv,
        "B03": 0.06 + 0.05 * inv,
        "B04": 0.04 + 0.16 * inv**1.2,
        "B05": 0.08 + 0.10 * inv + 0.05 * v,
        "B06": 0.12 + 0.20 * v,
        "B07": 0.14 + 0.28 * v,
        "B08": 0.18 + 0.30 * v,
        "B8A": 0.18 + 0.29 * v,
        "B09": 0.17 + 0.26 * v,
        "B11": 0.30 - 0.14 * v,
        "B12": 0.22 - 0.12 * v,
    }
    base["B01"] = base["B02"] * 1.1
    rng = np.random.default_rng(_seed("noise", scene.scene_id))
    out: dict[str, np.ndarray] = {}
    for band in needed:
        arr = base[band] * (1 + rng.normal(0, 0.015, v.shape))
        out[band] = np.clip(arr, 0.001, 1.0).astype("float32")
    return out
