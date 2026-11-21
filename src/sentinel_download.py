"""Live data access: STAC search + band crops from Microsoft Planetary Computer.

Public collections work without an API key. Heavy geospatial dependencies
(`rasterio`, `pystac-client`, `planetary-computer`, `requests`) are imported
lazily so the app - and DEMO_MODE - run without them.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np

from src.config import CACHE_DIR, DATA_API, HTTP_TIMEOUT, STAC_URL
from src.indices import to_reflectance
from src.raster import Grid, Scene


class LiveDependencyError(RuntimeError):
    """Optional live-mode dependencies are not installed."""


class NoScenesFound(RuntimeError):
    """The STAC search returned no scenes for the given filters."""


def live_dependencies_available() -> bool:
    import importlib.util

    return all(
        importlib.util.find_spec(m) is not None
        for m in ("rasterio", "pystac_client", "planetary_computer", "requests")
    )


def _require_live():
    try:
        import planetary_computer as pc
        import pystac_client
        import rasterio
        import requests
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise LiveDependencyError(
            "Live mode needs extra packages: pip install -r requirements-live.txt "
            "(or enable DEMO mode)."
        ) from exc
    return pc, pystac_client, rasterio, requests


def search_scenes(
    bbox: list[float],
    start: str,
    end: str,
    collection: str,
    max_cloud: float,
    limit: int,
) -> list[Scene]:
    """Search the STAC API; keep the ``limit`` least-cloudy scenes, sorted by date."""
    pc, pystac_client, _, _ = _require_live()
    catalog = pystac_client.Client.open(STAC_URL, modifier=pc.sign_inplace)
    search = catalog.search(
        collections=[collection],
        bbox=bbox,
        datetime=f"{start}/{end}",
        query={"eo:cloud_cover": {"lt": max_cloud}},
    )
    items = list(search.items())
    if not items:
        raise NoScenesFound(
            f"No scenes in '{collection}' for {start}..{end} with cloud cover < {max_cloud}%. "
            "Try a wider date range or higher cloud limit."
        )
    items.sort(key=lambda i: i.properties.get("eo:cloud_cover", 100))
    items = sorted(items[:limit], key=lambda i: i.datetime)
    return [
        Scene(
            scene_id=i.id,
            image_date=i.datetime.strftime("%Y-%m-%d"),
            cloud_cover=i.properties.get("eo:cloud_cover"),
            extra={
                "mgrs_tile": i.properties.get("s2:mgrs_tile"),
                "processing_baseline": i.properties.get("s2:processing_baseline"),
                "collection": collection,
            },
        )
        for i in items
    ]


def _bbox_key(bbox: list[float]) -> str:
    return hashlib.sha1(",".join(f"{v:.6f}" for v in bbox).encode()).hexdigest()[:10]


def download_band_crop(scene: Scene, band: str, bbox: list[float], collection: str) -> Path:
    """Download only the bbox crop of one band (GeoTIFF) via the Data API; cached on disk."""
    _, _, _, requests = _require_live()
    out_dir = CACHE_DIR / collection
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{scene.scene_id}_{band}_{_bbox_key(bbox)}.tif"
    if out_path.exists() and out_path.stat().st_size > 1000:
        return out_path

    bbox_str = ",".join(f"{v:.6f}" for v in bbox)
    url = f"{DATA_API}/item/bbox/{bbox_str}.tif"
    params = {"collection": collection, "item": scene.scene_id, "assets": band}
    tmp = out_path.with_suffix(".part")
    with requests.get(url, params=params, stream=True, timeout=HTTP_TIMEOUT) as resp:
        resp.raise_for_status()
        with open(tmp, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    fh.write(chunk)
    tmp.replace(out_path)
    return out_path


def _processing_offset(scene: Scene, collection: str) -> float:
    """Sentinel-2 L2A baseline >= 04.00 stores reflectance with a +1000 offset."""
    if not collection.startswith("sentinel-2"):
        return 0.0
    try:
        return 1000.0 if float(scene.extra.get("processing_baseline") or 0) >= 4.0 else 0.0
    except (TypeError, ValueError):
        return 0.0


def load_bands(
    scene: Scene, bbox: list[float], bands: list[str], collection: str
) -> tuple[dict[str, np.ndarray], Grid]:
    """Download + read band crops, resampled onto the first band's grid."""
    _, _, rasterio, _ = _require_live()
    from rasterio.enums import Resampling

    offset = _processing_offset(scene, collection)
    arrays: dict[str, np.ndarray] = {}
    grid: Grid | None = None
    for band in bands:
        path = download_band_crop(scene, band, bbox, collection)
        with rasterio.open(path) as src:
            if grid is None:
                t = src.transform
                grid = Grid(t.c, t.f, t.a, -t.e, src.height, src.width, src.crs.to_string() if src.crs else "EPSG:4326")
                data = src.read(1)
            else:
                data = src.read(1, out_shape=(grid.height, grid.width), resampling=Resampling.bilinear)
        if collection.startswith("sentinel-2"):
            arrays[band] = to_reflectance(data, nodata=0.0, offset=offset)
        else:
            arrays[band] = data.astype("float32")
    assert grid is not None
    return arrays, grid
