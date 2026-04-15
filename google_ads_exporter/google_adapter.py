"""Adapter functions to run Google exporter phases with progress callbacks."""

from __future__ import annotations

import asyncio
import datetime as dt
import multiprocessing as mp
import os
import queue
import sys
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .account_discovery import collect_accounts
from .auth import (
    ensure_logged_in,
    get_browser_window_state,
    launch_ads_context,
    maximize_browser_window,
    minimize_browser_window,
)
from .google_excel_builder import create_unified_workbook_for_account, summaries_as_rows
from .main import _download_targets_for_account, ensure_account_report_editor_ready, scan_account_saved_reports
from .models import AdsAccount, DownloadResult, SavedReportItem
from .targets import TARGET_DISPLAY_NAMES, TARGET_ORDER, TargetMappingError, load_target_mapping_file
from .utils import setup_logger

ProgressCallback = Callable[[dict[str, Any]], None]

WORKER_POLL_INTERVAL_SEC = 0.2
WORKER_HEARTBEAT_INTERVAL_SEC = 2.0
WORKER_HEARTBEAT_TIMEOUT_SEC = 120.0
LOGIN_WORKER_TIMEOUT_SEC = 60.0 * 45.0
EXPORT_WORKER_TIMEOUT_SEC = 60.0 * 90.0


def _now_run_id() -> str:
    return dt.datetime.now().strftime("%Y%m%d_%H%M%S")


def _emit(progress_cb: ProgressCallback | None, payload: dict[str, Any]) -> None:
    if progress_cb:
        progress_cb(payload)


def _row_id(*, cid_digits: str, activity_key: str, target_key: str) -> str:
    return (
        f"{str(cid_digits or '').strip()}::"
        f"{str(activity_key or '').strip()}::"
        f"{str(target_key or '').strip()}"
    )


def _exc_text(exc: Exception) -> str:
    text = str(exc or "").strip()
    if text:
        return text
    rep = repr(exc)
    if rep:
        return rep
    return exc.__class__.__name__


def _focus_browser_page(page, logger=None) -> None:
    try:
        page.bring_to_front()
    except Exception as exc:  # noqa: BLE001
        if logger:
            logger.info("browser bring_to_front skipped: %s", exc)


def _default_window_policy() -> dict[str, Any]:
    return {
        "keep_minimized": False,
        "user_override": False,
        "last_auto_state": "",
    }


def _maximize_browser_page(page, logger=None) -> None:
    pages = [page]
    try:
        context_pages = [candidate for candidate in page.context.pages if not candidate.is_closed()]
        if context_pages:
            pages = context_pages
    except Exception:  # noqa: BLE001
        pages = [page]

    success = False
    for candidate in pages:
        success = maximize_browser_window(candidate, logger=logger) or success
    if logger:
        logger.info("browser maximize request | success=%s | page_count=%s", success, len(pages))


def _detect_user_window_override(page, window_policy: dict[str, Any], logger=None) -> bool:
    if not window_policy.get("keep_minimized"):
        return False
    if window_policy.get("user_override"):
        return True
    if not window_policy.get("last_auto_state"):
        return False

    state = get_browser_window_state(page, logger=logger)
    if not state:
        return False
    if state == str(window_policy.get("last_auto_state", "")).strip().lower():
        return False

    window_policy["user_override"] = True
    if logger:
        logger.info(
            "browser window override detected | observed_state=%s | last_auto_state=%s",
            state,
            window_policy.get("last_auto_state"),
        )
    return True


def _minimize_browser_page(page, logger=None, window_policy: dict[str, Any] | None = None) -> None:
    policy = window_policy or _default_window_policy()
    if policy.get("keep_minimized") and _detect_user_window_override(page, policy, logger=logger):
        if logger:
            logger.info("browser minimize keepalive skipped due to user override")
        return

    pages = [page]
    try:
        context_pages = [candidate for candidate in page.context.pages if not candidate.is_closed()]
        if context_pages:
            pages = context_pages
    except Exception:  # noqa: BLE001
        pages = [page]

    success = False
    for candidate in pages:
        minimized = minimize_browser_window(candidate, logger=logger)
        success = success or minimized

    if success and policy.get("keep_minimized"):
        policy["last_auto_state"] = "minimized"

    if logger:
        logger.info(
            "browser minimize keepalive | success=%s | page_count=%s | keep_minimized=%s | user_override=%s",
            success,
            len(pages),
            policy.get("keep_minimized"),
            policy.get("user_override"),
        )


def _account_to_dict(account: AdsAccount) -> dict[str, Any]:
    return {
        "name": account.name,
        "cid": account.cid,
        "cid_digits": account.cid_digits,
        "is_manager": bool(account.is_manager),
        "raw_text": account.raw_text,
    }


def _account_from_dict(payload: dict[str, Any]) -> AdsAccount:
    return AdsAccount(
        name=str(payload.get("name") or ""),
        cid=str(payload.get("cid") or ""),
        cid_digits=str(payload.get("cid_digits") or ""),
        is_manager=bool(payload.get("is_manager")),
        raw_text=str(payload.get("raw_text") or ""),
    )


