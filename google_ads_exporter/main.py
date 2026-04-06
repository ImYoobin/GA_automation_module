"""Main orchestration entrypoint."""

from __future__ import annotations

import argparse
import os
import re
import traceback
from pathlib import Path
from typing import Callable

from playwright.sync_api import sync_playwright

from .account_mapping import AccountMappingError, load_account_mapping, normalize_cid_digits
from .account_discovery import collect_accounts
from .account_selector_ui import pick_accounts
from .auth import assert_session_active, ensure_logged_in, launch_ads_context, minimize_browser_window
from .config import SAVED_REPORT_SCAN_RETRIES
from .downloader import download_item
from .env import load_env_file
from .models import AdsAccount, DownloadResult, SavedReportItem
from .report_editor import (
    account_selector_visible,
    assert_current_account,
    click_account_in_reporteditor_selector,
    is_report_editor_ready,
    open_report_editor,
)
from .result_ui import confirm_scan_results, show_download_results
from .saved_reports_scanner import match_targets, scan_saved_reports
from .targets import TARGET_ORDER, TargetMappingError, load_target_mapping_file
from .utils import get_run_output_dir, setup_logger


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _default_target_map_path() -> Path:
    return (_project_root() / "config" / "google" / "target_mapping.json").resolve()


def _default_account_map_path() -> Path:
    return (_project_root() / "config" / "google" / "account_mapping.json").resolve()


def _resolve_value(cli_value: str, env_name: str, default: str = "") -> str:
    cli_text = str(cli_value or "").strip()
    if cli_text:
        return cli_text
    env_text = str(os.getenv(env_name, "")).strip()
    if env_text:
        return env_text
    return str(default or "").strip()


def _resolve_bool(cli_value: bool | None, env_name: str, default: bool) -> bool:
    if cli_value is not None:
        return bool(cli_value)
    env_text = str(os.getenv(env_name, "")).strip().lower()
    if not env_text:
        return default
    return env_text in {"1", "true", "yes", "on"}


def _resolve_browser_preference(cli_value: str) -> str:
    browser = _resolve_value(cli_value, "GOOGLE_ADS_BROWSER", default="msedge").lower()
    if browser not in {"auto", "chromium", "msedge", "chrome"}:
        raise ValueError("Unsupported browser value. Use one of: msedge, chrome, auto, chromium.")
    return browser


def _parse_cid_filters(value: str) -> set[str]:
    filters: set[str] = set()
    text = str(value or "").strip()
    if not text:
        return filters
    for item in text.split(","):
        digits = normalize_cid_digits(item)
        if digits:
            filters.add(digits)
    return filters


def _apply_runtime_directory_overrides(
    *,
    runtime_dir: str,
    output_dir: str,
    logs_dir: str,
    user_data_dir: str,
) -> None:
    for env_name, value in {
        "GOOGLE_ADS_RUNTIME_DIR": runtime_dir,
        "GOOGLE_ADS_OUTPUT_DIR": output_dir,
        "GOOGLE_ADS_LOGS_DIR": logs_dir,
        "GOOGLE_ADS_USER_DATA_DIR": user_data_dir,
    }.items():
        text = str(value or "").strip()
        if text:
            os.environ[env_name] = str(Path(text).expanduser().resolve())


