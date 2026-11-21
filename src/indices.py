"""Spectral index registry and a safe band-math expression evaluator.

Custom formulas such as ``(B08-B04)/(B08+B04)`` are parsed with :mod:`ast` and
evaluated against NumPy arrays. Only arithmetic, band names, numeric literals
and a few whitelisted functions are allowed - ``eval`` is never used.
"""

from __future__ import annotations

import ast
import operator
import re
from dataclasses import dataclass
from typing import Callable

import numpy as np

# Sentinel-2 L2A band names accepted in formulas (STAC asset keys).
BANDS = ("B01", "B02", "B03", "B04", "B05", "B06", "B07", "B08", "B8A", "B09", "B11", "B12")

# Surface reflectance scale for Sentinel-2 L2A (processing baseline >= 04.00 also
# applies an offset of -1000; the reflectance conversion is handled in `to_reflectance`).
REFLECTANCE_SCALE = 10000.0
REFLECTANCE_OFFSET = 1000.0


@dataclass(frozen=True)
class IndexDef:
    key: str
    label: str
    expression: str
    description: str
    vmin: float = -0.1
    vmax: float = 0.8


INDICES: dict[str, IndexDef] = {
    "NDVI": IndexDef(
        "NDVI", "NDVI", "(B08 - B04) / (B08 + B04)", "Normalized Difference Vegetation Index", -0.1, 0.9
    ),
    "NDWI": IndexDef(
        "NDWI", "NDWI", "(B03 - B08) / (B03 + B08)", "Normalized Difference Water Index (McFeeters)", -0.6, 0.6
    ),
    "EVI": IndexDef(
        "EVI",
        "EVI",
        "2.5 * (B08 - B04) / (B08 + 6 * B04 - 7.5 * B02 + 1)",
        "Enhanced Vegetation Index",
        -0.1,
        0.9,
    ),
    "NDMI": IndexDef(
        "NDMI", "NDMI", "(B08 - B11) / (B08 + B11)", "Normalized Difference Moisture Index", -0.4, 0.6
    ),
}

CUSTOM_KEY = "CUSTOM"


class FormulaError(ValueError):
    """Raised when a band-math expression is invalid or unsafe."""


_BINOPS: dict[type, Callable] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
}
_UNARYOPS: dict[type, Callable] = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_FUNCS: dict[str, Callable] = {"sqrt": np.sqrt, "abs": np.abs, "log": np.log, "exp": np.exp}

_MAX_EXPR_LEN = 200


def _parse(expression: str) -> ast.Expression:
    expression = (expression or "").strip()
    if not expression:
        raise FormulaError("Formula is empty.")
    if len(expression) > _MAX_EXPR_LEN:
        raise FormulaError(f"Formula is too long (max {_MAX_EXPR_LEN} characters).")
    try:
        return ast.parse(expression.upper().replace("SQRT", "sqrt").replace("ABS", "abs")
                         .replace("LOG", "log").replace("EXP", "exp"), mode="eval")
    except SyntaxError as exc:
        raise FormulaError(f"Syntax error in formula: {exc.msg}") from exc


def _validate(node: ast.AST) -> set[str]:
    """Walk the AST, reject anything outside the whitelist, return used bands."""
    used: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, (ast.Expression, ast.Load)):
            continue
        if isinstance(child, ast.BinOp) and type(child.op) in _BINOPS:
            continue
        if isinstance(child, ast.UnaryOp) and type(child.op) in _UNARYOPS:
            continue
        if isinstance(child, tuple(_BINOPS) + tuple(_UNARYOPS)):
            continue
        if isinstance(child, ast.Constant) and isinstance(child.value, (int, float)) and not isinstance(child.value, bool):
            continue
        if isinstance(child, ast.Name):
            if child.id in BANDS:
                used.add(child.id)
                continue
            if child.id in _FUNCS:
                continue
            raise FormulaError(f"Unknown band or name '{child.id}'. Allowed bands: {', '.join(BANDS)}.")
        if isinstance(child, ast.Call):
            if isinstance(child.func, ast.Name) and child.func.id in _FUNCS and not child.keywords:
                continue
            raise FormulaError("Only sqrt(), abs(), log() and exp() calls are allowed.")
        raise FormulaError(f"Unsupported syntax in formula: {type(child).__name__}.")
    if not used:
        raise FormulaError("Formula must reference at least one band (e.g. B04, B08).")
    return used


def bands_in_formula(expression: str) -> list[str]:
    """Return the sorted list of bands referenced by an expression (validates it)."""
    return sorted(_validate(_parse(expression)))


def _eval(node: ast.AST, env: dict[str, np.ndarray]):
    if isinstance(node, ast.Expression):
        return _eval(node.body, env)
    if isinstance(node, ast.Constant):
        return float(node.value)
    if isinstance(node, ast.Name):
        if node.id not in env:
            raise FormulaError(f"'{node.id}' cannot be used as a value.")
        return env[node.id]
    if isinstance(node, ast.BinOp):
        return _BINOPS[type(node.op)](_eval(node.left, env), _eval(node.right, env))
    if isinstance(node, ast.UnaryOp):
        return _UNARYOPS[type(node.op)](_eval(node.operand, env))
    if isinstance(node, ast.Call):
        return _FUNCS[node.func.id](*[_eval(a, env) for a in node.args])  # type: ignore[union-attr]
    raise FormulaError("Unsupported expression.")  # pragma: no cover


def resolve_expression(index_key: str, custom_formula: str | None = None) -> tuple[str, str]:
    """Map the UI selection to ``(label, expression)``."""
    key = (index_key or "").upper()
    if key == CUSTOM_KEY:
        expression = (custom_formula or "").strip()
        bands_in_formula(expression)  # validate early
        return "CUSTOM", expression
    if key not in INDICES:
        raise FormulaError(f"Unknown index '{index_key}'.")
    return INDICES[key].label, INDICES[key].expression


def compute_index(expression: str, bands: dict[str, np.ndarray]) -> np.ndarray:
    """Evaluate an index expression over band arrays (reflectance, float32).

    Division by zero and invalid operations yield NaN rather than raising.
    """
    tree = _parse(expression)
    used = _validate(tree)
    missing = used - set(bands)
    if missing:
        raise FormulaError(f"Missing band data for: {', '.join(sorted(missing))}.")
    env = {name: np.asarray(arr, dtype="float32") for name, arr in bands.items() if name in used}
    try:
        with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
            result = _eval(tree, env)
    except (OverflowError, ZeroDivisionError) as exc:
        raise FormulaError(f"Arithmetic error in formula: {exc}") from exc
    result = np.asarray(result, dtype="float32")
    result[~np.isfinite(result)] = np.nan
    return result


def to_reflectance(dn: np.ndarray, nodata: float = 0.0, offset: float = 0.0) -> np.ndarray:
    """Convert Sentinel-2 L2A digital numbers to surface reflectance (0..1)."""
    arr = dn.astype("float32")
    valid = arr != nodata
    out = np.where(valid, (arr - offset) / REFLECTANCE_SCALE, np.nan).astype("float32")
    return np.clip(out, 0.0, 1.5)


def index_range(index_key: str) -> tuple[float, float]:
    """Display range used for previews."""
    if index_key in INDICES:
        d = INDICES[index_key]
        return d.vmin, d.vmax
    return -1.0, 1.0


def slugify(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").lower() or "index"
