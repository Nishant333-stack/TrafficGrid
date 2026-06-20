from __future__ import annotations

from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def project_root() -> Path:
    """Repository root (contains main.py and requirements.txt)."""

    here = Path(__file__).resolve()
    for candidate in here.parents:
        if (candidate / "main.py").is_file() and (candidate / "requirements.txt").is_file():
            return candidate
    return here.parents[2]


PROJECT_ROOT = project_root()
FRONTEND_DIST = PROJECT_ROOT / "dist"
