"""Result formatting helpers.

Tkinter dialogs were removed as part of Streamlit migration.
"""

from __future__ import annotations

from .models import AdsAccount, DownloadResult, SavedReportItem
from .saved_reports_scanner import match_targets
from .targets import TARGET_DISPLAY_NAMES, TARGET_ORDER


def confirm_scan_results(
    account: AdsAccount,
    items: list[SavedReportItem],
    download_enabled: bool = False,
    scan_only_enabled: bool = True,
    matched_map: dict[str, SavedReportItem] | None = None,
) -> str:
    """
    Return one of: cancel / proceed_scan_only / proceed_download.

    Legacy function retained for compatibility with non-Streamlit paths.
    """
    _ = (account, items, matched_map)
    if download_enabled:
        return "proceed_download"
    if scan_only_enabled:
        return "proceed_scan_only"
    return "cancel"


def build_scan_summary_text(
    account: AdsAccount,
    items: list[SavedReportItem],
    matched_map: dict[str, SavedReportItem] | None = None,
) -> str:
    target_map = matched_map if matched_map is not None else match_targets(items)
    lines = [f"Account: {account.name} {account.cid}", ""]
    lines.append("target csv / Saved reports / owner / creation date / date range / created by")

    for key in TARGET_ORDER:
        label = TARGET_DISPLAY_NAMES.get(key, key)
        item = target_map.get(key)
        if not item:
            lines.append(f"{label} / Not found")
            continue

        owner = item.owner_text or "-"
        creation_date = item.creation_date or "-"
        date_range = item.date_range or "-"
        created_by = item.created_by or "-"
        lines.append(
            f"{label} / {item.visible_name} / {owner} / {creation_date} / {date_range} / {created_by}"
        )

    return "\n".join(lines)


def show_download_results(results_by_account: dict[str, list[DownloadResult]]) -> str:
    lines: list[str] = []
    for account_label, results in results_by_account.items():
        lines.append(f"[{account_label}]")
        for result in results:
            activity = str(result.activity_name or result.activity_key or "").strip()
            prefix = f"[{activity}] " if activity else ""
            if result.success:
                lines.append(f"{prefix}{result.target_key:<18} SUCCESS  -> {result.filename}")
            else:
                lines.append(f"{prefix}{result.target_key:<18} FAIL     -> {result.reason}")
        lines.append("")

    if not lines:
        lines = ["No download results."]

    rendered = "\n".join(lines)
    print(rendered)
    return rendered