def _select_accounts_for_run(
    *,
    discovered_accounts: list[AdsAccount],
    account_group: str,
    mapped_cids: set[str],
    explicit_cids: set[str],
    logger,
) -> list[AdsAccount]:
    selected_cids: set[str] = set()
    if account_group:
        selected_cids.update(mapped_cids)
    selected_cids.update(explicit_cids)

    if not selected_cids:
        return pick_accounts(discovered_accounts)

    selected = [account for account in discovered_accounts if account.cid_digits in selected_cids]
    if not selected:
        raise RuntimeError(
            "No discovered accounts match configured CID filters. "
            "Check account_mapping.json / --account-group / --account-cids."
        )

    if logger:
        logger.info(
            "account auto-selection applied group=%s selected_count=%s selected_cids=%s",
            account_group or "-",
            len(selected),
            sorted(selected_cids),
        )
    return selected


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Windows-local Google Ads exporter")
    parser.add_argument(
        "--env-file",
        default=".env",
        help="Path to .env file (default: ./.env).",
    )
    head_mode_group = parser.add_mutually_exclusive_group()
    head_mode_group.add_argument("--headless", dest="headless", action="store_true", help="Run browser headless")
    head_mode_group.add_argument("--headed", dest="headless", action="store_false", help="Force headed browser")
    parser.set_defaults(headless=None)
    parser.add_argument(
        "--download",
        action="store_true",
        default=True,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--browser",
        default="",
        help="Browser launch strategy (default: msedge)",
    )
    parser.add_argument(
        "--target-map",
        default="",
        help="Target mapping JSON path (default: config/google/target_mapping.json).",
    )
    parser.add_argument(
        "--account-map",
        default="",
        help="Account-group mapping JSON path (default: config/google/account_mapping.json).",
    )
    parser.add_argument(
        "--account-group",
        default="",
        help="Account group key from account mapping JSON (e.g. innisfree_main).",
    )
    parser.add_argument(
        "--account-cids",
        default="",
        help="Comma-separated CID list for non-interactive account selection.",
    )
    parser.add_argument(
        "--runtime-dir",
        default="",
        help="Base runtime directory for output/logs/user_data.",
    )
    parser.add_argument(
        "--output-dir",
        default="",
        help="Explicit CSV output directory (overrides runtime/date path).",
    )
    parser.add_argument(
        "--logs-dir",
        default="",
        help="Explicit log directory.",
    )
    parser.add_argument(
        "--user-data-dir",
        default="",
        help="Explicit browser profile directory root.",
    )
    return parser.parse_args()


def process_account(
    page,
    account: AdsAccount,
    enable_download: bool,
    logger,
) -> list[DownloadResult] | None:
    items, matched_map = scan_account_saved_reports(page=page, account=account, logger=logger)

    action = confirm_scan_results(
        account,
        items,
        download_enabled=enable_download,
        scan_only_enabled=False,
        matched_map=matched_map,
    )
    logger.info(
        "scan confirmation action=%s account=%s (%s)",
        action,
        account.name,
        account.cid,
    )
    if action == "cancel":
        return None
    if action != "proceed_download":
        return []

    # proceed_download path
    return _download_targets_for_account(page, account, matched_map, logger=logger)


def ensure_account_report_editor_ready(page, account: AdsAccount, logger) -> None:
    assert_session_active(page, logger=logger)

    selector_seen = open_report_editor(page, logger=logger)
    selector_retry_used = False
    account_verified = False

    selector_present, switched = _switch_account_if_selector_present(page, account, logger=logger, stage="initial_open")
    if selector_seen or selector_present:
        account_verified = switched and assert_current_account(page, account, logger=logger)
    else:
        account_verified = assert_current_account(page, account, logger=logger)

    if not account_verified:
        selector_retry_used = True
        logger.info(
            "Account verification failed. Retrying account selector flow for %s (%s).",
            account.name,
            account.cid,
        )
        selector_seen_retry = open_report_editor(page, logger=logger)
        selector_present_retry, switched = _switch_account_if_selector_present(
            page, account, logger=logger, stage="retry_open"
        )
        if selector_seen_retry or selector_present_retry:
            account_verified = switched and assert_current_account(page, account, logger=logger)
        else:
            account_verified = False

    logger.info(
        "account flow state | selector_seen=%s | account_verified=%s | selector_retry_used=%s",
        selector_seen,
        account_verified,
        selector_retry_used,
    )

    if not account_verified:
        raise RuntimeError("account switch failed")

    if not is_report_editor_ready(page):
        logger.warning(
            "report editor not ready after account verify. Reopening report editor once for account=%s (%s).",
            account.name,
            account.cid,
        )
        selector_seen_reopen = open_report_editor(page, logger=logger)
        if selector_seen_reopen:
            switched = click_account_in_reporteditor_selector(page, account, logger=logger)
            account_verified = switched and assert_current_account(page, account, logger=logger)
            if not account_verified:
                raise RuntimeError("account switch failed")

    if not is_report_editor_ready(page):
        raise RuntimeError("report editor load failed")


