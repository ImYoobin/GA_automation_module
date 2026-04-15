"""Download logic for matched saved report items."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from playwright.sync_api import Page
else:
    Page = Any

from .config import DOWNLOAD_CLICK_RETRIES, DOWNLOAD_TIMEOUT_MS, MEDIUM_WAIT_MS
from .models import AdsAccount, DownloadResult, SavedReportItem
from .targets import REPORT_KEYS
from .utils import normalize_report_name, sanitize_filename

REPORT_VIEW_LOAD_TIMEOUT_MS = 14000
VIEW_BASE_READY_TIMEOUT_MS = 6500
VIEW_STABILIZE_WAIT_MS = 1000
ROW_LOOKUP_MAX_SCROLL_ROUNDS = 72
ROW_LOOKUP_STAGNATION_LIMIT = 3
SHOW_ROWS_BUTTON_SELECTOR = "div[role='button'][aria-label*='Show rows']"
SHOW_ROWS_BUTTON_FALLBACK_SELECTOR = "div[role='button']:has(span.button-text)"
SHOW_ROWS_LISTBOX_SELECTOR = (
    "material-list[role='listbox'][aria-label*='Choose number of rows to be displayed per page']"
)
ROW_COUNT_REGEX = re.compile(r"^(10|25|50|100|250|500|1000)$")
PAGINATION_TEXT_REGEX = re.compile(r"\b\d+\s*-\s*\d+\s+of\s+\d+\b", re.IGNORECASE)
REPORT_DOWNLOAD_START_TIMEOUT_MS = 240000
PENDING_DOWNLOAD_REASON = "background_download_pending"


def download_item(
    page: Page,
    account: AdsAccount,
    item: SavedReportItem,
    output_dir: Path,
    activity_name: str = "",
    activity_key: str = "",
    lookup_state: dict[str, Any] | None = None,
    logger=None,
) -> DownloadResult:
    is_report = _is_report_target(item)
    last_reason = "download click failed"
    if lookup_state is None:
        lookup_state = {}

    for attempt in range(DOWNLOAD_CLICK_RETRIES + 1):
        prefer_reports_asc = bool(lookup_state.get("prefer_reports_asc"))
        _set_show_rows_to_500(page, logger=logger)
        if prefer_reports_asc:
            _apply_reports_sort_fallback(page, logger=logger, reason="sticky_asc")
        row = _find_row_for_item_with_scroll(page, item, logger=logger)
        if row is None:
            diagnostics = _collect_row_lookup_diagnostics(page, item)
            _log_row_lookup_miss(
                page,
                item,
                attempt=attempt + 1,
                logger=logger,
                phase="before_sort_fallback" if not prefer_reports_asc else "sticky_asc_miss",
                diagnostics=diagnostics,
            )

            fallback_applied = False
            if not prefer_reports_asc:
                fallback_applied = _apply_reports_sort_fallback(
                    page,
                    logger=logger,
                    reason="row_not_found",
                )
                if fallback_applied:
                    lookup_state["prefer_reports_asc"] = True
                    row = _find_row_for_item_with_scroll(page, item, logger=logger)
                    if row is not None:
                        if logger:
                            logger.info(
                                "row lookup recovered after sort fallback | target=%s | activity=%s",
                                item.matched_key,
                                item.activity_name or item.activity_key or "-",
                            )

            if row is None:
                post_diagnostics = _collect_row_lookup_diagnostics(page, item)
                _log_row_lookup_miss(
                    page,
                    item,
                    attempt=attempt + 1,
                    logger=logger,
                    phase="after_sort_fallback" if fallback_applied else "without_sort_fallback",
                    diagnostics=post_diagnostics,
                )
                last_reason = _build_row_miss_reason(post_diagnostics)
                continue

        if is_report:
            saved, reason = _try_report_download(
                page,
                row,
                account,
                item,
                output_dir,
                activity_name=activity_name,
                lookup_state=lookup_state,
                logger=logger,
            )
        else:
            saved, reason = _try_view_download(
                page,
                row,
                account,
                item,
                output_dir,
                activity_name=activity_name,
                lookup_state=lookup_state,
                logger=logger,
            )

        if saved:
            return DownloadResult(
                target_key=item.matched_key or "unknown",
                success=True,
                activity_name=activity_name,
                activity_key=activity_key,
                filename=saved.name,
                reason=reason,
            )

        if reason:
            last_reason = reason

        if logger:
            logger.warning(
                "download attempt failed target=%s attempt=%s reason=%s",
                item.matched_key,
                attempt + 1,
                last_reason,
            )

    return DownloadResult(
        target_key=item.matched_key or "unknown",
        success=False,
        activity_name=activity_name,
        activity_key=activity_key,
        reason=last_reason,
    )


def _try_view_download(
    page: Page,
    row,
    account: AdsAccount,
    item: SavedReportItem,
    output_dir: Path,
    activity_name: str = "",
    lookup_state: dict[str, Any] | None = None,
    logger=None,
):
    """
    View targets: use row-local selectors only.
    Priority (validated):
    1) .report-list-action-button[role='button']            (download_text variant)
    2) .load-report-button[role='button']:has(file_download) (download_icon variant)
    3) img[src*='file_download']                            (fallback)
    """
    if not _wait_for_view_base_ready(page, item, logger=logger):
        return None, "view base context not ready"

    # Brief stabilization wait to avoid flaky immediate clicks after restore.
    page.wait_for_timeout(VIEW_STABILIZE_WAIT_MS)

    latest_row = _find_row_for_item_with_scroll(page, item, logger=logger)
    if latest_row is not None:
        row = latest_row

    signature = _get_view_row_signature(row)
    if logger:
        logger.info(
            "view row signature target=%s | action_button=%s | file_download_icon=%s | table_chart_icon=%s",
            item.matched_key,
            signature["has_action_button"],
            signature["has_file_download_icon"],
            signature["has_table_chart_icon"],
        )

    click_plan = [
        (".report-list-action-button[role='button']", "view-action-button"),
        (".load-report-button[role='button']:has(img[src*='file_download'])", "view-load-button-file-download"),
        ("img.report-icon[src*='file_download']", "view-file-download-icon"),
        ("span.text-and-action:has-text('Download')", "view-text-and-action-download"),
        ("[role='button']:has-text('Download')", "view-role-button-download"),
    ]

    for selector, tag in click_plan:
        control = row.locator(selector).first
        if control.count() == 0:
            continue
        try:
            with page.expect_download(timeout=DOWNLOAD_TIMEOUT_MS) as download_info:
                control.click(timeout=5000)
            saved = _save_download(download_info.value, output_dir, account, item, activity_name=activity_name)
            if logger:
                logger.info(
                    "download success (%s) target=%s file=%s",
                    tag,
                    item.matched_key,
                    saved.name,
                )
            return saved, None
        except Exception as exc:  # noqa: BLE001
            current_url = (page.url or "").lower()
            if "/aw/reporteditor/view" in current_url:
                if logger:
                    logger.warning(
                        "view selector caused navigation to report view selector=%s target=%s url=%s",
                        selector,
                        item.matched_key,
                        page.url,
                    )
                return None, "unexpected navigation to report view"
            if logger:
                logger.warning("view direct download selector failed selector=%s reason=%s", selector, exc)

    text_controls = [
        (row.get_by_text("Download", exact=True).first, "view-exact-download-text"),
        (row.get_by_text("Download").first, "view-download-text"),
    ]
    for control, tag in text_controls:
        if control.count() == 0:
            continue
        try:
            with page.expect_download(timeout=DOWNLOAD_TIMEOUT_MS) as download_info:
                control.click(timeout=5000)
            saved = _save_download(download_info.value, output_dir, account, item, activity_name=activity_name)
            if logger:
                logger.info(
                    "download success (%s) target=%s file=%s",
                    tag,
                    item.matched_key,
                    saved.name,
                )
            return saved, None
        except Exception as exc:  # noqa: BLE001
            current_url = (page.url or "").lower()
            if "/aw/reporteditor/view" in current_url:
                if logger:
                    logger.warning(
                        "view text control caused navigation target=%s url=%s",
                        item.matched_key,
                        page.url,
                    )
                return None, "unexpected navigation to report view"
            if logger:
                logger.warning("view text-control download failed reason=%s", exc)

    return None, "view download control not found"


def _try_report_download(
    page: Page,
    row,
    account: AdsAccount,
    item: SavedReportItem,
    output_dir: Path,
    activity_name: str = "",
    lookup_state: dict[str, Any] | None = None,
    logger=None,
):
    """
    Report targets: open item, click top Download button, then pick .csv from popup menu.
    """
    opened, open_reason = _open_item_from_row(page, row, item, logger=logger)
    if not opened:
        return None, open_reason

    ready = _wait_until_report_view_ready(page, item, logger=logger)
    if not ready:
        return None, "report view load timeout"

    clicked = _click_download_menu_button(page, logger=logger)
    if not clicked:
        return None, "download menu button click failed"

    csv_item = _wait_for_csv_menu_item(page, timeout_ms=7000)
    if csv_item is None:
        return None, "csv menu item not found"

    try:
        with page.expect_download(timeout=REPORT_DOWNLOAD_START_TIMEOUT_MS) as download_info:
            csv_item.click(timeout=5000)
        queued_path = _queue_pending_download(
            output_dir=output_dir,
            account=account,
            item=item,
            activity_name=activity_name,
            lookup_state=lookup_state,
            download_obj=download_info.value,
            logger=logger,
        )
        if queued_path is not None:
            if logger:
                logger.info(
                    "report download started target=%s file=%s",
                    item.matched_key,
                    queued_path.name,
                )
            return queued_path, PENDING_DOWNLOAD_REASON

        saved = _save_download(download_info.value, output_dir, account, item, activity_name=activity_name)
        if logger:
            logger.info("download success (report-csv-menu) target=%s file=%s", item.matched_key, saved.name)
        return saved, None
    except Exception as exc:  # noqa: BLE001
        if logger:
            logger.warning("report csv menu click failed reason=%s", exc)
        return None, "csv click/download timeout or start not detected"


def _open_item_from_row(page: Page, row, item: SavedReportItem, logger=None) -> tuple[bool, str | None]:
    selectors = (
        "span.ess-cell-link.report-name-text",
        "span.report-name-text",
        "report-name",
        "a",
        "div.load-report-button",
    )
    for selector in selectors:
        target = row.locator(selector)
        if target.count() == 0:
            continue
        try:
            target.first.click(timeout=5000)
            page.wait_for_timeout(MEDIUM_WAIT_MS)
            return True, None
        except Exception as exc:  # noqa: BLE001
            if logger:
                logger.warning("open row click failed selector=%s target=%s reason=%s", selector, item.matched_key, exc)

    # Global fallback by name text.
    try:
        global_name = page.locator("span.report-name-text, span.ess-cell-link.report-name-text").filter(
            has_text=item.visible_name
        )
        if global_name.count() > 0:
            global_name.first.click(timeout=5000)
            page.wait_for_timeout(MEDIUM_WAIT_MS)
            return True, None
    except Exception as exc:  # noqa: BLE001
        if logger:
            logger.warning("global name click failed target=%s reason=%s", item.matched_key, exc)

    return False, "open row failed"


def _click_download_menu_button(page: Page, logger=None) -> bool:
    candidates = _download_menu_button_candidates(page)
    for button in candidates:
        if button.count() == 0:
            continue
        try:
            button.first.click(timeout=5000)
            return True
        except Exception as exc:  # noqa: BLE001
            if logger:
                logger.warning("report download button click failed reason=%s", exc)
    return False


def _wait_until_report_view_ready(page: Page, item: SavedReportItem, logger=None) -> bool:
    elapsed = 0
    while elapsed < REPORT_VIEW_LOAD_TIMEOUT_MS:
        url_has_view = _is_report_view_page(page)
        close_visible = _is_report_view_close_visible(page)
        download_ready = _is_report_download_ready(page)
        if url_has_view and close_visible and download_ready:
            if logger:
                logger.info(
                    "report view ready target=%s elapsed_ms=%s | url_has_view=%s | close_visible=%s | download_ready=%s",
                    item.matched_key,
                    elapsed,
                    url_has_view,
                    close_visible,
                    download_ready,
                )
            return True

        page.wait_for_timeout(400)
        elapsed += 400

    if logger:
        logger.warning(
            "report view readiness timeout target=%s waited_ms=%s | url=%s",
            item.matched_key,
            REPORT_VIEW_LOAD_TIMEOUT_MS,
            page.url,
        )
    return False


def _download_menu_button_candidates(page: Page):
    return [
        page.locator("material-button[aria-label='Download'][role='button']"),
        page.locator("material-button[aria-label='Download']"),
        page.locator("[aria-label='Download'][role='button']"),
        page.get_by_role("button", name="Download"),
    ]


def _wait_for_csv_menu_item(page: Page, timeout_ms: int = 7000):
    elapsed = 0
    while elapsed < timeout_ms:
        item = _find_csv_menu_item(page)
        if item is not None:
            return item
        page.wait_for_timeout(400)
        elapsed += 400
    return None


def _find_csv_menu_item(page: Page):
    locators = [
        page.locator("material-list.download-dropdown material-list-item[role='listitem']"),
        page.locator("material-popup material-list-item[role='listitem']"),
        page.locator("material-list-item[role='listitem']"),
    ]
    for locator in locators:
        count = locator.count()
        if count == 0:
            continue
        for i in range(count):
            item = locator.nth(i)
            text = _safe_inner_text(item)
            if _looks_like_csv_text(text):
                return item
    return None


def _looks_like_csv_text(text: str) -> bool:
    normalized = re.sub(r"\s+", "", (text or "").strip().lower())
    return normalized in {".csv", "csv"} or normalized.startswith(".csv") or normalized.endswith(".csv")


def _save_download(
    download,
    output_dir: Path,
    account: AdsAccount,
    item: SavedReportItem,
    activity_name: str = "",
) -> Path:
    out_path = _build_output_path(
        output_dir,
        account,
        target_key=item.matched_key or "unknown",
        activity_name=activity_name,
    )
    if out_path.exists():
        out_path.unlink()
    download.save_as(str(out_path))
    return out_path


def _is_report_target(item: SavedReportItem) -> bool:
    if item.matched_key in REPORT_KEYS:
        return True
    return item.inferred_type == "report"


def _find_row_for_item_with_scroll(page: Page, item: SavedReportItem, logger=None):
    _reset_saved_reports_scroll(page)

    row = _find_row_for_item_once(page, item)
    if row is not None:
        return row

    no_move_rounds = 0
    for round_idx in range(ROW_LOOKUP_MAX_SCROLL_ROUNDS):
        moved = _scroll_saved_reports_for_lookup(page)
        if not moved:
            no_move_rounds += 1
            if no_move_rounds >= ROW_LOOKUP_STAGNATION_LIMIT:
                break
        else:
            no_move_rounds = 0

        if round_idx > 0 and round_idx % 10 == 0:
            _set_show_rows_to_500(page, logger=logger)

        row = _find_row_for_item_once(page, item)
        if row is not None:
            if logger:
                logger.info(
                    "row lookup success after scroll | target=%s | round=%s",
                    item.matched_key,
                    round_idx + 1,
                )
            return row
    return None


def _find_row_for_item_once(page: Page, item: SavedReportItem):
    # Preferred for observed DOM: name cell in table.
    name_cells = page.locator("[essfield='definition.report_name']")
    for i in range(name_cells.count()):
        cell = name_cells.nth(i)
        row = _as_row_container(cell)
        row_class = (row.get_attribute("class") or "").strip().lower() if row is not None else ""
        if "particle-table-header" in row_class:
            continue
        candidate_name = _extract_name_from_cell(cell)
        if not candidate_name:
            continue
        if normalize_report_name(candidate_name) == item.normalized_name:
            return row

    # Fallback row lookup by text.
    for selector in ("particle-table-row", "tbody tr", "[role='row']"):
        rows = page.locator(selector).filter(has_text=item.visible_name)
        if rows.count() == 1:
            return rows.first
        if rows.count() > 1:
            for i in range(rows.count()):
                row = rows.nth(i)
                text = _safe_inner_text(row)
                if normalize_report_name(item.visible_name) in normalize_report_name(text):
                    return row

    # Last fallback: name element itself.
    names = page.locator("span.report-name-text, span.ess-cell-link.report-name-text").filter(
        has_text=item.visible_name
    )
    if names.count() > 0:
        return _as_row_container(names.first)
    return None


def _as_row_container(locator):
    row = locator.locator("xpath=ancestor::*[@role='row'][1]")
    if row.count() > 0:
        return row.first
    return locator


def _build_output_path(
    output_dir: Path,
    account: AdsAccount,
    target_key: str,
    activity_name: str = "",
) -> Path:
    run_date = datetime.now().strftime("%Y%m%d")
    activity_fragment = str(activity_name or "").strip()
    if activity_fragment:
        filename = f"{run_date}_{account.name}_{activity_fragment}_{target_key}.csv"
    else:
        filename = f"{run_date}_{account.name}_{target_key}.csv"
    return output_dir / sanitize_filename(filename)


def _extract_name_from_cell(cell) -> str:
    for selector in (
        ".report-name-text",
        "span.ess-cell-link.report-name-text",
        "span.report-name-text",
        "div.report-name-text",
        "a",
    ):
        loc = cell.locator(selector)
        if loc.count() == 0:
            continue
        text = _safe_inner_text(loc.first)
        cleaned = _clean_name_text(text)
        if cleaned:
            return cleaned
    return _clean_name_text(_safe_inner_text(cell))


def _clean_name_text(text: str) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    lines = [line for line in lines if line.lower() != "download"]
    return lines[0] if lines else ""


def _safe_inner_text(locator) -> str:
    try:
        return (locator.inner_text(timeout=1000) or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _get_view_row_signature(row) -> dict[str, bool]:
    def _exists(selector: str) -> bool:
        try:
            return row.locator(selector).count() > 0
        except Exception:  # noqa: BLE001
            return False

    return {
        "has_action_button": _exists(".report-list-action-button[role='button']"),
        "has_file_download_icon": _exists("img.report-icon[src*='file_download']"),
        "has_table_chart_icon": _exists("img.report-icon[src*='table_chart']"),
    }


def _wait_for_view_base_ready(page: Page, item: SavedReportItem, logger=None) -> bool:
    elapsed = 0
    while elapsed < VIEW_BASE_READY_TIMEOUT_MS:
        url_has_view = _is_report_view_page(page)
        data_row_count = _saved_reports_data_row_count(page)
        target_row_visible = _is_target_row_visible(page, item)
        if (not url_has_view) and data_row_count > 0 and target_row_visible:
            if logger:
                logger.info(
                    "view base ready target=%s elapsed_ms=%s | url_has_view=%s | data_row_count=%s | target_row_visible=%s",
                    item.matched_key,
                    elapsed,
                    url_has_view,
                    data_row_count,
                    target_row_visible,
                )
            return True
        page.wait_for_timeout(300)
        elapsed += 300

    if logger:
        logger.warning(
            "view base ready timeout target=%s waited_ms=%s | url=%s",
            item.matched_key,
            VIEW_BASE_READY_TIMEOUT_MS,
            page.url,
        )
    return False


def _is_target_row_visible(page: Page, item: SavedReportItem) -> bool:
    if not item.visible_name:
        return False
    try:
        names = page.locator("[essfield='definition.report_name'] .report-name-text").filter(has_text=item.visible_name)
        if names.count() > 0:
            return True
    except Exception:  # noqa: BLE001
        pass
    try:
        rows = page.locator("div.particle-table-row[role='row']").filter(has_text=item.visible_name)
        return rows.count() > 0
    except Exception:  # noqa: BLE001
        return False


def _saved_reports_data_row_count(page: Page) -> int:
    try:
        return page.locator("div.particle-table-row[role='row']").count()
    except Exception:  # noqa: BLE001
        return 0


def _set_show_rows_to_500(page: Page, logger=None) -> None:
    if _is_report_view_page(page):
        return
    button = _resolve_show_rows_button(page)
    if button.count() == 0:
        return

    try:
        current_text = _safe_inner_text(button.locator("span.button-text").first)
        current_aria = (button.get_attribute("aria-label") or "").strip()
        if current_text == "500" or "500 selected" in current_aria:
            return
    except Exception:  # noqa: BLE001
        pass

    try:
        button.click(timeout=2000)
    except Exception as exc:  # noqa: BLE001
        if logger:
            logger.info("download show rows button click failed | reason=%s", exc)
        return

    listbox = page.locator(SHOW_ROWS_LISTBOX_SELECTOR).first
    if listbox.count() == 0:
        listbox = page.locator("material-list[role='listbox']").first
    try:
        listbox.wait_for(state="visible", timeout=3500)
    except Exception as exc:  # noqa: BLE001
        if logger:
            logger.info("download show rows listbox not visible | reason=%s", exc)
        return

    option = listbox.locator("[role='option']").filter(has_text="500").first
    if option.count() == 0:
        if logger:
            logger.info("download show rows option 500 not found")
        return

    try:
        option.click(timeout=2000)
        page.wait_for_timeout(450)
        if logger:
            logger.info("download show rows set to 500")
    except Exception as exc:  # noqa: BLE001
        if logger:
            logger.info("download show rows option click failed | reason=%s", exc)


def _resolve_show_rows_button(page: Page):
    panel = _resolve_saved_reports_panel(page)
    if panel.count() == 0:
        return page.locator("div.__ga_saved_reports_show_rows_not_found__")

    button = panel.locator(SHOW_ROWS_BUTTON_SELECTOR).first
    if button.count() > 0:
        return button

    candidates = panel.locator(SHOW_ROWS_BUTTON_FALLBACK_SELECTOR)
    count = candidates.count()
    for i in range(count):
        candidate = candidates.nth(i)
        text = _safe_inner_text(candidate.locator("span.button-text").first)
        if ROW_COUNT_REGEX.match(text):
            return candidate

    return panel.locator("div[role='button']").filter(has_text="500").first


def _apply_reports_sort_fallback(page: Page, logger=None, reason: str = "") -> bool:
    _ensure_saved_reports_panel_expanded(page, logger=logger)
    _set_show_rows_to_500(page, logger=logger)
    asc_ready = _ensure_reports_column_sort_ascending(page, logger=logger)
    if logger:
        logger.info(
            "sort fallback applied | success=%s | reason=%s | url=%s",
            asc_ready,
            reason,
            page.url,
        )
    return asc_ready


def _ensure_saved_reports_panel_expanded(page: Page, logger=None) -> bool:
    panel = _resolve_saved_reports_panel(page)
    if panel.count() == 0:
        return False

    region = panel.locator("div.main[role='region'], div[role='region']").first
    try:
        hidden = (region.get_attribute("aria-hidden") or "").strip().lower() if region.count() > 0 else ""
        if hidden == "false":
            return True
    except Exception:  # noqa: BLE001
        pass

    click_targets = [
        panel.locator("div[role='button'][aria-label='Saved reports']").first,
        panel.get_by_text("Saved reports", exact=False).first,
        panel.locator("material-icon.expand-button").first,
    ]
    for target in click_targets:
        if target.count() == 0:
            continue
        try:
            target.click(timeout=1500)
            page.wait_for_timeout(400)
            if region.count() == 0:
                return True
            hidden = (region.get_attribute("aria-hidden") or "").strip().lower()
            if hidden != "true":
                return True
        except Exception:  # noqa: BLE001
            continue

    if logger:
        logger.info("saved reports expand fallback failed")
    return False


def _resolve_saved_reports_panel(page: Page):
    panel = page.locator("material-expansionpanel:has(div[role='button'][aria-label='Saved reports'])").first
    if panel.count() > 0:
        return panel
    panel = page.locator("material-expansionpanel:has-text('Saved reports')").first
    if panel.count() > 0:
        return panel
    return page.locator("material-expansionpanel:has([essfield='definition.report_name'])").first


def _resolve_saved_reports_grid(page: Page):
    panel = _resolve_saved_reports_panel(page)
    if panel.count() > 0:
        grid = panel.locator(".ess-table-canvas[role='grid'], [role='grid']").first
        if grid.count() > 0:
            return grid
    return page.locator(".ess-table-canvas[role='grid'], [role='grid']").first


def _ensure_reports_column_sort_ascending(page: Page, logger=None) -> bool:
    header = _resolve_reports_column_header(page)
    if header is None:
        if logger:
            logger.info("reports sort fallback skipped | reason=reports_header_not_found")
        return False

    current_sort = _read_sort_state(header)
    if current_sort == "ascending":
        return True

    for _ in range(2):
        if not _click_reports_header(header):
            break
        page.wait_for_timeout(350)
        current_sort = _read_sort_state(header)
        if current_sort == "ascending":
            return True

    if logger:
        logger.info("reports sort fallback failed | final_sort=%s", current_sort or "unknown")
    return False


def _resolve_reports_column_header(page: Page):
    grid = _resolve_saved_reports_grid(page)
    if grid.count() == 0:
        return None

    try:
        headers = grid.locator("[role='columnheader']")
        for idx in range(headers.count()):
            header = headers.nth(idx)
            text = normalize_report_name(_safe_inner_text(header))
            if text.startswith("reports"):
                return header
    except Exception:  # noqa: BLE001
        return None
    return None


def _read_sort_state(header) -> str:
    for candidate in (
        header,
        header.locator("xpath=ancestor::*[@aria-sort][1]").first,
        header.locator("[aria-sort]").first,
    ):
        try:
            if candidate.count() == 0:
                continue
            value = (candidate.get_attribute("aria-sort") or "").strip().lower()
            if value:
                return value
        except Exception:  # noqa: BLE001
            continue
    return ""


def _click_reports_header(header) -> bool:
    click_targets = [
        header,
        header.locator("[role='button']").first,
        header.locator("button").first,
        header.locator("span").first,
    ]
    for target in click_targets:
        try:
            if target.count() == 0:
                continue
            target.click(timeout=1800)
            return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _reset_saved_reports_scroll(page: Page) -> None:
    try:
        page.evaluate("() => window.scrollTo(0, 0)")
    except Exception:  # noqa: BLE001
        pass
    try:
        region = page.locator("material-expansionpanel:has(div[aria-label='Saved reports']) div.main[role='region']").first
        if region.count() > 0:
            region.evaluate(
                """(el) => {
                    try {
                        el.scrollTop = 0;
                        el.dispatchEvent(new Event('scroll', { bubbles: true }));
                    } catch (e) {}
                }"""
            )
    except Exception:  # noqa: BLE001
        pass
    page.wait_for_timeout(140)


def _scroll_saved_reports_for_lookup(page: Page) -> bool:
    moved = False
    try:
        region = page.locator("material-expansionpanel:has(div[aria-label='Saved reports']) div.main[role='region']").first
        if region.count() > 0:
            moved = bool(
                region.evaluate(
                    """(el) => {
                        try {
                            const before = el.scrollTop || 0;
                            const delta = Math.max(360, Math.floor((el.clientHeight || 650) * 0.8));
                            el.scrollTop = before + delta;
                            el.dispatchEvent(new Event('scroll', { bubbles: true }));
                            return Math.abs((el.scrollTop || 0) - before) > 2;
                        } catch (e) {
                            return false;
                        }
                    }"""
                )
            )
    except Exception:  # noqa: BLE001
        moved = False

    if not moved:
        try:
            moved = bool(
                page.evaluate(
                    """() => {
                        const before = window.scrollY || 0;
                        window.scrollBy(0, 560);
                        return Math.abs((window.scrollY || 0) - before) > 2;
                    }"""
                )
            )
        except Exception:  # noqa: BLE001
            moved = False

    page.wait_for_timeout(200)
    return moved


def _is_report_view_page(page: Page) -> bool:
    return "/aw/reporteditor/view" in (page.url or "").lower()


def _is_report_view_close_visible(page: Page) -> bool:
    selectors = (
        "awsm-app-bar material-button[aria-label='close'][role='button']",
        "awsm-app-bar material-button[aria-label='Close'][role='button']",
        "awsm-app-bar material-button.back-button[aria-label='close']",
        "awsm-app-bar material-button.back-button[aria-label='Close']",
        "material-button.back-button[aria-label='close']",
        "material-button.back-button[aria-label='Close']",
        "[aria-label='close'][role='button']",
        "[aria-label='Close'][role='button']",
    )
    for selector in selectors:
        try:
            button = page.locator(selector).first
            if button.count() > 0 and button.is_visible(timeout=300):
                return True
        except Exception:  # noqa: BLE001
            continue
    try:
        button = page.get_by_role("button", name=re.compile("close", re.IGNORECASE)).first
        return button.count() > 0 and button.is_visible(timeout=300)
    except Exception:  # noqa: BLE001
        return False


def _is_report_download_ready(page: Page) -> bool:
    for button in _download_menu_button_candidates(page):
        try:
            if button.count() > 0 and button.first.is_visible(timeout=300):
                return True
        except Exception:  # noqa: BLE001
            continue

    menu_items = (
        "material-list.download-dropdown material-list-item[role='listitem']",
        "material-popup material-list-item[role='listitem']",
    )
    for selector in menu_items:
        try:
            if page.locator(selector).count() > 0:
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _collect_row_lookup_diagnostics(page: Page, item: SavedReportItem) -> dict[str, Any]:
    sample_names = _sample_saved_report_names(page, limit=12)
    target_activity = str(item.activity_name or item.activity_key or "").strip().lower()
    near_names = [
        name
        for name in sample_names
        if (
            target_activity
            and target_activity in normalize_report_name(name).replace(" ", "_")
        )
        or (item.matched_key and item.matched_key in normalize_report_name(name))
    ]
    return {
        "dom_rows": _saved_reports_data_row_count(page),
        "aria_rowcount": _extract_grid_aria_rowcount(page),
        "pagination": _extract_pagination_text(page),
        "sample_names": sample_names,
        "near_names": near_names,
        "url": page.url,
    }


def _build_row_miss_reason(diagnostics: dict[str, Any] | None) -> str:
    if not diagnostics:
        return "report row not found in Saved reports table"
    dom_rows = diagnostics.get("dom_rows")
    aria_rowcount = diagnostics.get("aria_rowcount")
    pagination = diagnostics.get("pagination") or "n/a"
    return (
        "report row not found in Saved reports table "
        f"(dom_rows={dom_rows}, aria_rowcount={aria_rowcount or 'n/a'}, pagination={pagination})"
    )


def _log_row_lookup_miss(
    page: Page,
    item: SavedReportItem,
    attempt: int,
    logger=None,
    phase: str = "initial",
    diagnostics: dict[str, Any] | None = None,
) -> None:
    if not logger:
        return
    details = diagnostics or _collect_row_lookup_diagnostics(page, item)
    logger.warning(
        "row lookup miss | phase=%s | attempt=%s | target=%s | activity=%s | expected_name=%s | url=%s | dom_rows=%s | aria_rowcount=%s | pagination=%s | sample_names=%s | near_names=%s",
        phase,
        attempt,
        item.matched_key,
        item.activity_name or item.activity_key or "-",
        item.visible_name,
        details.get("url"),
        details.get("dom_rows"),
        details.get("aria_rowcount"),
        details.get("pagination"),
        details.get("sample_names"),
        details.get("near_names"),
    )


def _extract_grid_aria_rowcount(page: Page) -> str:
    grid = _resolve_saved_reports_grid(page)
    if grid.count() == 0:
        return ""
    try:
        return (grid.get_attribute("aria-rowcount") or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _extract_pagination_text(page: Page) -> str:
    panel = _resolve_saved_reports_panel(page)
    try:
        text_pool = _safe_inner_text(panel if panel.count() > 0 else page)
    except Exception:  # noqa: BLE001
        text_pool = ""

    if text_pool:
        match = PAGINATION_TEXT_REGEX.search(text_pool)
        if match:
            return match.group(0)
    return ""


def _sample_saved_report_names(page: Page, limit: int = 12) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    locators = (
        page.locator("[essfield='definition.report_name'] .report-name-text"),
        page.locator("span.report-name-text"),
    )
    for locator in locators:
        try:
            count = min(locator.count(), limit * 3)
        except Exception:  # noqa: BLE001
            continue
        for idx in range(count):
            text = _clean_name_text(_safe_inner_text(locator.nth(idx)))
            if not text:
                continue
            normalized = normalize_report_name(text)
            if normalized in {"", "reports", "saved reports"}:
                continue
            if normalized in seen:
                continue
            seen.add(normalized)
            names.append(text)
            if len(names) >= limit:
                return names
    return names


def finalize_background_download_results(
    *,
    results: list[DownloadResult],
    lookup_state: dict[str, Any] | None,
    logger=None,
) -> list[DownloadResult]:
    if not results:
        return results
    if not isinstance(lookup_state, dict):
        return results

    pending_map = lookup_state.get("pending_downloads", {})
    if not isinstance(pending_map, dict):
        return results

    pending_by_target: dict[str, DownloadResult] = {
        result.target_key: result
        for result in results
        if result.success and (result.reason == PENDING_DOWNLOAD_REASON)
    }
    if not pending_by_target:
        return results

    remaining_targets: list[str] = []
    for target_key, result in pending_by_target.items():
        entry = pending_map.get(target_key, {})
        if not isinstance(entry, dict):
            result.success = False
            result.filename = None
            result.reason = "background download handle missing"
            remaining_targets.append(target_key)
            continue

        download_obj = entry.get("download")
        path_text = str(entry.get("path") or "").strip()
        if not path_text:
            result.success = False
            result.filename = None
            result.reason = "background download path missing"
            remaining_targets.append(target_key)
            continue

        output_path = Path(path_text)
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            if output_path.exists():
                output_path.unlink()
            if download_obj is not None:
                download_obj.save_as(str(output_path))
            if output_path.exists() and output_path.stat().st_size > 0:
                result.filename = output_path.name
                result.reason = None
                continue
            result.success = False
            result.filename = None
            result.reason = "background download file missing after save"
            remaining_targets.append(target_key)
        except Exception as exc:  # noqa: BLE001
            result.success = False
            result.filename = None
            result.reason = f"background download save failed: {exc}"
            remaining_targets.append(target_key)

    if logger:
        logger.info(
            "background download finalize | pending_initial=%s | pending_remaining=%s",
            len(pending_by_target),
            remaining_targets,
        )
    return results


def _queue_pending_download(
    *,
    output_dir: Path,
    account: AdsAccount,
    item: SavedReportItem,
    activity_name: str,
    lookup_state: dict[str, Any] | None,
    download_obj,
    logger=None,
) -> Path | None:
    if not isinstance(lookup_state, dict):
        return None
    target_key = str(item.matched_key or "").strip()
    if not target_key:
        return None

    expected_path = _build_output_path(
        output_dir=output_dir,
        account=account,
        target_key=target_key,
        activity_name=activity_name,
    )
    pending_map = lookup_state.setdefault("pending_downloads", {})
    if not isinstance(pending_map, dict):
        return None

    pending_map[target_key] = {
        "download": download_obj,
        "path": str(expected_path),
        "activity_name": str(activity_name or item.activity_name or item.activity_key or "").strip(),
        "visible_name": item.visible_name,
    }
    if logger:
        logger.info("background download queued target=%s file=%s", target_key, expected_path.name)
    return expected_path
