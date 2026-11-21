"""Vector input handling: parse/validate GeoJSON polygons and build demo geometries."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import numpy as np
from shapely import force_2d
from shapely.geometry import MultiPolygon, Polygon, mapping, shape
from shapely.validation import make_valid

from src.config import MAX_FEATURES

NAME_KEYS = ("name", "field_name", "field_id", "id", "NAME", "Name", "ID")


class GeometryInputError(ValueError):
    """Raised when uploaded vector data cannot be used."""


@dataclass
class Field:
    field_id: str
    geometry: Polygon | MultiPolygon  # WGS84 (EPSG:4326), 2D


def _extract_features(data: Any) -> list[dict]:
    if not isinstance(data, dict) or "type" not in data:
        raise GeometryInputError("Input is not valid GeoJSON (missing 'type').")
    kind = data["type"]
    if kind == "FeatureCollection":
        return list(data.get("features") or [])
    if kind == "Feature":
        return [data]
    if kind in {"Polygon", "MultiPolygon"}:
        return [{"type": "Feature", "properties": {}, "geometry": data}]
    raise GeometryInputError(f"Unsupported GeoJSON type '{kind}'. Use a FeatureCollection of polygons.")


def parse_geojson(data: Any) -> list[Field]:
    """Parse a GeoJSON dict/str/bytes into validated polygon fields."""
    if isinstance(data, (str, bytes, bytearray)):
        try:
            data = json.loads(data)
        except json.JSONDecodeError as exc:
            raise GeometryInputError(f"File is not valid JSON: {exc.msg}.") from exc

    features = _extract_features(data)
    if not features:
        raise GeometryInputError("GeoJSON contains no features.")
    if len(features) > MAX_FEATURES:
        raise GeometryInputError(f"Too many features ({len(features)}); the limit is {MAX_FEATURES}.")

    fields: list[Field] = []
    seen: set[str] = set()
    for idx, feat in enumerate(features, start=1):
        geom_dict = (feat or {}).get("geometry")
        if not geom_dict:
            continue
        try:
            geom = shape(geom_dict)
        except Exception as exc:  # shapely raises various types
            raise GeometryInputError(f"Feature {idx}: invalid geometry ({exc}).") from exc
        if geom.geom_type not in {"Polygon", "MultiPolygon"}:
            continue  # points/lines are ignored: we aggregate over areas
        geom = force_2d(geom)
        if not geom.is_valid:
            geom = make_valid(geom)
            polys = [g for g in getattr(geom, "geoms", [geom]) if g.geom_type in {"Polygon", "MultiPolygon"}]
            if not polys:
                continue
            geom = polys[0] if len(polys) == 1 else MultiPolygon(
                [p for g in polys for p in (g.geoms if g.geom_type == "MultiPolygon" else [g])]
            )
        if geom.is_empty:
            continue
        minx, miny, maxx, maxy = geom.bounds
        if not (-180 <= minx <= maxx <= 180 and -90 <= miny <= maxy <= 90):
            raise GeometryInputError(
                f"Feature {idx}: coordinates are outside WGS84 range. GeoJSON must use EPSG:4326 (lon/lat)."
            )

        props = feat.get("properties") or {}
        name = next((str(props[k]) for k in NAME_KEYS if props.get(k) not in (None, "")), f"field_{idx}")
        base, n = name, 2
        while name in seen:
            name, n = f"{base}_{n}", n + 1
        seen.add(name)
        fields.append(Field(field_id=name, geometry=geom))

    if not fields:
        raise GeometryInputError("No polygon geometries found in the input.")
    return fields


def fields_to_geojson(fields: list[Field]) -> dict:
    return {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "properties": {"field_id": f.field_id}, "geometry": mapping(f.geometry)}
            for f in fields
        ],
    }


def get_bbox(fields: list[Field], pad: float = 0.002) -> list[float]:
    """Bounding box [minx, miny, maxx, maxy] with a small padding (degrees)."""
    bounds = np.array([f.geometry.bounds for f in fields])
    return [
        float(bounds[:, 0].min() - pad),
        float(bounds[:, 1].min() - pad),
        float(bounds[:, 2].max() + pad),
        float(bounds[:, 3].max() + pad),
    ]


def demo_fields(n: int = 6, origin: tuple[float, float] = (-93.65, 42.02), seed: int = 7) -> list[Field]:
    """Deterministic synthetic parcels (generic farmland-like polygons)."""
    rng = np.random.default_rng(seed)
    cols = 3
    cell_w, cell_h = 0.012, 0.009
    fields: list[Field] = []
    for i in range(n):
        r, c = divmod(i, cols)
        cx = origin[0] + c * cell_w * 1.25
        cy = origin[1] - r * cell_h * 1.3
        w = cell_w * rng.uniform(0.7, 0.95) / 2
        h = cell_h * rng.uniform(0.7, 0.95) / 2
        jitter = lambda: rng.uniform(-0.12, 0.12)  # noqa: E731
        coords = [
            (cx - w * (1 + jitter()), cy - h * (1 + jitter())),
            (cx + w * (1 + jitter()), cy - h * (1 + jitter())),
            (cx + w * (1 + jitter()), cy + h * (1 + jitter())),
            (cx - w * (1 + jitter()), cy + h * (1 + jitter())),
        ]
        fields.append(Field(field_id=f"demo_parcel_{i + 1:02d}", geometry=Polygon(coords)))
    return fields


def demo_geojson() -> dict:
    return fields_to_geojson(demo_fields())


def _merge_fields(batches: list[list[Field]]) -> list[Field]:
    """Merge field lists with unique IDs (supports multi-file ZIP uploads)."""
    merged: list[Field] = []
    seen: set[str] = set()
    for batch in batches:
        for field in batch:
            name, n = field.field_id, 2
            while name in seen:
                name, n = f"{field.field_id}_{n}", n + 1
            seen.add(name)
            merged.append(Field(field_id=name, geometry=field.geometry))
    if len(merged) > MAX_FEATURES:
        raise GeometryInputError(f"Too many features ({len(merged)}); the limit is {MAX_FEATURES}.")
    return merged


def parse_upload_bytes(payload: bytes, filename: str = "") -> list[Field]:
    """Accept raw GeoJSON/JSON bytes or a ZIP that contains one or more GeoJSON files.

    ZIP support is for bulk field packs (many parcels). Shapefiles are not parsed
    here — convert to GeoJSON first, or put several ``.geojson`` files in one ZIP.
    """
    name = (filename or "").lower()
    data = payload
    if name.endswith(".zip") or data[:2] == b"PK":
        import io
        import zipfile

        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as exc:
            raise GeometryInputError("File looks like a ZIP but is not a valid archive.") from exc

        batches: list[list[Field]] = []
        members = [
            m
            for m in zf.namelist()
            if not m.endswith("/")
            and not m.startswith("__MACOSX/")
            and m.lower().endswith((".geojson", ".json"))
        ]
        if not members:
            raise GeometryInputError(
                "ZIP contains no .geojson / .json files. Add GeoJSON FeatureCollections "
                "(many polygons / fields) and upload again."
            )
        for member in members:
            raw = zf.read(member)
            batches.append(parse_geojson(raw))
        return _merge_fields(batches)

    return parse_geojson(data)