def scan_account_saved_reports(
    *,
    page,
    account: AdsAccount,
    logger,
) -> tuple[list[SavedReportItem], dict[str, SavedReportItem]]:
    ensure_account_report_editor_ready(page=page, account=account, logger=logger)
    assert_session_active(page, logger=logger)
    items = _scan_saved_reports_with_retries(page, logger=logger)
    matched_map = match_targets(items, logger=logger)
    return items, matched_map


def _scan_saved_reports_with_retries(page, logger) -> list[SavedReportItem]:
    last_error = None
    for attempt in range(SAVED_REPORT_SCAN_RETRIES + 1):
        try:
            items = scan_saved_reports(page, logger=logger)
            if items:
                return items
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            logger.warning("scan attempt failed attempt=%s reason=%s", attempt + 1, exc)
    if last_error:
        logger.warning("scan retries exhausted. returning empty list. reason=%s", last_error)
    return []


def _download_targets_for_account(
    page,
    account: AdsAccount,
    matched_map: dict[str, SavedReportItem],
    logger,
    progress_callback: Callable[[AdsAccount, str, str, str | None], None] | None = None,
    target_keys: set[str] | None = None,
) -> list[DownloadResult]:
    output_dir = get_run_output_dir()
    if logger:
        logger.info("download output directory=%s", output_dir)
    results: list[DownloadResult] = []

    def emit_progress(target_key: str, status: str, detail: str | None = None) -> None:
        if progress_callback is None:
            return
        try:
            progress_callback(account, target_key, status, detail)
        except Exception as exc:  # noqa: BLE001
            if logger:
                logger.warning(
                    "progress callback failed account=%s target=%s status=%s reason=%s",
                    account.cid,
                    target_key,
                    status,
                    exc,
                )

    for target_key in TARGET_ORDER:
        if target_keys and target_key not in target_keys:
            continue
        item = matched_map.get(target_key)
        if not item:
            emit_progress(target_key, "not_found", "report not found")
            results.append(
                DownloadResult(
                    target_key=target_key,
                    success=False,
                    reason="report not found",
                )
            )
            continue

        assert_session_active(page, logger=logger)
        logger.info(
            "download target start | key=%s | name=%s | type=%s",
            target_key,
            item.visible_name,
            item.inferred_type,
        )
        emit_progress(target_key, "downloading", item.visible_name)

        if not _ensure_report_editor_download_context(page, account, logger=logger, target_item=item):
            emit_progress(target_key, "failed", "report editor restore failed")
            results.append(
                DownloadResult(
                    target_key=target_key,
                    success=False,
                    reason="report editor restore failed",
                )
            )
            continue

        result = download_item(page, account, item, output_dir, logger=logger)
        results.append(result)
        logger.info(
            "download target result | key=%s | success=%s | reason=%s | filename=%s",
            target_key,
            result.success,
            result.reason,
            result.filename,
        )
        if result.success:
            emit_progress(target_key, "downloaded", result.filename)
        else:
            emit_progress(target_key, "failed", result.reason or "download failed")

        # Always return to Saved reports before next target.
        restored = _ensure_report_editor_download_context(page, account, logger=logger)
        if logger:
            logger.info(
                "post-download restore | target=%s | restored=%s",
                target_key,
                restored,
            )
    return results


