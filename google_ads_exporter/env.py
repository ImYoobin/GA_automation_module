"""Minimal .env loader (no third-party dependency)."""

from __future__ import annotations

import os
from pathlib import Path


def _strip_wrapping_quotes(value: str) -> str:
    text = value.strip()
    if len(text) >= 2 and ((text[0] == text[-1]) and text[0] in {"'", '"'}):
        return text[1:-1]
    return text


def load_env_file(path: str | Path, *, override: bool = False, logger=None) -> Path | None:
    env_path = Path(path).expanduser().resolve()
    if not env_path.exists():
        if logger:
            logger.info(".env file not found; skipping path=%s", env_path)
        return None

    loaded = 0
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export ") :].strip()
        if "=" not in stripped:
            continue

        key, raw_value = stripped.split("=", 1)
        key = key.strip()
        if not key:
            continue

        value = _strip_wrapping_quotes(raw_value)
        if not override and key in os.environ:
            continue
        os.environ[key] = value
        loaded += 1

    if logger:
        logger.info(".env loaded path=%s loaded_keys=%s", env_path, loaded)
    return env_path

