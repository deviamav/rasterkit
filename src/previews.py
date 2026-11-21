"""Preview rendering: colorize an index raster into a transparent PNG.

Uses a tiny pure NumPy + zlib PNG encoder, so previews need no matplotlib/Pillow.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

import numpy as np

# ColorBrewer RdYlGn (10 classes) - low (red) to high (green).
_STOPS = np.array(
    [
        (165, 0, 38),
        (215, 48, 39),
        (244, 109, 67),
        (253, 174, 97),
        (254, 224, 139),
        (217, 239, 139),
        (166, 217, 106),
        (102, 189, 99),
        (26, 152, 80),
        (0, 104, 55),
    ],
    dtype="float32",
)


def colorize(values: np.ndarray, vmin: float, vmax: float) -> np.ndarray:
    """Map values to an RGBA uint8 image; NaN becomes fully transparent."""
    span = max(vmax - vmin, 1e-9)
    t = np.clip((np.nan_to_num(values, nan=vmin) - vmin) / span, 0, 1)
    xp = np.linspace(0, 1, len(_STOPS))
    rgba = np.empty(values.shape + (4,), dtype="uint8")
    for ch in range(3):
        rgba[..., ch] = np.interp(t, xp, _STOPS[:, ch]).astype("uint8")
    rgba[..., 3] = np.where(np.isfinite(values), 230, 0).astype("uint8")
    return rgba


def _chunk(tag: bytes, data: bytes) -> bytes:
    body = tag + data
    return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)


def encode_png(rgba: np.ndarray) -> bytes:
    h, w, _ = rgba.shape
    raw = np.concatenate([np.zeros((h, 1), dtype="uint8"), rgba.reshape(h, w * 4)], axis=1).tobytes()
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
        + _chunk(b"IDAT", zlib.compress(raw, 6))
        + _chunk(b"IEND", b"")
    )


def save_preview_png(values: np.ndarray, vmin: float, vmax: float, out_path: Path, scale: int = 2) -> Path:
    """Render ``values`` to a PNG (upscaled with nearest-neighbour for small rasters)."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    rgba = colorize(values, vmin, vmax)
    if scale > 1 and max(rgba.shape[:2]) * scale <= 2048:
        rgba = rgba.repeat(scale, axis=0).repeat(scale, axis=1)
    out_path.write_bytes(encode_png(rgba))
    return out_path