def _ensure_report_editor_download_context(page, account: AdsAccount, logger, target_item: SavedReportItem | None = None) -> bool:
    restore_reason: str | None = None

    _wait_for_saved_reports_ready(page, target_item=target_item, timeout_ms=2200, logger=logger)
    state = _capture_restore_state(page, target_item)
    _log_restore_state(logger, "start", state)
    if _restore_success(state):
        if logger:
            logger.info(
                "download context restore fast-path | account=%s (%s) | target_ok=%s",
                account.name,
                account.cid,
                state["target_row_visible"],
            )
        return True

    # Step 1: try view close button first.
    if _is_report_view_page(page) or _has_report_view_close_button(page):
        clicked = _click_report_view_close(page, logger=logger)
        if not clicked:
            restore_reason = "close_click_failed"
            _log_restore_state(logger, "after_close_click_failed", _capture_restore_state(page, target_item), restore_reason)
        else:
            page.wait_for_timeout(900)
            _wait_for_saved_reports_ready(page, target_item=target_item, timeout_ms=3600, logger=logger)
            state = _capture_restore_state(page, target_item)
            _log_restore_state(logger, "after_close_click", state)
            if _restore_success(state):
                if logger:
                    logger.info(
                        "download context restore success via close | account=%s (%s)",
                        account.name,
                        account.cid,
                    )
                return True

    # Step 2: browser back.
    try:
        page.go_back(wait_until="domcontentloaded", timeout=5000)
        page.wait_for_timeout(900)
        _wait_for_saved_reports_ready(page, target_item=target_item, timeout_ms=3600, logger=logger)
        state = _capture_restore_state(page, target_item)
        _log_restore_state(logger, "after_back", state)
        if _restore_success(state):
            if logger:
                logger.info(
                    "download context restore success via back | account=%s (%s)",
                    account.name,
                    account.cid,
                )
            return True
    except Exception as exc:  # noqa: BLE001
        restore_reason = restore_reason or "back_failed"
        if logger:
            logger.info("download context back failed | reason=%s", exc)
        _log_restore_state(logger, "after_back_failed", _capture_restore_state(page, target_item), restore_reason)

    # Step 3: reopen report editor exactly once.
    selector_seen = open_report_editor(page, logger=logger)
    selector_present, switched = _switch_account_if_selector_present(
        page, account, logger=logger, stage="reopen"
    )
    if selector_seen or selector_present:
        if not switched:
            restore_reason = restore_reason or "account_switch_failed"
            _log_restore_state(logger, "reopen_switch_failed", _capture_restore_state(page, target_item), restore_reason)
            return False

    _expand_saved_reports_panel(page)
    _wait_for_saved_reports_ready(page, target_item=target_item, timeout_ms=6500, logger=logger)
    state = _capture_restore_state(page, target_item)
    _log_restore_state(logger, "after_reopen", state)

    verified = assert_current_account(page, account, logger=logger)
    ok = _restore_success(state) and verified
    if not _restore_success(state) and not restore_reason:
        restore_reason = "base_ready_timeout"
    if logger:
        logger.info(
            "download context restore reopen | account=%s (%s) | selector_seen=%s | verified=%s | ok=%s | reason=%s",
            account.name,
            account.cid,
            selector_seen,
            verified,
            ok,
            restore_reason,
        )
    return ok


def _switch_account_if_selector_present(page, account: AdsAccount, logger=None, stage: str = "") -> tuple[bool, bool]:
    """
    Re-check selector presence after navigation and click the working account when visible.
    Returns (selector_present, switched_ok).
    """
    selector_present = False
    try:
        selector_present = account_selector_visible(page)
    except Exception:  # noqa: BLE001
        selector_present = False

    if not selector_present:
        page.wait_for_timeout(700)
        try:
            selector_present = account_selector_visible(page)
        except Exception:  # noqa: BLE001
            selector_present = False

    if not selector_present:
        if logger:
            logger.info("selector recheck | stage=%s | present=%s", stage, selector_present)
        return False, True

    switched = click_account_in_reporteditor_selector(page, account, logger=logger)
    if logger:
        logger.info(
            "selector recheck | stage=%s | present=%s | switched=%s | account=%s (%s)",
            stage,
            selector_present,
            switched,
            account.name,
            account.cid,
        )
    return True, switched


