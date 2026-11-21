"""Offline test-suite (stdlib unittest): python -m unittest discover -s tests -v"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

import numpy as np

import os

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("RASTERKIT_JOBS_DIR", tempfile.mkdtemp(prefix="rasterkit-test-"))

from src import indices  # noqa: E402
from src.fields import GeometryInputError, demo_geojson, parse_geojson  # noqa: E402
from src.pipeline import JobParams, JobParamsError, run_job  # noqa: E402
from src.raster import Grid, zonal_stats  # noqa: E402


class IndexTests(unittest.TestCase):
    def test_ndvi_matches_formula(self):
        b = {"B04": np.array([[0.1, 0.2]], "float32"), "B08": np.array([[0.5, 0.2]], "float32")}
        out = indices.compute_index(indices.INDICES["NDVI"].expression, b)
        np.testing.assert_allclose(out, [[(0.5 - 0.1) / 0.6, 0.0]], rtol=1e-5)

    def test_division_by_zero_gives_nan(self):
        z = np.zeros((1, 1), "float32")
        out = indices.compute_index("(B08 - B04) / (B08 + B04)", {"B04": z, "B08": z})
        self.assertTrue(np.isnan(out).all())

    def test_custom_formula_is_case_insensitive(self):
        self.assertEqual(indices.bands_in_formula("(b08-b04)/(b08+b04)"), ["B04", "B08"])

    def test_unsafe_expressions_rejected(self):
        for bad in ["__import__('os').system('echo hi')", "B04.real", "B04 if B08 else B02", "open('x')", "[B04]", "B99 + 1", "", "1 + 2"]:
            with self.subTest(expr=bad), self.assertRaises(indices.FormulaError):
                indices.bands_in_formula(bad)

    def test_huge_power_does_not_hang(self):
        z = np.ones((1, 1), "float32")
        with self.assertRaises(indices.FormulaError):
            indices.compute_index("B04 + 9 ** 9 ** 9 ** 9", {"B04": z})


class GeometryTests(unittest.TestCase):
    def test_demo_geojson_roundtrip(self):
        fields = parse_geojson(demo_geojson())
        self.assertEqual(len(fields), 6)

    def test_rejects_projected_coordinates(self):
        gj = {"type": "Polygon", "coordinates": [[[500000, 5000000], [500100, 5000000], [500100, 5000100], [500000, 5000000]]]}
        with self.assertRaises(GeometryInputError):
            parse_geojson(gj)

    def test_rejects_garbage(self):
        with self.assertRaises(GeometryInputError):
            parse_geojson("not json")
        with self.assertRaises(GeometryInputError):
            parse_geojson({"type": "FeatureCollection", "features": []})


class ZonalTests(unittest.TestCase):
    def test_zonal_mean(self):
        grid = Grid.from_bbox([0, 0, 10, 10], 10, 10)
        values = np.zeros((10, 10), "float32")
        values[:, 5:] = 1.0
        left = parse_geojson({"type": "Polygon", "coordinates": [[[0, 0], [4, 0], [4, 10], [0, 10], [0, 0]]]})[0]
        right = parse_geojson({"type": "Polygon", "coordinates": [[[6, 0], [10, 0], [10, 10], [6, 10], [6, 0]]]})[0]
        self.assertEqual(zonal_stats(values, grid, left.geometry)["mean"], 0.0)
        self.assertEqual(zonal_stats(values, grid, right.geometry)["mean"], 1.0)


class PipelineTests(unittest.TestCase):
    def _params(self, **kw):
        base = {"index": "NDVI", "start": "2026-06-01", "end": "2026-08-31", "max_cloud": 60,
                "outputs": ["csv", "sqlite", "png"], "demo": True}
        base.update(kw)
        return JobParams.from_dict(base)

    def test_demo_pipeline_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            res = run_job(self._params(), parse_geojson(demo_geojson()), out, log=lambda _m: None)
            self.assertTrue(res.synthetic)
            self.assertGreater(len(res.rows), 0)
            self.assertTrue((out / "results.csv").exists())
            self.assertTrue(list((out / "previews").glob("*.png")))
            conn = sqlite3.connect(out / "results.sqlite")
            try:
                n = conn.execute("SELECT COUNT(*) FROM results").fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(n, len(res.rows))
            # PNG signature
            png = next((out / "previews").glob("*.png")).read_bytes()
            self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
            means = [r["mean_value"] for r in res.rows if r["mean_value"] is not None]
            self.assertTrue(all(-1 <= m <= 1 for m in means))

    def test_demo_is_deterministic(self):
        fields = parse_geojson(demo_geojson())
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            r1 = run_job(self._params(outputs=["csv"]), fields, Path(a), log=lambda _m: None)
            r2 = run_job(self._params(outputs=["csv"]), fields, Path(b), log=lambda _m: None)
            self.assertEqual(r1.rows, r2.rows)

    def test_custom_formula_in_demo(self):
        p = self._params(index="custom", custom_formula="(B08 - B11) / (B08 + B11)", outputs=["csv"])
        with tempfile.TemporaryDirectory() as tmp:
            res = run_job(p, parse_geojson(demo_geojson()), Path(tmp), log=lambda _m: None)
        self.assertEqual({r["index_name"] for r in res.rows}, {"CUSTOM"})

    def test_invalid_params(self):
        for kw in ({"start": "2026-09-01", "end": "2026-08-01"}, {"max_cloud": 150}, {"outputs": ["xlsx"]},
                   {"index": "custom", "custom_formula": "B04 +"}, {"collection": "../../etc"}):
            with self.subTest(kw=kw), self.assertRaises(JobParamsError):
                self._params(**kw)


class WebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import web.app as webapp  # type: ignore  # noqa: F401
        cls.app = webapp.app
        cls.client = webapp.app.test_client()

    def test_index_and_config(self):
        self.assertEqual(self.client.get("/").status_code, 200)
        cfg = self.client.get("/api/config").get_json()
        self.assertIn("NDVI", [i["key"] for i in cfg["indices"]])

    def test_validate_rejects_bad_input(self):
        r = self.client.post("/api/validate", json={"type": "Point", "coordinates": [0, 0]})
        self.assertEqual(r.status_code, 422)

    def test_job_end_to_end(self):
        gj = self.client.get("/api/demo-geometry").get_json()
        body = {"geojson": gj, "params": {"index": "NDVI", "start": "2026-06-01", "end": "2026-08-01",
                                           "max_cloud": 60, "outputs": ["csv", "png"], "demo": True}}
        r = self.client.post("/api/jobs", json=body)
        self.assertEqual(r.status_code, 202)
        job_id = r.get_json()["job_id"]
        for _ in range(200):
            snap = self.client.get(f"/api/jobs/{job_id}").get_json()
            if snap["status"] in {"done", "failed"}:
                break
            time.sleep(0.1)
        self.assertEqual(snap["status"], "done", snap.get("error"))
        self.assertTrue(snap["summary"]["synthetic"])
        self.assertGreater(len(self.client.get(f"/api/jobs/{job_id}/results").get_json()), 0)
        with self.client.get(f"/api/jobs/{job_id}/download/csv") as resp:
            self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.client.get(f"/api/jobs/{job_id}/download/sqlite").status_code, 404)
        ov = snap["summary"]["overlays"][0]
        with self.client.get(f"/api/jobs/{job_id}/preview/{ov['file']}") as resp:
            self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.client.get(f"/api/jobs/{job_id}/preview/..%2F..%2Fapp.py").status_code, 404)
        self.assertEqual(self.client.get("/api/jobs/not-a-job").status_code, 404)


if __name__ == "__main__":
    unittest.main()