def _saved_report_to_dict(item: SavedReportItem) -> dict[str, Any]:
    return {
        "visible_name": item.visible_name,
        "normalized_name": item.normalized_name,
        "inferred_type": item.inferred_type,
        "activity_name": item.activity_name or "",
        "activity_key": item.activity_key or "",
        "row_text": item.row_text,
        "matched_key": item.matched_key,
        "owner_text": item.owner_text,
        "created_by": item.created_by,
        "creation_date": item.creation_date,
        "last_accessed": item.last_accessed,
        "date_range": item.date_range,
        "has_download_text": bool(item.has_download_text),
        "has_download_icon": bool(item.has_download_icon),
    }


def _saved_report_from_dict(payload: dict[str, Any]) -> SavedReportItem:
    return SavedReportItem(
        visible_name=str(payload.get("visible_name") or ""),
        normalized_name=str(payload.get("normalized_name") or ""),
        inferred_type=str(payload.get("inferred_type") or "unknown"),
        activity_name=str(payload.get("activity_name") or "") or None,
        activity_key=str(payload.get("activity_key") or "") or None,
        row_text=str(payload.get("row_text") or ""),
        matched_key=payload.get("matched_key"),
        owner_text=payload.get("owner_text"),
        created_by=payload.get("created_by"),
        creation_date=payload.get("creation_date"),
        last_accessed=payload.get("last_accessed"),
        date_range=payload.get("date_range"),
        has_download_text=bool(payload.get("has_download_text")),
        has_download_icon=bool(payload.get("has_download_icon")),
    )


