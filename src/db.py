"""Result persistence: CSV and SQLite exports."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pandas as pd

RESULT_COLUMNS = [
    "field_id",
    "image_date",
    "index_name",
    "mean_value",
    "min_value",
    "max_value",
    "std_value",
    "pixel_count",
    "scene_id",
    "cloud_cover",
]


def save_csv(results: pd.DataFrame, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    results.to_csv(path, index=False, encoding="utf-8")
    return path


def save_sqlite(
    results: pd.DataFrame,
    scenes: pd.DataFrame,
    meta: dict[str, object],
    path: Path,
) -> Path:
    """Write ``results``, ``scenes`` and ``job_meta`` tables (replacing any existing file)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    meta_df = pd.DataFrame([{"key": k, "value": str(v)} for k, v in meta.items()])
    # sqlite3's own context manager only commits; closing() releases the file handle
    # (required on Windows, where an open handle blocks deletion).
    with closing(sqlite3.connect(path)) as conn:
        results.to_sql("results", conn, index=False)
        scenes.to_sql("scenes", conn, index=False)
        meta_df.to_sql("job_meta", conn, index=False)
        conn.execute("CREATE INDEX idx_results_field ON results(field_id, image_date)")
        conn.commit()
    return path


def read_results(path: Path) -> pd.DataFrame:
    with closing(sqlite3.connect(path)) as conn:
        return pd.read_sql("SELECT * FROM results", conn)
