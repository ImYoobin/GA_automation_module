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
from .action_log_downloader import build_action_log_run_dir, download_action_logs_for_account
from .auth import (
    ensure_logged_in,
    get_browser_window_state,
    launch_ads_context,
    maximize_browser_window,
    minimize_browser_window,
)
from .google_excel_builder import POLICY_BY_TARGET, create_unified_workbook_for_account, summaries_as_rows
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
LOGIN_WORKER_RESULT_GRACE_SEC = 3.0


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
    result_published = False

    def _publish_success(accounts: list[AdsAccount], browser_used: str) -> None:
        nonlocal result_published
        if result_published:
            return
        result_queue.put(
            {
                "ok": True,
                "accounts": [_account_to_dict(account) for account in accounts],
                "browser_used": browser_used,
                "log_file": str(log_path),
            }
        )
        result_published = True

    try:
        accounts, browser_used = login_and_crawl_accounts(
            browser=browser,
            headless=headless,
            target_map_path=target_map_path,
            logger=logger,
            progress_cb=event_queue.put,
            on_accounts_crawled=_publish_success,
        )
        _publish_success(accounts, browser_used)
    except Exception as exc:  # noqa: BLE001
        if result_published:
            if logger:
                logger.warning(
                    "login worker cleanup failed after publishing accounts | reason=%s",
                    _exc_text(exc),
                )
        else:
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
    action_log_dir: str,
    enable_report_download: bool,
    enable_action_log_download: bool,
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
            action_log_dir=Path(action_log_dir).expanduser().resolve(),
            enable_report_download=enable_report_download,
            enable_action_log_download=enable_action_log_download,
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
    return_on_result: bool = False,
    post_result_grace_sec: float = 0.0,
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

        def _try_get_result() -> dict[str, Any] | None:
            try:
                result = result_queue.get_nowait()
            except queue.Empty:
                return None
            if not isinstance(result, dict):
                raise RuntimeError("Worker returned invalid result payload.")
            return result

        try:
            while process.is_alive():
                _drain_events()
                if return_on_result:
                    result = _try_get_result()
                    if result is not None:
                        grace_deadline = time.monotonic() + max(0.0, float(post_result_grace_sec))
                        while process.is_alive() and time.monotonic() < grace_deadline:
                            _drain_events()
                            time.sleep(WORKER_POLL_INTERVAL_SEC)
                        return result
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

            result = _try_get_result()
            if result is None:
                raise RuntimeError("Worker exited without result payload.")
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
        return_on_result=True,
        post_result_grace_sec=LOGIN_WORKER_RESULT_GRACE_SEC,
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
    action_log_dir: Path,
    enable_report_download: bool,
    enable_action_log_download: bool,
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
            "action_log_dir": str(Path(action_log_dir).expanduser().resolve()),
            "enable_report_download": enable_report_download,
            "enable_action_log_download": enable_action_log_download,
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
    on_accounts_crawled: Callable[[list[AdsAccount], str], None] | None = None,
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
                        "status": "Crawling Accounts",
                        "message": "계정 크롤링중입니다.",
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
                if on_accounts_crawled is not None:
                    on_accounts_crawled(accounts, browser_used)
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


def _matched_activity_entries(
    matched_map_by_activity: dict[str, dict[str, SavedReportItem]],
) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    for activity_key in sorted(matched_map_by_activity.keys()):
        matched_map = matched_map_by_activity.get(activity_key, {})
        if not isinstance(matched_map, dict) or not matched_map:
            continue
        entries.append((activity_key, _resolve_activity_name(activity_key=activity_key, matched_map=matched_map)))
    return entries


def _emit_action_log_update(
    progress_cb: ProgressCallback | None,
    *,
    account: AdsAccount,
    activity_name: str,
    activity_key: str,
    status: str,
    message: str,
) -> None:
    _emit(
        progress_cb,
        {
            "type": "action_log_update",
            "account": account.name,
            "cid": account.cid,
            "activity": activity_name,
            "activity_key": activity_key,
            "status": status,
            "message": message,
        },
    )


