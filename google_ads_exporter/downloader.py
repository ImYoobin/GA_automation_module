"""Download logic for matched saved report items."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re

from playwright.sync_api import Page

from .config import DOWNLOAD_CLICK_RETRIES, DOWNLOAD_TIMEOUT_MS, MEDIUM_WAIT_MS
from .models import AdsAccount, DownloadResult, SavedReportItem
from .targets import REPORT_KEYS
from .utils import normalize_report_name, sanitize_filename

REPORT_VIEW_LOAD_TIMEOUT_MS = 14000
VIEW_BASE_READY_TIMEOUT_MS = 6500
VIEW_STABILIZE_WAIT_MS = 1000
ROW_LOOKUP_MAX_SCROLL_ROUNDS = 26
SHOW_ROWS_BUTTON_SELECTOR = "div[role='button'][aria-label*='Show rows']"
SHOW_ROWS_LISTBOX_SELECTOR = (
    "material-list[role='listbox'][aria-label*='Choose number of rows to be displayed per page']"
)


def download_item(
    page: Page,
    account: AdsAccount,
    item: SavedReportItem,
    output_dir: Path,
    logger=None,
) -> DownloadResult:
    is_report = _is_report_target(item)
    last_reason = "download click failed"

    for attempt in range(DOWNLOAD_CLICK_RETRIES + 1):
        _set_show_rows_to_500(page, logger=logger)
        row = _find_row_for_item_with_scroll(page, item, logger=logger)
        if row is None:
            last_reason = "report row not found in Saved reports table"
            continue

        if is_report:
            saved, reason = _try_report_download(page, row, account, item, output_dir, logger=logger)
        else:
            saved, reason = _try_view_download(page, row, account, item, output_dir, logger=logger)

        if saved:
            return DownloadResult(
                target_key=item.matched_key or "unknown",
                success=True,
                filename=saved.name,
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
        reason=last_reason,
    )


def _try_view_download(
    page: Page,
    row,
    account: AdsAccount,
    item: SavedReportItem,
    output_dir: Path,
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
            saved = _save_download(download_info.value, output_dir, account, item)
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
            saved = _save_download(download_info.value, output_dir, account, item)
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
        with page.expect_download(timeout=DOWNLOAD_TIMEOUT_MS) as download_info:
            csv_item.click(timeout=5000)
        saved = _save_download(download_info.value, output_dir, account, item)
        if logger:
            logger.info("download success (report-csv-menu) target=%s file=%s", item.matched_key, saved.name)
        return saved, None
    except Exception as exc:  # noqa: BLE001
        if logger:
            logger.warning("report csv menu click failed reason=%s", exc)
        return None, "csv click/download timeout"


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


def _save_download(download, output_dir: Path, account: AdsAccount, item: SavedReportItem) -> Path:
    out_path = _build_output_path(output_dir, account, item.matched_key or "unknown")
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

    no_hit_rounds = 0
    for round_idx in range(ROW_LOOKUP_MAX_SCROLL_ROUNDS):
        moved = _scroll_saved_reports_for_lookup(page)
        if not moved:
            break
        row = _find_row_for_item_once(page, item)
        if row is not None:
            if logger:
                logger.info(
                    "row lookup success after scroll | target=%s | round=%s",
                    item.matched_key,
                    round_idx + 1,
                )
            return row
        no_hit_rounds += 1
        if no_hit_rounds >= 4:
            break
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


def _build_output_path(output_dir: Path, account: AdsAccount, target_key: str) -> Path:
    run_date = datetime.now().strftime("%Y%m%d")
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
    button = page.locator(SHOW_ROWS_BUTTON_SELECTOR).first
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