def _is_saved_reports_list_ready(page) -> bool:
    if _is_report_view_page(page):
        return False
    if not _is_report_editor_base_page(page):
        return False
    return _saved_reports_data_row_count(page) > 0


def _wait_for_saved_reports_ready(page, target_item: SavedReportItem | None, timeout_ms: int, logger=None) -> bool:
    elapsed = 0
    while elapsed < timeout_ms:
        _expand_saved_reports_panel(page)
        _try_set_show_rows_to_500_for_restore(page)
        state = _capture_restore_state(page, target_item)
        if _restore_success(state):
            return True
        if state["data_row_count"] == 0:
            _scroll_saved_reports_for_target_lookup(page)
        try:
            page.evaluate("() => window.scrollBy(0, 300)")
            page.wait_for_timeout(120)
            page.evaluate("() => window.scrollBy(0, -220)")
        except Exception:  # noqa: BLE001
            pass
        page.wait_for_timeout(350)
        elapsed += 350

    if logger:
        logger.info(
            "saved reports ready wait timeout | timeout_ms=%s | target=%s | url=%s",
            timeout_ms,
            target_item.visible_name if target_item else None,
            page.url,
        )
    return False


def _is_target_row_ready(page, target_item: SavedReportItem | None) -> bool:
    if not target_item:
        return True
    if _target_row_visible_once(page, target_item.visible_name):
        return True

    _try_set_show_rows_to_500_for_restore(page)
    for _ in range(10):
        if _target_row_visible_once(page, target_item.visible_name):
            return True
        _scroll_saved_reports_for_target_lookup(page)
    return _target_row_visible_once(page, target_item.visible_name)


def _target_row_visible_once(page, visible_name: str) -> bool:
    try:
        locator = page.locator("[essfield='definition.report_name'] .report-name-text").filter(
            has_text=visible_name
        )
        if locator.count() > 0:
            return True
    except Exception:  # noqa: BLE001
        pass
    try:
        rows = page.locator("div.particle-table-row[role='row']").filter(has_text=visible_name)
        return rows.count() > 0
    except Exception:  # noqa: BLE001
        return False


def _try_set_show_rows_to_500_for_restore(page) -> None:
    if _is_report_view_page(page):
        return
    button = page.locator("div[role='button'][aria-label*='Show rows']").first
    if button.count() == 0:
        return
    try:
        text_value = (button.locator("span.button-text").first.inner_text(timeout=700) or "").strip()
        aria_label = (button.get_attribute("aria-label") or "").strip()
        if text_value == "500" or "500 selected" in aria_label:
            return
    except Exception:  # noqa: BLE001
        pass
    try:
        button.click(timeout=1500)
        listbox = page.locator(
            "material-list[role='listbox'][aria-label*='Choose number of rows to be displayed per page']"
        ).first
        if listbox.count() == 0:
            listbox = page.locator("material-list[role='listbox']").first
        listbox.wait_for(state="visible", timeout=2500)
        option = listbox.locator("[role='option']").filter(has_text="500").first
        if option.count() > 0:
            option.click(timeout=1500)
            page.wait_for_timeout(350)
    except Exception:  # noqa: BLE001
        return


def _scroll_saved_reports_for_target_lookup(page) -> None:
    try:
        region = page.locator("material-expansionpanel:has(div[aria-label='Saved reports']) div.main[role='region']").first
        if region.count() > 0:
            region.evaluate(
                """(el) => {
                    try {
                        const before = el.scrollTop || 0;
                        const delta = Math.max(280, Math.floor((el.clientHeight || 650) * 0.75));
                        el.scrollTop = before + delta;
                        el.dispatchEvent(new Event('scroll', { bubbles: true }));
                    } catch (e) {}
                }"""
            )
            page.wait_for_timeout(160)
            return
    except Exception:  # noqa: BLE001
        pass
    try:
        page.evaluate("() => window.scrollBy(0, 500)")
        page.wait_for_timeout(160)
    except Exception:  # noqa: BLE001
        pass