def _emit_action_log_waiting_rows(
    progress_cb: ProgressCallback | None,
    *,
    account: AdsAccount,
    activity_entries: list[tuple[str, str]],
) -> None:
    for activity_key, activity_name in activity_entries:
        _emit_action_log_update(
            progress_cb,
            account=account,
            activity_name=activity_name,
            activity_key=activity_key,
            status="Waiting",
            message="\uc561\uc158\ub85c\uadf8 \ub300\uae30\uc911",
        )


def _matching_completed_message(*, enable_report_download: bool, enable_action_log_download: bool) -> str:
    if enable_report_download and enable_action_log_download:
        return "Matching completed. Report/action log execution starting."
    if enable_report_download:
        return "Matching completed. Report export starting."
    return "Matching completed. Action log collection starting."


def _build_run_completed_message(
    *,
    enable_report_download: bool,
    enable_action_log_download: bool,
    workbook_count: int,
    action_log_count: int,
    skipped_activities: int,
) -> str:
    if enable_report_download and enable_action_log_download:
        message = f"Execution completed. Workbook count={workbook_count}, action log count={action_log_count}"
        if skipped_activities:
            message = f"{message}, skipped={skipped_activities}"
        return message
    if enable_report_download:
        message = f"Export completed. Workbook count={workbook_count}"
        if skipped_activities:
            message = f"{message}, skipped={skipped_activities}"
        return message
    return f"Action log completed. File count={action_log_count}"


