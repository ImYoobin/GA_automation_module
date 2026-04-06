"""Shared utility helpers."""

from __future__ import annotations

import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path

from .models import AdsAccount

CID_REGEX = re.compile(r"\b(\d{3}-\d{3}-\d{4})\b")
MULTISPACE_REGEX = re.compile(r"\s+")
INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*]+')
RUNTIME_DIR_ENV = "GOOGLE_ADS_RUNTIME_DIR"
OUTPUT_DIR_ENV = "GOOGLE_ADS_OUTPUT_DIR"
LOGS_DIR_ENV = "GOOGLE_ADS_LOGS_DIR"
USER_DATA_DIR_ENV = "GOOGLE_ADS_USER_DATA_DIR"


def _env_path(name: str) -> Path | None:
    value = str(os.getenv(name, "")).strip()
    if not value:
        return None
    return Path(value).expanduser().resolve()


def get_runtime_dir() -> Path:
    """Return directory where outputs/logs should be written."""
    env_runtime = _env_path(RUNTIME_DIR_ENV)
    if env_runtime:
        return env_runtime
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    # In script mode keep runtime folders next to project entrypoint (main.py).
    return Path(__file__).resolve().parent.parent


def get_run_output_dir() -> Path:
    """
    Return per-run-date output directory under runtime dir.
    Example: <runtime_dir>/20260331
    """
    explicit_output_dir = _env_path(OUTPUT_DIR_ENV)
    if explicit_output_dir:
        output_dir = explicit_output_dir
    else:
        run_date = datetime.now().strftime("%Y%m%d")
        output_dir = get_runtime_dir() / run_date
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def get_logs_dir() -> Path:
    explicit_logs_dir = _env_path(LOGS_DIR_ENV)
    if explicit_logs_dir:
        return explicit_logs_dir
    return get_runtime_dir() / "logs"


def get_user_data_dir() -> Path:
    explicit_user_data_dir = _env_path(USER_DATA_DIR_ENV)
    if explicit_user_data_dir:
        return explicit_user_data_dir
    return get_runtime_dir() / "user_data"


def ensure_runtime_dirs() -> None:
    get_logs_dir().mkdir(parents=True, exist_ok=True)
    get_user_data_dir().mkdir(parents=True, exist_ok=True)


def normalize_report_name(name: str) -> str:
    lowered = (name or "").strip().lower()
    lowered = MULTISPACE_REGEX.sub(" ", lowered)
    return lowered


def extract_cid(text: str) -> str | None:
    if not text:
        return None
    match = CID_REGEX.search(text)
    if not match:
        return None
    return match.group(1)


def cid_to_digits(cid: str) -> str:
    return re.sub(r"\D+", "", cid or "")


def sanitize_filename(name: str) -> str:
    cleaned = INVALID_FILENAME_CHARS.sub("_", name.strip())
    cleaned = cleaned.rstrip(". ")
    return cleaned or "untitled"


def dedupe_accounts(accounts: list[AdsAccount]) -> list[AdsAccount]:
    by_cid: dict[str, AdsAccount] = {}
    for account in accounts:
        if not account.cid_digits:
            continue
        by_cid[account.cid_digits] = account
    return list(by_cid.values())


def setup_logger(name: str = "google_ads_exporter") -> tuple[logging.Logger, Path]:
    ensure_runtime_dirs()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = get_logs_dir() / f"run_{ts}.log"

    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    return logger, log_path
