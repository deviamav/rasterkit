"""Lightweight raster grid helpers and zonal statistics (NumPy + Shapely only).

These helpers work for both real rasters (read with rasterio) and the synthetic
rasters produced in demo mode, so the zonal-statistics code path is identical.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import partial

import numpy as np
import shapely
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform as shp_transform


@dataclass(frozen=True)
class Grid:
    """North-up raster grid: top-left corner, pixel size and CRS."""

    x0: float  # left edge
    y0: float  # top edge
    dx: float  # pixel width (> 0)
    dy: float  # pixel height (> 0, rows go downward)
    height: int
    width: int
    crs: str = "EPSG:4326"

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        """(minx, miny, maxx, maxy) in the grid CRS."""
        return (self.x0, self.y0 - self.dy * self.height, self.x0 + self.dx * self.width, self.y0)

    @classmethod
    def from_bbox(cls, bbox: list[float], width: int, height: int, crs: str = "EPSG:4326") -> "Grid":
        minx, miny, maxx, maxy = bbox
        return cls(minx, maxy, (maxx - minx) / width, (maxy - miny) / height, height, width, crs)

    def centers(self, rows: slice, cols: slice) -> tuple[np.ndarray, np.ndarray]:
        r = np.arange(rows.start, rows.stop)
        c = np.arange(cols.start, cols.stop)
        xs = self.x0 + (c + 0.5) * self.dx
        ys = self.y0 - (r + 0.5) * self.dy
        return np.meshgrid(xs, ys)


@dataclass
class Scene:
    """A single acquisition (real STAC item or synthetic demo scene)."""

    scene_id: str
    image_date: str  # YYYY-MM-DD
    cloud_cover: float | None = None
    extra: dict = field(default_factory=dict)


def reproject_geometry(geom: BaseGeometry, src_crs: str, dst_crs: str) -> BaseGeometry:
    """Reproject a Shapely geometry between CRSs (pyproj is imported lazily)."""
    if src_crs.upper() == dst_crs.upper():
        return geom
    from pyproj import Transformer

    tr = Transformer.from_crs(src_crs, dst_crs, always_xy=True)
    return shp_transform(partial(tr.transform), geom)


def zonal_stats(values: np.ndarray, grid: Grid, geom: BaseGeometry) -> dict:
    """Summary statistics of ``values`` for pixels whose centre lies inside ``geom``.

    Falls back to "any intersection" for polygons smaller than one pixel so tiny
    parcels still get a value. ``geom`` must already be in the grid CRS.
    """
    empty = {"mean": None, "min": None, "max": None, "std": None, "pixel_count": 0}
    minx, miny, maxx, maxy = geom.bounds
    c0 = max(int(np.floor((minx - grid.x0) / grid.dx)), 0)
    c1 = min(int(np.ceil((maxx - grid.x0) / grid.dx)), grid.width)
    r0 = max(int(np.floor((grid.y0 - maxy) / grid.dy)), 0)
    r1 = min(int(np.ceil((grid.y0 - miny) / grid.dy)), grid.height)
    if c1 <= c0 or r1 <= r0:
        return empty

    rows, cols = slice(r0, r1), slice(c0, c1)
    xx, yy = grid.centers(rows, cols)
    mask = shapely.contains_xy(geom, xx, yy)
    if not mask.any():
        mask = shapely.intersects_xy(geom, xx, yy)
    sample = values[rows, cols][mask]
    sample = sample[np.isfinite(sample)]
    if sample.size == 0:
        return empty
    return {
        "mean": round(float(sample.mean()), 6),
        "min": round(float(sample.min()), 6),
        "max": round(float(sample.max()), 6),
        "std": round(float(sample.std()), 6),
        "pixel_count": int(sample.size),
    }