def _run_report_phase_for_account(
    *,
    page,
    account: AdsAccount,
    matched_map_by_activity: dict[str, dict[str, SavedReportItem]],
    final_output_dir: Path,
    downloads_dir: Path,
    logger,
    progress_cb: ProgressCallback | None,
    window_policy: dict[str, Any],
) -> tuple[int, int, bool]:
    outputs_count = 0
    skipped_activities = 0
    had_failures = False
    total_targets = len(TARGET_ORDER)
    account_label = f"{account.name} | {account.cid}"

    try:
        ensure_account_report_editor_ready(page=page, account=account, logger=logger)
        _minimize_browser_page(page, logger=logger, window_policy=window_policy)
    except Exception as exc:  # noqa: BLE001
        error_text = _exc_text(exc)
        had_failures = True
        if logger:
            logger.exception(
                "account report phase bootstrap failed | account=%s(%s) | reason=%s",
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
                "stage": "\ud1b5\ud569\ubcf8",
                "status": "Failed",
                "message": error_text,
            },
        )
        for activity_key, activity_name in _matched_activity_entries(matched_map_by_activity):
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
        return outputs_count, skipped_activities, had_failures

    for activity_key, activity_name in _matched_activity_entries(matched_map_by_activity):
        matched_map = matched_map_by_activity.get(activity_key, {})
        if not isinstance(matched_map, dict):
            continue

        processed_sheet_keys: set[str] = set()
        active_sheet_key = ""
        processed_sheet_count = 0

        try:
            _emit(
                progress_cb,
                {
                    "type": "account_stage",
                    "account": account.name,
                    "cid": account.cid,
                    "activity": activity_name,
                    "activity_key": activity_key,
                    "stage": "\ub2e4\uc6b4\ub85c\ub4dc",
                    "status": "Exporting",
                    "message": "\ub2e4\uc6b4\ub85c\ub4dc \uc9c4\ud589\uc911",
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
                        "stage": "\ub2e4\uc6b4\ub85c\ub4dc",
                        "status": "Exporting",
                        "message": (
                            f"{successful_downloads}/{total_targets} \ub2e4\uc6b4\ub85c\ub4dc \uc644\ub8cc, "
                            f"\uc2e4\ud328 {len(failed_retry_targets)}\uac1c \uc7ac\uc2dc\ub3c4 \uc911(1/1)"
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
                    "stage": "\ub2e4\uc6b4\ub85c\ub4dc",
                    "status": "Completed" if not final_failed_results else "Failed",
                    "message": f"{final_successful_downloads}/{total_targets} \ub2e4\uc6b4\ub85c\ub4dc \uc644\ub8cc",
                },
            )

            if final_failed_results:
                skipped_activities += 1
                had_failures = True
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
                        "stage": "\ud1b5\ud569\ubcf8",
                        "status": "Failed",
                        "message": (
                            f"\ud1b5\ud569\ubcf8 \uc0dd\uc131 \uc2a4\ud0b5 "
                            f"({len(final_failed_results)}/{total_targets} \uc2e4\ud328: {failed_display_names})"
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
                    "stage": "\ud1b5\ud569\ubcf8",
                    "status": "Exporting",
                    "message": "\ud1b5\ud569\ubcf8 \uc0dd\uc131\uc911",
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

            def _emit_workbook_sheet_progress(
                target_key: str,
                stage: str,
                summary,
                *,
                _account: AdsAccount = account,
                _activity_name: str = activity_name,
                _activity_key: str = activity_key,
            ) -> None:
                nonlocal active_sheet_key, processed_sheet_count
                policy = POLICY_BY_TARGET.get(target_key)
                sheet_name = ""
                if summary is not None:
                    sheet_name = str(getattr(summary, "sheet_name", "") or "").strip()
                if not sheet_name and policy is not None:
                    sheet_name = str(policy.sheet_name or "").strip()
                if not sheet_name:
                    sheet_name = target_key

                row_id = _row_id(
                    cid_digits=_account.cid_digits,
                    activity_key=_activity_key,
                    target_key=target_key,
                )
                target_display = TARGET_DISPLAY_NAMES.get(target_key, target_key)

                if stage == "start":
                    active_sheet_key = target_key
                    _emit(
                        progress_cb,
                        {
                            "type": "row_update",
                            "row_id": row_id,
                            "account": _account.name,
                            "cid": _account.cid,
                            "activity": _activity_name,
                            "activity_key": _activity_key,
                            "target_key": target_key,
                            "target_display": target_display,
                            "sheet_name": sheet_name,
                            "status": "Exporting",
                            "message": "시트 처리중",
                            "row_count_text": "",
                            "missing_columns_text": "",
                            "has_warning": False,
                        },
                    )
                    _emit(
                        progress_cb,
                        {
                            "type": "account_stage",
                            "account": _account.name,
                            "cid": _account.cid,
                            "activity": _activity_name,
                            "activity_key": _activity_key,
                            "stage": "\ud1b5\ud569\ubcf8",
                            "status": "Exporting",
                            "message": (
                                f"\ud1b5\ud569\ubcf8 \uc0dd\uc131\uc911 "
                                f"(\ud604\uc7ac \uc2dc\ud2b8: {sheet_name}, {processed_sheet_count}/{total_targets})"
                            ),
                        },
                    )
                    return

                if summary is None:
                    return

                active_sheet_key = ""
                processed_sheet_keys.add(target_key)
                processed_sheet_count += 1
                missing_columns = tuple(getattr(summary, "missing_columns", ()) or ())
                missing_columns_text = (
                    ", ".join(str(item).strip() for item in missing_columns if str(item).strip())
                    if getattr(summary, "csv_path", None)
                    else ""
                )
                row_count_text = ""
                message = str(getattr(summary, "reason", "") or "failed").strip() or "failed"
                status = "Failed"
                if getattr(summary, "status", "") == "excel_written":
                    status = "Completed"
                    row_count_text = (
                        f"\ucc98\ub9ac \ud589\uc218 "
                        f"{int(getattr(summary, 'written_rows', 0) or 0)}/"
                        f"{int(getattr(summary, 'csv_rows', 0) or 0)}"
                    )
                    message = row_count_text

                _emit(
                    progress_cb,
                    {
                        "type": "row_update",
                        "row_id": row_id,
                        "account": _account.name,
                        "cid": _account.cid,
                        "activity": _activity_name,
                        "activity_key": _activity_key,
                        "target_key": target_key,
                        "target_display": target_display,
                        "sheet_name": sheet_name,
                        "status": status,
                        "message": message,
                        "row_count_text": row_count_text,
                        "missing_columns_text": missing_columns_text,
                        "has_warning": bool(missing_columns_text),
                    },
                )
                _emit(
                    progress_cb,
                    {
                        "type": "account_stage",
                        "account": _account.name,
                        "cid": _account.cid,
                        "activity": _activity_name,
                        "activity_key": _activity_key,
                        "stage": "\ud1b5\ud569\ubcf8",
                        "status": "Exporting",
                        "message": (
                            f"\ud1b5\ud569\ubcf8 \uc0dd\uc131\uc911 "
                            f"({processed_sheet_count}/{total_targets} \uc2dc\ud2b8 \ucc98\ub9ac \uc644\ub8cc, "
                            f"\ub9c8\uc9c0\ub9c9 \uc2dc\ud2b8: {sheet_name})"
                        ),
                    },
                )

            output_path, summaries = create_unified_workbook_for_account(
                account=account,
                download_results=download_results,
                activity_name=activity_name,
                output_dir=final_output_dir,
                csv_dir=downloads_dir,
                logger=logger,
                progress_callback=_emit_workbook_sheet_progress,
            )
            _emit(
                progress_cb,
                {
                    "type": "account_stage",
                    "account": account.name,
                    "cid": account.cid,
                    "activity": activity_name,
                    "activity_key": activity_key,
                    "stage": "\ud1b5\ud569\ubcf8",
                    "status": "Completed",
                    "message": f"\ud1b5\ud569\ubcf8 \uc0dd\uc131\uc644\ub8cc:{output_path.name}",
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
        except Exception as exc:  # noqa: BLE001
            had_failures = True
            error_text = _exc_text(exc)
            if logger:
                logger.exception(
                    "activity report phase failed | account=%s(%s) | activity=%s | reason=%s",
                    account.name,
                    account.cid,
                    activity_name,
                    error_text,
                )
            _emit(
                progress_cb,
                {
                    "type": "account_stage",
                    "account": account.name,
                    "cid": account.cid,
                    "activity": activity_name,
                    "activity_key": activity_key,
                    "stage": "\ud1b5\ud569\ubcf8",
                    "status": "Failed",
                    "message": error_text,
                },
            )
            for target_key in TARGET_ORDER:
                if target_key in processed_sheet_keys:
                    continue
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
                        "sheet_name": (
                            POLICY_BY_TARGET.get(target_key).sheet_name
                            if POLICY_BY_TARGET.get(target_key) is not None
                            else ""
                        ),
                        "status": "Failed",
                        "message": error_text,
                        "row_count_text": "",
                        "missing_columns_text": "",
                        "has_warning": False,
                    },
                )

    return outputs_count, skipped_activities, had_failures


def _run_action_log_phase_for_account(
    *,
    page,
    account: AdsAccount,
    activity_entries: list[tuple[str, str]],
    action_log_dir: Path,
    logger,
    progress_cb: ProgressCallback | None,
) -> tuple[int, bool]:
    if not activity_entries:
        return 0, False

    had_failures = False
    normalized_dir = Path(action_log_dir).expanduser().resolve()

    def _action_log_progress(
        progress_account: AdsAccount,
        activity_name: str,
        activity_key: str,
        status: str,
        message: str,
    ) -> None:
        _emit_action_log_update(
            progress_cb,
            account=progress_account,
            activity_name=activity_name,
            activity_key=activity_key,
            status=status,
            message=message,
        )

    try:
        results = download_action_logs_for_account(
            page=page,
            account=account,
            activities=activity_entries,
            action_log_dir=normalized_dir,
            logger=logger,
            progress_callback=_action_log_progress,
        )
    except Exception as exc:  # noqa: BLE001
        error_text = _exc_text(exc)
        had_failures = True
        for activity_key, activity_name in activity_entries:
            _emit_action_log_update(
                progress_cb,
                account=account,
                activity_name=activity_name,
                activity_key=activity_key,
                status="Failed",
                message=error_text,
            )
        return 0, had_failures

    saved_count = 0
    for result in results:
        if result.success and result.filename:
            saved_count += 1
            _emit(
                progress_cb,
                {
                    "type": "action_log_result",
                    "account": account.name,
                    "cid": account.cid,
                    "activity": result.activity_name,
                    "activity_key": result.activity_key,
                    "file_path": str(normalized_dir / result.filename),
                },
            )
            continue
        had_failures = True
    return saved_count, had_failures


def run_google_export_for_accounts(
    *,
    selected_accounts: list[AdsAccount],
    scan_results: dict[str, dict[str, Any]],
    browser: str,
    headless: bool,
    target_map_path: str,
    final_output_dir: Path,
    downloads_dir: Path,
    action_log_dir: Path,
    enable_report_download: bool,
    enable_action_log_download: bool,
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

    if not enable_report_download and not enable_action_log_download:
        _emit(
            progress_cb,
            {
                "type": "run_failed",
                "error": "At least one execution mode must be enabled.",
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
    action_log_dir = Path(action_log_dir).expanduser().resolve()
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
                            "message": _matching_completed_message(
                                enable_report_download=enable_report_download,
                                enable_action_log_download=enable_action_log_download,
                            ),
                        },
                    )
                else:
                    scan_rows = _scan_results_as_rows(effective_scan_results)

                outputs_count = 0
                action_log_count = 0
                skipped_activities = 0
                had_failures = False

                if enable_action_log_download:
                    for selected in selected_accounts:
                        account = discovered_by_cid.get(selected.cid_digits, selected)
                        matched_map_by_activity: dict[str, dict[str, SavedReportItem]] = (
                            effective_scan_results.get(selected.cid_digits, {}).get("matched_map_by_activity", {})
                        )
                        if not matched_map_by_activity:
                            matched_map_by_activity = effective_scan_results.get(account.cid_digits, {}).get(
                                "matched_map_by_activity",
                                {},
                            )
                        _emit_action_log_waiting_rows(
                            progress_cb,
                            account=account,
                            activity_entries=_matched_activity_entries(matched_map_by_activity),
                        )

                for selected in selected_accounts:
                    account = discovered_by_cid.get(selected.cid_digits, selected)
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
                        had_failures = True
                        continue

                    activity_entries = _matched_activity_entries(matched_map_by_activity)
                    if enable_report_download:
                        account_outputs, account_skipped, report_failed = _run_report_phase_for_account(
                            page=page,
                            account=account,
                            matched_map_by_activity=matched_map_by_activity,
                            final_output_dir=final_output_dir,
                            downloads_dir=downloads_dir,
                            logger=logger,
                            progress_cb=progress_cb,
                            window_policy=window_policy,
                        )
                        outputs_count += account_outputs
                        skipped_activities += account_skipped
                        had_failures = had_failures or report_failed

                    if enable_action_log_download:
                        saved_count, action_log_failed = _run_action_log_phase_for_account(
                            page=page,
                            account=account,
                            activity_entries=activity_entries,
                            action_log_dir=action_log_dir,
                            logger=logger,
                            progress_cb=progress_cb,
                        )
                        action_log_count += saved_count
                        had_failures = had_failures or action_log_failed

                _emit(
                    progress_cb,
                    {
                        "type": "run_completed",
                        "run_status": "Completed (With Failures)" if had_failures else "Completed",
                        "message": _build_run_completed_message(
                            enable_report_download=enable_report_download,
                            enable_action_log_download=enable_action_log_download,
                            workbook_count=outputs_count,
                            action_log_count=action_log_count,
                            skipped_activities=skipped_activities,
                        ),
                    },
                )
                if logger:
                    logger.info(
                        "export completed | workbook_count=%s | action_log_count=%s | skipped_activities=%s | had_failures=%s",
                        outputs_count,
                        action_log_count,
                        skipped_activities,
                        had_failures,
                    )
            finally:
                context.close()
    return effective_scan_results, scan_rows

