from __future__ import annotations

import os
from pathlib import Path

from backend.config.paths import PROJECT_ROOT


def load_project_env(env_path: str | Path | None = None) -> None:
    """Load simple KEY=VALUE pairs from .env without overriding shell variables."""

    path = Path(env_path) if env_path else PROJECT_ROOT / ".env"
    if not path.exists():
        return

    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue

        key, value = stripped.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key.removeprefix("export ").strip()
        if not key or key in os.environ:
            continue

        value = value.strip()
        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in {"'", '"'}
        ):
            value = value[1:-1]
        os.environ[key] = value