def _serialize_scan_results(scan_results: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    payload: dict[str, dict[str, Any]] = {}
    for cid_digits, item in scan_results.items():
        account_obj = item.get("account")
        account_payload = _account_to_dict(account_obj) if isinstance(account_obj, AdsAccount) else {}
        matched_map_by_activity = item.get("matched_map_by_activity", {})
        matched_payload_by_activity: dict[str, dict[str, dict[str, Any]]] = {}
        if isinstance(matched_map_by_activity, dict):
            for activity_key, matched_map in matched_map_by_activity.items():
                if not isinstance(matched_map, dict):
                    continue
                activity_payload: dict[str, dict[str, Any]] = {}
                for target_key, report_item in matched_map.items():
                    if isinstance(report_item, SavedReportItem):
                        activity_payload[str(target_key)] = _saved_report_to_dict(report_item)
                if activity_payload:
                    matched_payload_by_activity[str(activity_key)] = activity_payload
        payload[str(cid_digits)] = {
            "account": account_payload,
            "matched_map_by_activity": matched_payload_by_activity,
            "error": str(item.get("error") or ""),
        }
    return payload


def _deserialize_scan_results(payload: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    for cid_digits, item in payload.items():
        account_payload = item.get("account", {})
        matched_payload_by_activity = item.get("matched_map_by_activity", {})
        matched_map_by_activity: dict[str, dict[str, SavedReportItem]] = {}
        if isinstance(matched_payload_by_activity, dict):
            for activity_key, matched_payload in matched_payload_by_activity.items():
                if not isinstance(matched_payload, dict):
                    continue
                matched_map: dict[str, SavedReportItem] = {}
                for target_key, report_data in matched_payload.items():
                    if isinstance(report_data, dict):
                        matched_map[str(target_key)] = _saved_report_from_dict(report_data)
                if matched_map:
                    matched_map_by_activity[str(activity_key)] = matched_map
        account = _account_from_dict(account_payload) if isinstance(account_payload, dict) else AdsAccount("", "", "")
        results[str(cid_digits)] = {
            "account": account,
            "items": [item for activity_map in matched_map_by_activity.values() for item in activity_map.values()],
            "matched_map_by_activity": matched_map_by_activity,
            "error": str(item.get("error") or ""),
        }
    return results


def _scan_results_as_rows(scan_results: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in scan_results.values():
        account = item.get("account")
        account_name = account.name if isinstance(account, AdsAccount) else "-"
        account_cid = account.cid if isinstance(account, AdsAccount) else "-"
        matched_map_by_activity = item.get("matched_map_by_activity", {})
        if not isinstance(matched_map_by_activity, dict):
            matched_map_by_activity = {}
        for activity_key in sorted(matched_map_by_activity.keys()):
            matched_map = matched_map_by_activity.get(activity_key, {})
            if not isinstance(matched_map, dict):
                continue
            activity_name = _resolve_activity_name(activity_key=activity_key, matched_map=matched_map)
            for target_key in TARGET_ORDER:
                report_item = matched_map.get(target_key)
                rows.append(
                    {
                        "account": account_name,
                        "cid": account_cid,
                        "activity": activity_name,
                        "activity_key": activity_key,
                        "target_key": target_key,
                        "target_display": TARGET_DISPLAY_NAMES.get(target_key, target_key),
                        "matched_report": report_item.visible_name if isinstance(report_item, SavedReportItem) else "-",
                        "owner": (
                            report_item.owner_text
                            if isinstance(report_item, SavedReportItem) and report_item.owner_text
                            else "-"
                        ),
                        "creation_date": (
                            report_item.creation_date
                            if isinstance(report_item, SavedReportItem) and report_item.creation_date
                            else "-"
                        ),
                        "date_range": (
                            report_item.date_range
                            if isinstance(report_item, SavedReportItem) and report_item.date_range
                            else "-"
                        ),
                        "created_by": (
                            report_item.created_by
                            if isinstance(report_item, SavedReportItem) and report_item.created_by
                            else "-"
                        ),
                        "status": "matched" if isinstance(report_item, SavedReportItem) else "not found",
                    }
                )
    return rows


def _resolve_activity_name(*, activity_key: str, matched_map: dict[str, SavedReportItem]) -> str:
    for report_item in matched_map.values():
        if isinstance(report_item, SavedReportItem) and report_item.activity_name:
            return str(report_item.activity_name)
    return activity_key


def _ensure_playwright_event_loop_policy(logger=None) -> None:
    if sys.platform != "win32":
        return
    policy_cls = getattr(asyncio, "WindowsProactorEventLoopPolicy", None)
    if policy_cls is None:
        return
    current_policy = asyncio.get_event_loop_policy()
    if isinstance(current_policy, policy_cls):
        return
    try:
        asyncio.set_event_loop_policy(policy_cls())
        if logger:
            logger.info("WindowsProactorEventLoopPolicy enabled for Playwright subprocess support.")
    except Exception as exc:  # noqa: BLE001
        if logger:
            logger.warning("failed to set Proactor event loop policy: %s", exc)


def _start_worker_heartbeat(event_queue: Any) -> tuple[threading.Event, threading.Thread]:
    stop_event = threading.Event()

    def _loop() -> None:
        while not stop_event.wait(WORKER_HEARTBEAT_INTERVAL_SEC):
            try:
                event_queue.put({"type": "worker_heartbeat", "ts": dt.datetime.now().isoformat()})
            except Exception:  # noqa: BLE001
                return

    thread = threading.Thread(target=_loop, daemon=True)
    thread.start()
    return stop_event, thread


def _worker_login_and_crawl(
    *,
    event_queue: Any,
    result_queue: Any,
    browser: str,
    headless: bool,
    target_map_path: str,
) -> None:
    logger, log_path = setup_logger("google_ads_exporter.worker.login")
    _ensure_playwright_event_loop_policy(logger=logger)
    stop_event, heartbeat_thread = _start_worker_heartbeat(event_queue)
    try:
        accounts, browser_used = login_and_crawl_accounts(
            browser=browser,
            headless=headless,
            target_map_path=target_map_path,
            logger=logger,
            progress_cb=event_queue.put,
        )
        result_queue.put(
            {
                "ok": True,
                "accounts": [_account_to_dict(account) for account in accounts],
                "browser_used": browser_used,
                "log_file": str(log_path),
            }
        )
    except Exception as exc:  # noqa: BLE001
        result_queue.put(
            {
                "ok": False,
                "error": _exc_text(exc),
                "log_file": str(log_path),
            }
        )
    finally:
        stop_event.set()
        heartbeat_thread.join(timeout=1.0)


def _worker_scan_and_export(
    *,
    event_queue: Any,
    result_queue: Any,
    selected_accounts_payload: list[dict[str, Any]],
    browser: str,
    headless: bool,
    target_map_path: str,
    final_output_dir: str,
    downloads_dir: str,
) -> None:
    logger, log_path = setup_logger("google_ads_exporter.worker.export")
    _ensure_playwright_event_loop_policy(logger=logger)
    stop_event, heartbeat_thread = _start_worker_heartbeat(event_queue)
    try:
        selected_accounts = [_account_from_dict(item) for item in selected_accounts_payload]
        scan_results, scan_rows = run_google_export_for_accounts(
            selected_accounts=selected_accounts,
            scan_results={},
            browser=browser,
            headless=headless,
            target_map_path=target_map_path,
            final_output_dir=Path(final_output_dir).expanduser().resolve(),
            downloads_dir=Path(downloads_dir).expanduser().resolve(),
            logger=logger,
            progress_cb=event_queue.put,
            scan_before_export=True,
        )
        result_queue.put(
            {
                "ok": True,
                "scan_results": _serialize_scan_results(scan_results),
                "scan_rows": scan_rows,
                "log_file": str(log_path),
            }
        )
    except Exception as exc:  # noqa: BLE001
        error_text = _exc_text(exc)
        event_queue.put({"type": "run_failed", "error": f"Execution failed: {error_text}"})
        result_queue.put(
            {
                "ok": False,
                "error": error_text,
                "log_file": str(log_path),
            }
        )
    finally:
        stop_event.set()
        heartbeat_thread.join(timeout=1.0)


def _run_worker_process(
    *,
    worker_target,
    worker_kwargs: dict[str, Any],
    progress_cb: ProgressCallback | None,
    timeout_sec: float,
    max_restarts: int = 1,
) -> dict[str, Any]:
    last_error: RuntimeError | None = None
    attempts = max(0, int(max_restarts)) + 1

    for attempt in range(1, attempts + 1):
        try:
            ctx = mp.get_context("spawn")
            event_queue = ctx.Queue()
            result_queue = ctx.Queue()
            process = ctx.Process(
                target=worker_target,
                kwargs={
                    "event_queue": event_queue,
                    "result_queue": result_queue,
                    **worker_kwargs,
                },
                daemon=True,
            )
            process.start()
        except Exception as exc:  # noqa: BLE001
            last_error = RuntimeError(f"Worker bootstrap failed: {_exc_text(exc)}")
            if attempt < attempts:
                _emit(
                    progress_cb,
                    {
                        "type": "run_warning",
                        "message": f"Worker bootstrap failed (attempt {attempt}/{attempts}). Restarting...",
                    },
                )
                continue
            break

        start_monotonic = time.monotonic()
        last_heartbeat = start_monotonic

        def _drain_events() -> None:
            nonlocal last_heartbeat
            while True:
                try:
                    event = event_queue.get_nowait()
                except queue.Empty:
                    break
                if not isinstance(event, dict):
                    continue
                if str(event.get("type") or "") == "worker_heartbeat":
                    last_heartbeat = time.monotonic()
                    continue
                _emit(progress_cb, event)

        try:
            while process.is_alive():
                _drain_events()
                now = time.monotonic()
                if now - start_monotonic > timeout_sec:
                    process.terminate()
                    process.join(timeout=5)
                    raise RuntimeError(f"Worker timeout after {int(timeout_sec)} seconds.")
                if now - last_heartbeat > WORKER_HEARTBEAT_TIMEOUT_SEC:
                    process.terminate()
                    process.join(timeout=5)
                    raise RuntimeError("Worker heartbeat timeout.")
                time.sleep(WORKER_POLL_INTERVAL_SEC)

            _drain_events()
            process.join(timeout=5)

            try:
                result = result_queue.get_nowait()
            except queue.Empty as exc:
                raise RuntimeError("Worker exited without result payload.") from exc
            if not isinstance(result, dict):
                raise RuntimeError("Worker returned invalid result payload.")
            return result
        except RuntimeError as exc:
            last_error = exc
            if attempt < attempts:
                _emit(
                    progress_cb,
                    {
                        "type": "run_warning",
                        "message": f"Worker failed (attempt {attempt}/{attempts}). Restarting...",
                    },
                )
                continue
            break
        finally:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)

    raise RuntimeError(str(last_error or "Worker execution failed."))


def login_and_crawl_accounts_in_subprocess(
    *,
    browser: str,
    headless: bool,
    target_map_path: str,
    progress_cb: ProgressCallback | None = None,
) -> tuple[list[AdsAccount], str, str]:
    result = _run_worker_process(
        worker_target=_worker_login_and_crawl,
        worker_kwargs={
            "browser": browser,
            "headless": headless,
            "target_map_path": target_map_path,
        },
        progress_cb=progress_cb,
        timeout_sec=LOGIN_WORKER_TIMEOUT_SEC,
    )
    if not bool(result.get("ok")):
        raise RuntimeError(str(result.get("error") or "login worker failed"))
    accounts_payload = result.get("accounts") or []
    accounts = [
        _account_from_dict(item)
        for item in accounts_payload
        if isinstance(item, dict)
    ]
    return accounts, str(result.get("browser_used") or ""), str(result.get("log_file") or "")


def run_scan_and_export_in_subprocess(
    *,
    selected_accounts: list[AdsAccount],
    browser: str,
    headless: bool,
    target_map_path: str,
    final_output_dir: Path,
    downloads_dir: Path,
    progress_cb: ProgressCallback | None = None,
) -> dict[str, Any]:
    result = _run_worker_process(
        worker_target=_worker_scan_and_export,
        worker_kwargs={
            "selected_accounts_payload": [_account_to_dict(account) for account in selected_accounts],
            "browser": browser,
            "headless": headless,
            "target_map_path": target_map_path,
            "final_output_dir": str(Path(final_output_dir).expanduser().resolve()),
            "downloads_dir": str(Path(downloads_dir).expanduser().resolve()),
        },
        progress_cb=progress_cb,
        timeout_sec=EXPORT_WORKER_TIMEOUT_SEC,
    )
    if not bool(result.get("ok")):
        raise RuntimeError(str(result.get("error") or "export worker failed"))
    scan_rows = result.get("scan_rows") or []
    if isinstance(scan_rows, list):
        _emit(progress_cb, {"type": "scan_results", "rows": scan_rows})
    worker_log_file = str(result.get("log_file") or "")
    if worker_log_file:
        _emit(
            progress_cb,
            {
                "type": "run_warning",
                "message": f"로그 파일 확인: {worker_log_file}",
            },
        )
    return {
        "scan_results": _deserialize_scan_results(result.get("scan_results") or {}),
        "scan_rows": scan_rows,
        "log_file": worker_log_file,
    }


@contextmanager
def _temporary_env(overrides: dict[str, str]) -> Any:
    old_values: dict[str, str | None] = {}
    try:
        for key, value in overrides.items():
            old_values[key] = os.environ.get(key)
            os.environ[key] = str(value)
        yield
    finally:
        for key, old_value in old_values.items():
            if old_value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old_value


def login_and_crawl_accounts(
    *,
    browser: str,
    headless: bool,
    target_map_path: str,
    logger,
    progress_cb: ProgressCallback | None = None,
) -> tuple[list[AdsAccount], str]:
    from playwright.sync_api import sync_playwright

    try:
        load_target_mapping_file(target_map_path, logger=logger)
    except TargetMappingError as exc:
        raise RuntimeError(str(exc)) from exc

    _ensure_playwright_event_loop_policy(logger=logger)

    if logger:
        logger.info("login_and_crawl_accounts start | browser=%s | headless=%s", browser, headless)

    _emit(
        progress_cb,
        {
            "type": "login_status",
            "status": "Opening Browser",
            "message": "Opening Google Ads browser window...",
        },
    )
    _emit(
        progress_cb,
        {
            "type": "login_status",
            "status": "Waiting Login",
            "message": "Waiting for Google Ads login confirmation.",
        },
    )

    try:
        with sync_playwright() as playwright:
            context, page, browser_used = launch_ads_context(
                playwright=playwright,
                headless=headless,
                browser_preference=browser,
                logger=logger,
            )
            try:
                window_policy = _default_window_policy()
                _focus_browser_page(page, logger=logger)
                _maximize_browser_page(page, logger=logger)
                page = ensure_logged_in(page, logger=logger)
                _emit(
                    progress_cb,
                    {
                        "type": "login_status",
                        "status": "Logged In",
                        "message": "Google Ads login confirmed.",
                    },
                )
                window_policy["keep_minimized"] = True
                _minimize_browser_page(page, logger=logger, window_policy=window_policy)
                accounts = collect_accounts(page, logger=logger)
                _emit(
                    progress_cb,
                    {
                        "type": "accounts_crawled",
                        "count": len(accounts),
                        "browser": browser_used,
                    },
                )
                return accounts, browser_used
            finally:
                context.close()
    except Exception as exc:  # noqa: BLE001
        message = _exc_text(exc)
        if logger:
            logger.exception("login_and_crawl_accounts failed: %s", message)
        raise RuntimeError(message) from exc


def scan_selected_accounts(
    *,
    selected_accounts: list[AdsAccount],
    browser: str,
    headless: bool,
    target_map_path: str,
    logger,
    progress_cb: ProgressCallback | None = None,
) -> dict[str, dict[str, Any]]:
    from playwright.sync_api import sync_playwright

    if not selected_accounts:
        return {}

    try:
        load_target_mapping_file(target_map_path, logger=logger)
    except TargetMappingError as exc:
        raise RuntimeError(str(exc)) from exc

    _ensure_playwright_event_loop_policy(logger=logger)

    results: dict[str, dict[str, Any]] = {}
    with sync_playwright() as playwright:
        context, page, _browser_used = launch_ads_context(
            playwright=playwright,
            headless=headless,
            browser_preference=browser,
            logger=logger,
        )
        try:
            window_policy = _default_window_policy()
            _focus_browser_page(page, logger=logger)
            _maximize_browser_page(page, logger=logger)
            page = ensure_logged_in(page, logger=logger)
            window_policy["keep_minimized"] = True
            _minimize_browser_page(page, logger=logger, window_policy=window_policy)
            discovered_now = collect_accounts(page, logger=logger)
            discovered_by_cid = {account.cid_digits: account for account in discovered_now}

            for selected in selected_accounts:
                account = discovered_by_cid.get(selected.cid_digits, selected)
                _minimize_browser_page(page, logger=logger, window_policy=window_policy)

                try:
                    items, matched_map_by_activity = scan_account_saved_reports(
                        page=page,
                        account=account,
                        logger=logger,
                    )
                    for activity_key in sorted(matched_map_by_activity.keys()):
                        matched_map = matched_map_by_activity.get(activity_key, {})
                        if not isinstance(matched_map, dict):
                            continue
                        activity_name = _resolve_activity_name(activity_key=activity_key, matched_map=matched_map)
                        for target_key in TARGET_ORDER:
                            row_item = matched_map.get(target_key)
                            _emit(
                                progress_cb,
                                {
                                    "type": "row_update",
                                    "row_id": _row_id(
                                        cid_digits=account.cid_digits,
                                        activity_key=activity_key,
                                        target_key=target_key,
                                    ),
                                    "account": account.name,
                                    "cid": account.cid,
                                    "activity": activity_name,
                                    "activity_key": activity_key,
                                    "target_key": target_key,
                                    "target_display": TARGET_DISPLAY_NAMES.get(target_key, target_key),
                                    "status": "Matched" if row_item else "Not Found",
                                    "message": row_item.visible_name if row_item else "report not found",
                                },
                            )
                    results[account.cid_digits] = {
                        "account": account,
                        "items": items,
                        "matched_map_by_activity": matched_map_by_activity,
                    }
                except Exception as exc:  # noqa: BLE001
                    error_text = _exc_text(exc)
                    _emit(
                        progress_cb,
                        {
                            "type": "account_stage",
                            "account": account.name,
                            "cid": account.cid,
                            "activity": "-",
                            "stage": "매칭",
                            "status": "Failed",
                            "message": error_text,
                        },
                    )
                    results[account.cid_digits] = {
                        "account": account,
                        "items": [],
                        "matched_map_by_activity": {},
                        "error": error_text,
                    }
        finally:
            context.close()

    return results


def run_google_export_for_accounts(
    *,
    selected_accounts: list[AdsAccount],
    scan_results: dict[str, dict[str, Any]],
    browser: str,
    headless: bool,
    target_map_path: str,
    final_output_dir: Path,
    downloads_dir: Path,
    logger,
    progress_cb: ProgressCallback | None = None,
    scan_before_export: bool = False,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    from playwright.sync_api import sync_playwright

    run_id = _now_run_id()
    log_path = getattr(logger.handlers[0], "baseFilename", "")
    _emit(
        progress_cb,
        {
            "type": "run_started",
            "run_id": run_id,
            "log_file": str(log_path or ""),
            "run_status": "Running",
        },
    )

    if not selected_accounts:
        _emit(
            progress_cb,
            {
                "type": "run_failed",
                "error": "No selected accounts for export.",
            },
        )
        return {}, []

    try:
        load_target_mapping_file(target_map_path, logger=logger)
    except TargetMappingError as exc:
        _emit(
            progress_cb,
            {
                "type": "run_failed",
                "error": str(exc),
            },
        )
        return {}, []

    _ensure_playwright_event_loop_policy(logger=logger)

    final_output_dir = Path(final_output_dir).expanduser().resolve()
    downloads_dir = Path(downloads_dir).expanduser().resolve()
    final_output_dir.mkdir(parents=True, exist_ok=True)
    downloads_dir.mkdir(parents=True, exist_ok=True)

    effective_scan_results: dict[str, dict[str, Any]] = dict(scan_results or {})
    scan_rows: list[dict[str, Any]] = []

    _emit(
        progress_cb,
        {
            "type": "login_status",
            "status": "Waiting Login",
            "message": "Waiting for Google Ads login confirmation.",
        },
    )

    with _temporary_env({"GOOGLE_ADS_OUTPUT_DIR": str(downloads_dir)}):
        with sync_playwright() as playwright:
            context, page, _browser_used = launch_ads_context(
                playwright=playwright,
                headless=headless,
                browser_preference=browser,
                logger=logger,
            )
            try:
                window_policy = _default_window_policy()
                window_policy["keep_minimized"] = True
                _minimize_browser_page(page, logger=logger, window_policy=window_policy)

                def _on_manual_login_required(login_page) -> None:
                    _focus_browser_page(login_page, logger=logger)
                    _maximize_browser_page(login_page, logger=logger)
                    window_policy["last_auto_state"] = "maximized"
                    window_policy["user_override"] = False

                page = ensure_logged_in(
                    page,
                    logger=logger,
                    on_manual_login_required=_on_manual_login_required,
                )
                _emit(
                    progress_cb,
                    {
                        "type": "login_status",
                        "status": "Logged In",
                        "message": "Google Ads login confirmed.",
                    },
                )
                _minimize_browser_page(page, logger=logger, window_policy=window_policy)
                discovered_now = collect_accounts(page, logger=logger)
                discovered_by_cid = {account.cid_digits: account for account in discovered_now}

                if scan_before_export:
                    effective_scan_results = {}
                    for selected in selected_accounts:
                        account = discovered_by_cid.get(selected.cid_digits, selected)
                        _minimize_browser_page(page, logger=logger, window_policy=window_policy)
                        try:
                            items, matched_map_by_activity = scan_account_saved_reports(
                                page=page,
                                account=account,
                                logger=logger,
                            )
                            for activity_key in sorted(matched_map_by_activity.keys()):
                                matched_map = matched_map_by_activity.get(activity_key, {})
                                if not isinstance(matched_map, dict):
                                    continue
                                activity_name = _resolve_activity_name(activity_key=activity_key, matched_map=matched_map)
                                for target_key in TARGET_ORDER:
                                    row_item = matched_map.get(target_key)
                                    _emit(
                                        progress_cb,
                                        {
                                            "type": "row_update",
                                            "row_id": _row_id(
                                                cid_digits=account.cid_digits,
                                                activity_key=activity_key,
                                                target_key=target_key,
                                            ),
                                            "account": account.name,
                                            "cid": account.cid,
                                            "activity": activity_name,
                                            "activity_key": activity_key,
                                            "target_key": target_key,
                                            "target_display": TARGET_DISPLAY_NAMES.get(target_key, target_key),
                                            "status": "Matched" if row_item else "Not Found",
                                            "message": row_item.visible_name if row_item else "report not found",
                                        },
                                    )
                            effective_scan_results[account.cid_digits] = {
                                "account": account,
                                "items": items,
                                "matched_map_by_activity": matched_map_by_activity,
                            }
                        except Exception as exc:  # noqa: BLE001
                            error_text = _exc_text(exc)
                            _emit(
                                progress_cb,
                                {
                                    "type": "account_stage",
                                    "account": account.name,
                                    "cid": account.cid,
                                    "activity": "-",
                                    "stage": "매칭",
                                    "status": "Failed",
                                    "message": error_text,
                                },
                            )
                            effective_scan_results[account.cid_digits] = {
                                "account": account,
                                "items": [],
                                "matched_map_by_activity": {},
                                "error": error_text,
                            }

                    scan_rows = _scan_results_as_rows(effective_scan_results)
                    _emit(progress_cb, {"type": "scan_results", "rows": scan_rows})
                    _emit(
                        progress_cb,
                        {
                            "type": "run_warning",
                            "message": "Matching completed. Export starting.",
                        },
                    )
                else:
                    scan_rows = _scan_results_as_rows(effective_scan_results)

                outputs_count = 0
                skipped_activities = 0
                total_targets = len(TARGET_ORDER)
                for selected in selected_accounts:
                    account = discovered_by_cid.get(selected.cid_digits, selected)
                    account_label = f"{account.name} | {account.cid}"
                    _minimize_browser_page(page, logger=logger, window_policy=window_policy)
                    matched_map_by_activity: dict[str, dict[str, SavedReportItem]] = (
                        effective_scan_results.get(selected.cid_digits, {}).get("matched_map_by_activity", {})
                    )
                    if not matched_map_by_activity:
                        matched_map_by_activity = effective_scan_results.get(account.cid_digits, {}).get(
                            "matched_map_by_activity",
                            {},
                        )

                    if not matched_map_by_activity:
                        _emit(
                            progress_cb,
                            {
                                "type": "account_stage",
                                "account": account.name,
                                "cid": account.cid,
                                "activity": "-",
                                "stage": "매칭",
                                "status": "Failed",
                                "message": "activity match not found",
                            },
                        )
                        continue

                    try:
                        ensure_account_report_editor_ready(page=page, account=account, logger=logger)
                        _minimize_browser_page(page, logger=logger, window_policy=window_policy)

                        for activity_key in sorted(matched_map_by_activity.keys()):
                            matched_map = matched_map_by_activity.get(activity_key, {})
                            if not isinstance(matched_map, dict):
                                continue
                            activity_name = _resolve_activity_name(activity_key=activity_key, matched_map=matched_map)
                            _emit(
                                progress_cb,
                                {
                                    "type": "account_stage",
                                    "account": account.name,
                                    "cid": account.cid,
                                    "activity": activity_name,
                                    "activity_key": activity_key,
                                    "stage": "다운로드",
                                    "status": "Exporting",
                                    "message": "다운로드 진행중",
                                },
                            )

                            def _download_progress(
                                progress_account: AdsAccount,
                                target_key: str,
                                status: str,
                                detail: str | None,
                                *,
                                _activity_name: str = activity_name,
                                _activity_key: str = activity_key,
                            ) -> None:
                                normalized = str(status or "").strip().replace("_", " ").title()
                                _emit(
                                    progress_cb,
                                    {
                                        "type": "row_update",
                                        "row_id": _row_id(
                                            cid_digits=progress_account.cid_digits,
                                            activity_key=_activity_key,
                                            target_key=target_key,
                                        ),
                                        "account": progress_account.name,
                                        "cid": progress_account.cid,
                                        "activity": _activity_name,
                                        "activity_key": _activity_key,
                                        "target_key": target_key,
                                        "target_display": TARGET_DISPLAY_NAMES.get(target_key, target_key),
                                        "status": normalized,
                                        "message": str(detail or ""),
                                    },
                                )

                            download_results = _download_targets_for_account(
                                page=page,
                                account=account,
                                matched_map=matched_map,
                                logger=logger,
                                progress_callback=_download_progress,
                                activity_name=activity_name,
                                activity_key=activity_key,
                            )
                            result_by_target: dict[str, DownloadResult] = {
                                result.target_key: result for result in download_results
                            }
                            failed_retry_targets = [
                                result.target_key
                                for result in download_results
                                if (not result.success) and (result.target_key in matched_map)
                            ]
                            successful_downloads = sum(
                                1 for result in result_by_target.values() if result.success and result.filename
                            )
                            if failed_retry_targets:
                                _minimize_browser_page(page, logger=logger, window_policy=window_policy)
                                _emit(
                                    progress_cb,
                                    {
                                        "type": "account_stage",
                                        "account": account.name,
                                        "cid": account.cid,
                                        "activity": activity_name,
                                        "activity_key": activity_key,
                                        "stage": "다운로드",
                                        "status": "Exporting",
                                        "message": (
                                            f"{successful_downloads}/{total_targets} 다운로드 완료, "
                                            f"실패 {len(failed_retry_targets)}개 재시도 중(1/1)"
                                        ),
                                    },
                                )
                                retry_results = _download_targets_for_account(
                                    page=page,
                                    account=account,
                                    matched_map=matched_map,
                                    logger=logger,
                                    progress_callback=_download_progress,
                                    target_keys=set(failed_retry_targets),
                                    activity_name=activity_name,
                                    activity_key=activity_key,
                                )
                                for retry_result in retry_results:
                                    result_by_target[retry_result.target_key] = retry_result

                            download_results = [
                                result_by_target.get(
                                    target_key,
                                    DownloadResult(
                                        target_key=target_key,
                                        success=False,
                                        activity_name=activity_name,
                                        activity_key=activity_key,
                                        reason="download result missing",
                                    ),
                                )
                                for target_key in TARGET_ORDER
                            ]
                            final_successful_downloads = sum(
                                1 for result in download_results if result.success and result.filename
                            )
                            final_failed_results = [result for result in download_results if not result.success]

                            _emit(
                                progress_cb,
                                {
                                    "type": "account_stage",
                                    "account": account.name,
                                    "cid": account.cid,
                                    "activity": activity_name,
                                    "activity_key": activity_key,
                                    "stage": "다운로드",
                                    "status": "Completed" if not final_failed_results else "Failed",
                                    "message": f"{final_successful_downloads}/{total_targets} 다운로드 완료",
                                },
                            )

                            if final_failed_results:
                                skipped_activities += 1
                                failed_display_names = ", ".join(
                                    TARGET_DISPLAY_NAMES.get(result.target_key, result.target_key)
                                    for result in final_failed_results
                                )
                                _emit(
                                    progress_cb,
                                    {
                                        "type": "account_stage",
                                        "account": account.name,
                                        "cid": account.cid,
                                        "activity": activity_name,
                                        "activity_key": activity_key,
                                        "stage": "통합본",
                                        "status": "Failed",
                                        "message": (
                                            f"통합본 생성 스킵 "
                                            f"({len(final_failed_results)}/{total_targets} 실패: {failed_display_names})"
                                        ),
                                    },
                                )
                                if logger:
                                    logger.warning(
                                        "skip unified workbook due to failed targets | account=%s(%s) | activity=%s | failed=%s",
                                        account.name,
                                        account.cid,
                                        activity_name,
                                        [result.target_key for result in final_failed_results],
                                    )
                                continue

                            _emit(
                                progress_cb,
                                {
                                    "type": "account_stage",
                                    "account": account.name,
                                    "cid": account.cid,
                                    "activity": activity_name,
                                    "activity_key": activity_key,
                                    "stage": "통합본",
                                    "status": "Exporting",
                                    "message": "통합본 생성중",
                                },
                            )
                            if logger:
                                logger.info(
                                    "unified workbook build start | account=%s(%s) | activity=%s | files=%s",
                                    account.name,
                                    account.cid,
                                    activity_name,
                                    [result.filename for result in download_results if result.filename],
                                )
                            output_path, summaries = create_unified_workbook_for_account(
                                account=account,
                                download_results=download_results,
                                activity_name=activity_name,
                                output_dir=final_output_dir,
                                csv_dir=downloads_dir,
                                logger=logger,
                            )
                            _emit(
                                progress_cb,
                                {
                                    "type": "account_stage",
                                    "account": account.name,
                                    "cid": account.cid,
                                    "activity": activity_name,
                                    "activity_key": activity_key,
                                    "stage": "통합본",
                                    "status": "Completed",
                                    "message": f"통합본 생성완료:{output_path.name}",
                                },
                            )
                            if logger:
                                logger.info(
                                    "unified workbook written | account=%s(%s) | activity=%s | path=%s",
                                    account.name,
                                    account.cid,
                                    activity_name,
                                    output_path,
                                )
                            outputs_count += 1
                            summary_rows = summaries_as_rows(
                                summaries,
                                account_label,
                                activity_name=activity_name,
                            )
                            _emit(
                                progress_cb,
                                {
                                    "type": "account_result",
                                    "account": account.name,
                                    "cid": account.cid,
                                    "activity": activity_name,
                                    "activity_key": activity_key,
                                    "workbook_path": str(output_path),
                                    "summaries": summary_rows,
                                },
                            )
                            for summary in summaries:
                                _emit(
                                    progress_cb,
                                    {
                                        "type": "row_update",
                                        "row_id": _row_id(
                                            cid_digits=account.cid_digits,
                                            activity_key=activity_key,
                                            target_key=summary.target_key,
                                        ),
                                        "account": account.name,
                                        "cid": account.cid,
                                        "activity": activity_name,
                                        "activity_key": activity_key,
                                        "target_key": summary.target_key,
                                        "target_display": TARGET_DISPLAY_NAMES.get(summary.target_key, summary.target_key),
                                        "status": "Completed" if summary.status == "excel_written" else "Failed",
                                        "message": (
                                            f"{summary.written_rows}/{summary.csv_rows} rows"
                                            if summary.status == "excel_written"
                                            else (summary.reason or "failed")
                                        ),
                                    },
                                )
                    except Exception as exc:  # noqa: BLE001
                        error_text = _exc_text(exc)
                        if logger:
                            logger.exception(
                                "account export failed | account=%s(%s) | reason=%s",
                                account.name,
                                account.cid,
                                error_text,
                            )
                        _emit(
                            progress_cb,
                            {
                                "type": "account_stage",
                                "account": account.name,
                                "cid": account.cid,
                                "activity": "-",
                                "stage": "통합본",
                                "status": "Failed",
                                "message": error_text,
                            },
                        )
                        for activity_key, matched_map in matched_map_by_activity.items():
                            activity_name = _resolve_activity_name(activity_key=activity_key, matched_map=matched_map)
                            for target_key in TARGET_ORDER:
                                _emit(
                                    progress_cb,
                                    {
                                        "type": "row_update",
                                        "row_id": _row_id(
                                            cid_digits=account.cid_digits,
                                            activity_key=activity_key,
                                            target_key=target_key,
                                        ),
                                        "account": account.name,
                                        "cid": account.cid,
                                        "activity": activity_name,
                                        "activity_key": activity_key,
                                        "target_key": target_key,
                                        "target_display": TARGET_DISPLAY_NAMES.get(target_key, target_key),
                                        "status": "Failed",
                                        "message": error_text,
                                    },
                                )

                _emit(
                    progress_cb,
                    {
                        "type": "run_completed",
                        "run_status": "Completed (With Failures)" if skipped_activities else "Completed",
                        "message": (
                            f"Export completed. Workbook count={outputs_count}"
                            + (f", skipped={skipped_activities}" if skipped_activities else "")
                        ),
                    },
                )
                if logger:
                    logger.info(
                        "export completed | workbook_count=%s | skipped_activities=%s",
                        outputs_count,
                        skipped_activities,
                    )
            finally:
                context.close()
    return effective_scan_results, scan_rows

