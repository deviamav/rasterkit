"""Central configuration for RasterKit.

Every value can be overridden with an environment variable so the app has no
hardcoded machine-specific paths.
"""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _env_path(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    return Path(value).expanduser().resolve() if value else default


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


# Working directories (created lazily, git-ignored).
DATA_DIR = _env_path("RASTERKIT_DATA_DIR", PROJECT_ROOT / "data")
CACHE_DIR = DATA_DIR / "cache"  # downloaded band crops
JOBS_DIR = _env_path("RASTERKIT_JOBS_DIR", PROJECT_ROOT / "outputs" / "jobs")

# STAC / Planetary Computer (public data, no API key required).
STAC_URL = os.environ.get("RASTERKIT_STAC_URL", "https://planetarycomputer.microsoft.com/api/stac/v1")
DATA_API = os.environ.get("RASTERKIT_DATA_API", "https://planetarycomputer.microsoft.com/api/data/v1")
DEFAULT_COLLECTION = "sentinel-2-l2a"
DEFAULT_MAX_CLOUD_COVER = 40
HTTP_TIMEOUT = int(os.environ.get("RASTERKIT_HTTP_TIMEOUT", "300"))

# When true, the UI starts with demo mode enabled and jobs never touch the network.
DEMO_MODE = _env_bool("DEMO_MODE", False)

# Upload / safety limits.
MAX_UPLOAD_MB = int(os.environ.get("RASTERKIT_MAX_UPLOAD_MB", "50"))
MAX_FEATURES = int(os.environ.get("RASTERKIT_MAX_FEATURES", "2000"))
MAX_SCENES = int(os.environ.get("RASTERKIT_MAX_SCENES", "12"))

# Server.
HOST = os.environ.get("RASTERKIT_HOST", "127.0.0.1")
PORT = int(os.environ.get("RASTERKIT_PORT", "5000"))