def _saved_reports_data_row_count(page) -> int:
    try:
        count = page.locator("div.particle-table-row[role='row']").count()
        if count > 0:
            return count
    except Exception:  # noqa: BLE001
        pass
    try:
        name_cells = page.locator("[essfield='definition.report_name']").count()
        # header only state has exactly one "Reports" cell.
        if name_cells > 1:
            return name_cells - 1
    except Exception:  # noqa: BLE001
        pass
    return 0


def _is_saved_reports_panel_expanded(page) -> bool:
    panel = page.locator("material-expansionpanel:has(div[role='button'][aria-label='Saved reports'])").first
    if panel.count() == 0:
        return False
    region = panel.locator("div.main[role='region'], div[role='region']").first
    if region.count() == 0:
        return False
    try:
        hidden = (region.get_attribute("aria-hidden") or "").strip().lower()
        return hidden == "false"
    except Exception:  # noqa: BLE001
        return False


def _capture_restore_state(page, target_item: SavedReportItem | None) -> dict:
    url_has_view = _is_report_view_page(page)
    data_row_count = _saved_reports_data_row_count(page)
    target_row_visible = _is_target_row_ready(page, target_item)
    panel_expanded = _is_saved_reports_panel_expanded(page)
    return {
        "url_has_view": url_has_view,
        "data_row_count": data_row_count,
        "target_row_visible": target_row_visible,
        "panel_expanded": panel_expanded,
    }


def _restore_success(state: dict) -> bool:
    return bool(
        (not state["url_has_view"])
        and state["data_row_count"] > 0
        and state["target_row_visible"]
    )


def _log_restore_state(logger, stage: str, state: dict, reason: str | None = None) -> None:
    if not logger:
        return
    logger.info(
        "download restore state | stage=%s | url_has_view=%s | data_row_count=%s | target_row_visible=%s | panel_expanded=%s | reason=%s",
        stage,
        state["url_has_view"],
        state["data_row_count"],
        state["target_row_visible"],
        state["panel_expanded"],
        reason,
    )


def _expand_saved_reports_panel(page) -> None:
    panel = page.locator("material-expansionpanel:has(div[role='button'][aria-label='Saved reports'])").first
    if panel.count() == 0:
        return
    header = panel.locator("div[role='button'][aria-label='Saved reports']").first
    region = panel.locator("div.main[role='region'], div[role='region']").first
    try:
        hidden = (region.get_attribute("aria-hidden") or "").strip().lower() if region.count() > 0 else ""
        if hidden == "false":
            return
    except Exception:  # noqa: BLE001
        pass
    try:
        header.click(timeout=1500)
    except Exception:  # noqa: BLE001
        try:
            panel.get_by_text("Saved reports", exact=False).first.click(timeout=1500)
        except Exception:  # noqa: BLE001
            return
    page.wait_for_timeout(500)


def _is_report_view_page(page) -> bool:
    url = (page.url or "").lower()
    return "/aw/reporteditor/view" in url


def _is_report_editor_base_page(page) -> bool:
    url = (page.url or "").lower()
    return "/aw/reporteditor" in url and "/aw/reporteditor/view" not in url


