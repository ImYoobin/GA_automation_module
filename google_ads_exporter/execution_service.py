"""Background execution worker and thread-safe progress store for Google exporter."""

from __future__ import annotations

import datetime as dt
import queue
import threading
from dataclasses import dataclass
from typing import Any, Callable

from .models import AdsAccount
from .targets import TARGET_DISPLAY_NAMES, TARGET_ORDER


ProgressCallback = Callable[[dict[str, Any]], None]
ExportRunner = Callable[..., None]


def _now_text() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _row_id(*, cid_digits: str, target_key: str) -> str:
    return f"{str(cid_digits or '').strip()}::{str(target_key or '').strip()}"


@dataclass(frozen=True, slots=True)
class LogRow:
    row_id: str
    account: str
    target_key: str
    target_display: str
    status: str
    message: str
    last_updated: str


@dataclass(frozen=True, slots=True)
class AccountStageRow:
    account: str
    cid: str
    stage: str
    status: str
    message: str
    updated_at: str


class ExecutionStateStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: queue.Queue[dict[str, Any]] = queue.Queue()
        self._rows: dict[str, LogRow] = {}
        self._row_order: list[str] = []
        self._account_stage_map: dict[str, AccountStageRow] = {}
        self._messages: list[dict[str, str]] = []
        self._outputs: list[dict[str, str]] = []
        self._summaries: list[dict[str, Any]] = []
        self._scan_result_rows: list[dict[str, Any]] = []
        self._thread: threading.Thread | None = None
        self._running = False
        self.run_status = "Idle"
        self.login_status = "Not Started"
        self.run_id = ""
        self.log_file = ""
        self.last_error = ""

    def push_event(self, event: dict[str, Any]) -> None:
        self._events.put(dict(event))

    def is_running(self) -> bool:
        with self._lock:
            alive = bool(self._thread and self._thread.is_alive())
            return bool(self._running and alive)

    def initialize_rows(self, accounts: list[AdsAccount]) -> None:
        with self._lock:
            self._rows = {}
            self._row_order = []
            self._account_stage_map = {}
            self._outputs = []
            self._summaries = []
            self._scan_result_rows = []
            self.last_error = ""
            for account in accounts:
                account_label = f"{account.name} | {account.cid}"
                for target_key in TARGET_ORDER:
                    row = LogRow(
                        row_id=_row_id(cid_digits=account.cid_digits, target_key=target_key),
                        account=account_label,
                        target_key=target_key,
                        target_display=TARGET_DISPLAY_NAMES.get(target_key, target_key),
                        status="Pending",
                        message="waiting",
                        last_updated=_now_text(),
                    )
                    self._rows[row.row_id] = row
                    self._row_order.append(row.row_id)

    def start_thread(self, thread: threading.Thread) -> None:
        with self._lock:
            self._thread = thread
            self._running = True

    def mark_finished(self) -> None:
        with self._lock:
            self._running = False
            self._thread = None

    def _update_row(
        self,
        *,
        row_id: str,
        status: str,
        message: str,
    ) -> None:
        existing = self._rows.get(row_id)
        if not existing:
            return
        self._rows[row_id] = LogRow(
            row_id=existing.row_id,
            account=existing.account,
            target_key=existing.target_key,
            target_display=existing.target_display,
            status=status,
            message=message,
            last_updated=_now_text(),
        )

    def drain_events(self) -> None:
        while True:
            try:
                event = self._events.get_nowait()
            except queue.Empty:
                break

            event_type = str(event.get("type") or "").strip()
            with self._lock:
                if event_type == "run_started":
                    self.run_id = str(event.get("run_id") or self.run_id)
                    self.log_file = str(event.get("log_file") or self.log_file)
                    self.run_status = str(event.get("run_status") or "Running")
                elif event_type == "login_status":
                    self.login_status = str(event.get("status") or self.login_status)
                    message = str(event.get("message") or "").strip()
                    if message:
                        self._messages.append(
                            {
                                "level": "info",
                                "text": message,
                                "time": _now_text(),
                            }
                        )
                elif event_type == "accounts_crawled":
                    count = int(event.get("count") or 0)
                    browser = str(event.get("browser") or "")
                    text = f"Accounts crawled: {count}"
                    if browser:
                        text = f"{text} (browser={browser})"
                    self._messages.append(
                        {
                            "level": "success",
                            "text": text,
                            "time": _now_text(),
                        }
                    )
                elif event_type == "row_update":
                    self._update_row(
                        row_id=str(event.get("row_id") or ""),
                        status=str(event.get("status") or "Running"),
                        message=str(event.get("message") or ""),
                    )
                elif event_type == "account_result":
                    account = str(event.get("account") or "")
                    cid = str(event.get("cid") or "")
                    workbook_path = str(event.get("workbook_path") or "")
                    if workbook_path:
                        self._outputs.append(
                            {
                                "account": account,
                                "cid": cid,
                                "workbook_path": workbook_path,
                            }
                        )
                    summaries = event.get("summaries") or []
                    if isinstance(summaries, list):
                        for item in summaries:
                            if not isinstance(item, dict):
                                continue
                            row = dict(item)
                            row.setdefault("account", account)
                            row.setdefault("cid", cid)
                            self._summaries.append(row)
                elif event_type == "scan_results":
                    rows = event.get("rows")
                    if isinstance(rows, list):
                        self._scan_result_rows = [item for item in rows if isinstance(item, dict)]
                elif event_type == "account_stage":
                    account = str(event.get("account") or "")
                    cid = str(event.get("cid") or "")
                    key = f"{account}|{cid}"
                    self._account_stage_map[key] = AccountStageRow(
                        account=account,
                        cid=cid,
                        stage=str(event.get("stage") or ""),
                        status=str(event.get("status") or "Waiting"),
                        message=str(event.get("message") or ""),
                        updated_at=_now_text(),
                    )
                elif event_type == "run_warning":
                    self._messages.append(
                        {
                            "level": "warning",
                            "text": str(event.get("message") or ""),
                            "time": _now_text(),
                        }
                    )
                elif event_type == "run_completed":
                    self.run_status = str(event.get("run_status") or "Completed")
                    message = str(event.get("message") or "").strip()
                    if message:
                        self._messages.append(
                            {
                                "level": "success",
                                "text": message,
                                "time": _now_text(),
                            }
                        )
                    self._running = False
                elif event_type == "run_failed":
                    self.run_status = "Failed"
                    self.login_status = "Error"
                    self.last_error = str(event.get("error") or "Unknown error")
                    self._messages.append(
                        {
                            "level": "error",
                            "text": self.last_error,
                            "time": _now_text(),
                        }
                    )
                    self._running = False
                elif event_type == "worker_heartbeat":
                    # Worker heartbeat is used only for liveness checks in parent thread.
                    continue

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            rows = [self._rows[row_id] for row_id in self._row_order if row_id in self._rows]
            return {
                "run_status": self.run_status,
                "login_status": self.login_status,
                "run_id": self.run_id,
                "log_file": self.log_file,
                "last_error": self.last_error,
                "rows": rows,
                "messages": list(self._messages[-14:]),
                "outputs": list(self._outputs),
                "summaries": list(self._summaries),
                "scan_result_rows": list(self._scan_result_rows),
                "account_stage_rows": list(self._account_stage_map.values()),
                "is_running": self._running,
            }


def _worker(
    *,
    store: ExecutionStateStore,
    runner: ExportRunner,
    runner_kwargs: dict[str, Any],
) -> None:
    try:
        runner(progress_cb=store.push_event, **runner_kwargs)
    except Exception as exc:  # noqa: BLE001
        store.push_event(
            {
                "type": "run_failed",
                "error": f"Execution failed: {exc}",
            }
        )
    finally:
        store.mark_finished()


def start_export_execution(
    *,
    store: ExecutionStateStore,
    runner: ExportRunner,
    runner_kwargs: dict[str, Any],
) -> tuple[bool, str]:
    if store.is_running():
        return False, "Another run is already in progress."

    thread = threading.Thread(
        target=_worker,
        kwargs={
            "store": store,
            "runner": runner,
            "runner_kwargs": runner_kwargs,
        },
        daemon=True,
    )
    store.start_thread(thread)
    thread.start()
    return True, "Export started."


def create_execution_store() -> ExecutionStateStore:
    return ExecutionStateStore()
