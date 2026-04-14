"""Datamodels used by the automation flow."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class AdsAccount:
    name: str
    cid: str
    cid_digits: str
    is_manager: bool = False
    raw_text: str = ""


@dataclass(slots=True)
class SavedReportItem:
    visible_name: str
    normalized_name: str
    inferred_type: str  # report / view / unknown
    activity_name: str | None
    activity_key: str | None
    row_text: str
    matched_key: str | None
    owner_text: str | None
    created_by: str | None
    creation_date: str | None
    last_accessed: str | None
    date_range: str | None = None
    has_download_text: bool = False
    has_download_icon: bool = False


@dataclass(slots=True)
class DownloadResult:
    target_key: str
    success: bool
    activity_name: str = ""
    activity_key: str = ""
    filename: str | None = None
    reason: str | None = None