def _has_report_view_close_button(page) -> bool:
    selectors = (
        "awsm-app-bar material-button[aria-label='close'][role='button']",
        "awsm-app-bar material-button[aria-label='Close'][role='button']",
        "awsm-app-bar material-button.back-button[aria-label='close']",
        "awsm-app-bar material-button.back-button[aria-label='Close']",
        "awsm-app-bar material-button[iconminerva-id='app-bar-primary-action-button'][aria-label='close']",
        "awsm-app-bar material-button[iconminerva-id='app-bar-primary-action-button'][aria-label='Close']",
        "material-button.back-button[aria-label='close']",
        "material-button.back-button[aria-label='Close']",
        "[aria-label='close'][role='button']",
        "[aria-label='Close'][role='button']",
    )
    for selector in selectors:
        try:
            if page.locator(selector).count() > 0:
                return True
        except Exception:  # noqa: BLE001
            continue
    try:
        candidate = page.get_by_role("button", name=re.compile("close", re.IGNORECASE)).first
        return candidate.count() > 0 and candidate.is_visible(timeout=500)
    except Exception:  # noqa: BLE001
        return False


def _return_to_saved_reports_from_view(page, logger=None) -> bool:
    """
    Prefer browser back to return from /aw/reporteditor/view.
    If back doesn't work, click the top app-bar close (X) button.
    """
    if _is_saved_reports_list_ready(page):
        return True

    for attempt in range(3):
        if _is_report_view_page(page):
            try:
                page.go_back(wait_until="domcontentloaded", timeout=5000)
                page.wait_for_timeout(700)
                if _wait_for_saved_reports_ready(page, target_item=None, timeout_ms=4000, logger=logger):
                    if logger:
                        logger.info("return from report view via back success attempt=%s", attempt + 1)
                    return True
            except Exception as exc:  # noqa: BLE001
                if logger:
                    logger.info("return via back skipped attempt=%s reason=%s", attempt + 1, exc)

        clicked = _click_report_view_close(page, logger=logger)
        if clicked:
            page.wait_for_timeout(800)
            if _wait_for_saved_reports_ready(page, target_item=None, timeout_ms=4500, logger=logger):
                if logger:
                    logger.info("return from report view via close success attempt=%s", attempt + 1)
                return True

    return _is_saved_reports_list_ready(page)


def _click_report_view_close(page, logger=None) -> bool:
    selectors = (
        "awsm-app-bar material-button[aria-label='close'][role='button']",
        "awsm-app-bar material-button[aria-label='Close'][role='button']",
        "awsm-app-bar material-button.back-button[aria-label='close']",
        "awsm-app-bar material-button.back-button[aria-label='Close']",
        "awsm-app-bar material-button[iconminerva-id='app-bar-primary-action-button'][aria-label='close']",
        "awsm-app-bar material-button[iconminerva-id='app-bar-primary-action-button'][aria-label='Close']",
        "material-button.back-button[aria-label='close']",
        "material-button.back-button[aria-label='Close']",
        "[aria-label='close'][role='button']",
        "[aria-label='Close'][role='button']",
    )
    for selector in selectors:
        try:
            button = page.locator(selector).first
            if button.count() == 0:
                continue
            button.click(timeout=2000)
            if logger:
                logger.info("clicked report view close button selector=%s", selector)
            return True
        except Exception as exc:  # noqa: BLE001
            if logger:
                logger.info("report view close click failed selector=%s reason=%s", selector, exc)
            continue

    try:
        button = page.get_by_role("button", name=re.compile("close", re.IGNORECASE)).first
        if button.count() > 0:
            button.click(timeout=2000)
            if logger:
                logger.info("clicked report view close button by role name=Close")
            return True
    except Exception as exc:  # noqa: BLE001
        if logger:
            logger.info("report view close click failed by role reason=%s", exc)

    return False


def show_error_dialog(message: str) -> None:
    print(message)


def run() -> int:
    args = parse_args()
    load_env_file(args.env_file)

    try:
        browser_preference = _resolve_browser_preference(args.browser)
    except ValueError:
        show_error_dialog(
            "Unsupported browser value. Use one of: msedge, chrome, auto, chromium."
        )
        return 1
    headless = _resolve_bool(args.headless, "GOOGLE_ADS_HEADLESS", default=False)

    _apply_runtime_directory_overrides(
        runtime_dir=_resolve_value(args.runtime_dir, "GOOGLE_ADS_RUNTIME_DIR"),
        output_dir=_resolve_value(args.output_dir, "GOOGLE_ADS_OUTPUT_DIR"),
        logs_dir=_resolve_value(args.logs_dir, "GOOGLE_ADS_LOGS_DIR"),
        user_data_dir=_resolve_value(args.user_data_dir, "GOOGLE_ADS_USER_DATA_DIR"),
    )

    logger, log_path = setup_logger()
    target_map_path = _resolve_value(
        args.target_map,
        "GOOGLE_ADS_TARGET_MAP",
        default=str(_default_target_map_path()),
    )
    account_map_path = _resolve_value(
        args.account_map,
        "GOOGLE_ADS_ACCOUNT_MAP",
        default=str(_default_account_map_path()),
    )
    account_group = _resolve_value(args.account_group, "GOOGLE_ADS_ACCOUNT_GROUP")
    explicit_cids = _parse_cid_filters(
        _resolve_value(args.account_cids, "GOOGLE_ADS_ACCOUNT_CIDS")
    )

    try:
        load_target_mapping_file(target_map_path, logger=logger)
        account_mapping = load_account_mapping(account_map_path, logger=logger)
    except (TargetMappingError, AccountMappingError) as exc:
        logger.error("configuration load failed: %s", exc)
        show_error_dialog(str(exc))
        return 1

    if not account_group and account_mapping.default_group:
        account_group = account_mapping.default_group

    mapped_cids: set[str] = set()
    if account_group:
        if account_group not in account_mapping.groups:
            message = (
                f"Configured account_group `{account_group}` not found in mapping file: {account_map_path}"
            )
            logger.error(message)
            show_error_dialog(message)
            return 1
        mapped_cids.update(account_mapping.groups[account_group])

    logger.info(
        "start | browser=%s | download=%s | headless=%s | target_map=%s | account_map=%s | account_group=%s | explicit_cid_count=%s | log=%s",
        browser_preference,
        args.download,
        headless,
        target_map_path,
        account_map_path,
        account_group or "-",
        len(explicit_cids),
        log_path,
    )

    download_results: dict[str, list[DownloadResult]] = {}

    try:
        with sync_playwright() as playwright:
            context, page, browser_used = launch_ads_context(
                playwright=playwright,
                headless=headless,
                browser_preference=browser_preference,
                logger=logger,
            )
            logger.info("active browser=%s", browser_used)

            try:
                page = ensure_logged_in(page, logger=logger)
                discovered_accounts = collect_accounts(page, logger=logger)
                selected_accounts = _select_accounts_for_run(
                    discovered_accounts=discovered_accounts,
                    account_group=account_group,
                    mapped_cids=mapped_cids,
                    explicit_cids=explicit_cids,
                    logger=logger,
                )
                if not selected_accounts:
                    logger.info("No account selected by user. Exiting.")
                    return 0
                if not headless:
                    minimized = minimize_browser_window(page, logger=logger)
                    logger.info("post-account-selection minimize | success=%s", minimized)

                for account in selected_accounts:
                    logger.info("processing account=%s (%s)", account.name, account.cid)
                    result = process_account(
                        page=page,
                        account=account,
                        enable_download=args.download,
                        logger=logger,
                    )
                    if result is None:
                        logger.info("User cancelled account=%s (%s)", account.name, account.cid)
                        continue
                    if result:
                        download_results[f"{account.name} | {account.cid}"] = result
            finally:
                try:
                    context.close()
                except Exception as close_exc:  # noqa: BLE001
                    logger.warning("context close skipped: %s", close_exc)

        if download_results:
            show_download_results(download_results)
        logger.info("run completed")
        return 0

    except Exception as exc:  # noqa: BLE001
        logger.error("run failed: %s", exc)
        logger.error(traceback.format_exc())
        show_error_dialog(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
