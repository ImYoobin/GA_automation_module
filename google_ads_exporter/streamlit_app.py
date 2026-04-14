"""Streamlit UI for Google Ads exporter."""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path, PureWindowsPath
from typing import Any

import pandas as pd
import streamlit as st

from google_ads_exporter.env import load_env_file
from google_ads_exporter.execution_service import (
    AccountStageRow,
    LogRow,
    create_execution_store,
    start_export_execution,
)
from google_ads_exporter.google_adapter import (
    login_and_crawl_accounts_in_subprocess,
    run_scan_and_export_in_subprocess,
)
from google_ads_exporter.google_excel_builder import open_file_in_explorer
from google_ads_exporter.main import _apply_runtime_directory_overrides, _default_target_map_path
from google_ads_exporter.models import AdsAccount
from google_ads_exporter.utils import setup_logger

STATUS_LABEL = {
    "waiting": "Waiting",
    "exporting": "Exporting",
    "completed": "Completed",
    "failed": "Failed",
}

STATUS_STYLE = {
    "waiting": "background-color: #f1f5f9; color: #64748b; font-weight: 600;",
    "exporting": "background-color: #dbeafe; color: #1d4ed8; font-weight: 700;",
    "completed": "background-color: #dff3e6; color: #166534; font-weight: 700;",
    "failed": "background-color: #fee2e2; color: #b91c1c; font-weight: 700;",
}

RUNTIME_SETTINGS_RELATIVE_PATH = Path("config") / "runtime_settings.json"
RUNTIME_PATH_KEYS: tuple[str, ...] = ("output_dir", "downloads_dir", "logs_dir")
RUNTIME_INPUT_KEY_BY_PATH_KEY: dict[str, str] = {
    "output_dir": "output_dir_input",
    "downloads_dir": "downloads_dir_input",
    "logs_dir": "logs_dir_input",
}
DEFAULT_USER_BASE_DIR = Path.home() / "GoogleAdsExport"
DEFAULT_USER_OUTPUT_DIR = DEFAULT_USER_BASE_DIR / "output"
DEFAULT_USER_DOWNLOADS_DIR = DEFAULT_USER_BASE_DIR / "downloads"
DEFAULT_USER_LOGS_DIR = DEFAULT_USER_BASE_DIR / "logs"
INVALID_RUNTIME_PATH_MESSAGE = "올바르지 않은 경로입니다. 로컬 PC 경로를 입력해주세요."
_WINDOWS_ABS_DRIVE_RE = re.compile(r"^[A-Za-z]:\\")
_WINDOWS_DRIVE_TOKEN_RE = re.compile(r"[A-Za-z]:\\")


def run_streamlit_app() -> None:
    st.set_page_config(page_title="Google Ads Exporter", layout="wide")
    _init_session_state()
    _inject_ui_css()

    store = st.session_state["execution_store"]
    store.drain_events()
    snapshot = store.snapshot()

    _validate_output_count(snapshot)
    store.drain_events()
    snapshot = store.snapshot()

    _open_output_folder_for_completed_run(snapshot)

    st.title("Google Ads Auto Export")
    st.markdown("📋 상단에서 Export할 Account를 선택합니다. Report Editor에 Report를 사전 세팅해주세요.")
    st.markdown("📊 하단에서 매칭 결과, 실행 로그, 처리 완료 요약을 확인합니다.")
    st.markdown("⚙️ 좌측 사이드바에서 Run Settings를 설정합니다.")

    _render_sidebar_execution_section(snapshot)
    runtime_path_error = _safe_text(st.session_state.pop("_runtime_path_error", ""))
    if runtime_path_error:
        st.warning(runtime_path_error)
    _persist_runtime_settings(force=bool(st.session_state.get("_runtime_settings_needs_heal")))

    _render_step_header("📋 Export할 Account 선택하기")
    _render_account_selection_flow(snapshot)

    _render_step_header("📊 진행 상황")
    _render_bottom_section(snapshot)

    if snapshot.get("is_running"):
        st.caption("Run is in progress. Auto-refresh every 2 seconds.")
        time.sleep(2)
        st.rerun()


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


def _safe_path(path_text: str) -> Path:
    expanded = _safe_text(os.path.expandvars(path_text))
    return Path(expanded).expanduser().resolve()


def _default_runtime_settings() -> dict[str, str]:
    return {
        "browser": "msedge",
        "output_dir": str(DEFAULT_USER_OUTPUT_DIR),
        "downloads_dir": str(DEFAULT_USER_DOWNLOADS_DIR),
        "logs_dir": str(DEFAULT_USER_LOGS_DIR),
    }


def _validate_runtime_path(value: Any, *, check_writable: bool) -> tuple[bool, str]:
    raw_value = _safe_text(value)
    if not raw_value:
        return False, INVALID_RUNTIME_PATH_MESSAGE

    expanded = _safe_text(os.path.expandvars(raw_value))
    candidate = expanded.replace("/", "\\")

    if not _WINDOWS_ABS_DRIVE_RE.match(candidate):
        return False, INVALID_RUNTIME_PATH_MESSAGE

    drive_tokens = _WINDOWS_DRIVE_TOKEN_RE.findall(candidate)
    if len(drive_tokens) != 1:
        return False, INVALID_RUNTIME_PATH_MESSAGE

    lowered = candidate.lower()
    if "\\onedrive" in lowered:
        return False, INVALID_RUNTIME_PATH_MESSAGE

    invalid_chars = set('<>:"|?*')
    for part in PureWindowsPath(candidate).parts[1:]:
        segment = _safe_text(part).rstrip("\\/")
        if not segment:
            continue
        if any(char in invalid_chars for char in segment):
            return False, INVALID_RUNTIME_PATH_MESSAGE

    normalized = str(Path(candidate).expanduser())
    if check_writable:
        try:
            target_dir = Path(normalized)
            target_dir.mkdir(parents=True, exist_ok=True)
            probe_path = target_dir / f".path_probe_{time.time_ns()}.tmp"
            probe_path.write_text("ok", encoding="utf-8")
            probe_path.unlink(missing_ok=True)
        except Exception:
            return False, INVALID_RUNTIME_PATH_MESSAGE

    return True, normalized


def _sanitize_loaded_runtime_settings(runtime_settings: dict[str, str]) -> tuple[dict[str, str], bool]:
    defaults = _default_runtime_settings()
    sanitized = dict(defaults)
    has_invalid = False

    raw_browser = _safe_text(runtime_settings.get("browser")).lower()
    if raw_browser:
        if raw_browser in {"msedge", "chrome", "auto", "chromium"}:
            sanitized["browser"] = raw_browser
        else:
            has_invalid = True

    for path_key in RUNTIME_PATH_KEYS:
        raw_value = _safe_text(runtime_settings.get(path_key))
        if not raw_value:
            if path_key in runtime_settings:
                has_invalid = True
            continue
        is_valid, normalized_or_message = _validate_runtime_path(raw_value, check_writable=False)
        if is_valid:
            sanitized[path_key] = normalized_or_message
        else:
            has_invalid = True

    return sanitized, has_invalid


def _push_runtime_path_warning() -> None:
    st.session_state["_runtime_path_error"] = INVALID_RUNTIME_PATH_MESSAGE


def _on_runtime_path_input_change(path_key: str) -> None:
    input_key = RUNTIME_INPUT_KEY_BY_PATH_KEY[path_key]
    candidate = _safe_text(st.session_state.get(input_key))
    is_valid, normalized_or_message = _validate_runtime_path(candidate, check_writable=False)
    if is_valid:
        st.session_state[path_key] = normalized_or_message
        st.session_state[f"_runtime_valid_{path_key}"] = normalized_or_message
        return

    fallback = _safe_text(st.session_state.get(f"_runtime_valid_{path_key}"))
    if not fallback:
        fallback = _default_runtime_settings()[path_key]
    st.session_state[path_key] = fallback
    st.session_state[f"_runtime_valid_{path_key}"] = fallback
    _push_runtime_path_warning()


def _on_output_dir_input_change() -> None:
    _on_runtime_path_input_change("output_dir")


def _on_downloads_dir_input_change() -> None:
    _on_runtime_path_input_change("downloads_dir")


def _on_logs_dir_input_change() -> None:
    _on_runtime_path_input_change("logs_dir")


def _validate_runtime_paths_before_run() -> tuple[bool, dict[str, str]]:
    normalized_paths: dict[str, str] = {}
    for path_key in RUNTIME_PATH_KEYS:
        is_valid, normalized_or_message = _validate_runtime_path(
            st.session_state.get(path_key),
            check_writable=True,
        )
        if not is_valid:
            _push_runtime_path_warning()
            return False, {}
        normalized_paths[path_key] = normalized_or_message

    for path_key, normalized in normalized_paths.items():
        st.session_state[path_key] = normalized
        st.session_state[f"_runtime_valid_{path_key}"] = normalized

    return True, normalized_paths


def _app_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _exc_text(exc: Exception) -> str:
    text = str(exc or "").strip()
    if text:
        return text
    rep = repr(exc)
    if rep:
        return rep
    return exc.__class__.__name__


def _now_run_id() -> str:
    return dt.datetime.now().strftime("%Y%m%d_%H%M%S")


def _runtime_settings_path(base_dir: Path) -> Path:
    return (base_dir / RUNTIME_SETTINGS_RELATIVE_PATH).resolve()


def _load_runtime_settings(base_dir: Path) -> dict[str, str]:
    settings_path = _runtime_settings_path(base_dir)
    if not settings_path.exists():
        return {}
    try:
        parsed = json.loads(settings_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    if not isinstance(parsed, dict):
        return {}

    out: dict[str, str] = {}
    for key in ("browser", "output_dir", "downloads_dir", "logs_dir"):
        value = _safe_text(parsed.get(key))
        if value:
            out[key] = value
    return out


def _runtime_settings_payload(base_dir: Path) -> dict[str, str]:
    _ = base_dir
    defaults = _default_runtime_settings()
    browser = _safe_text(st.session_state.get("browser")).lower() or defaults["browser"]
    if browser not in {"msedge", "chrome", "auto", "chromium"}:
        browser = defaults["browser"]
    return {
        "browser": browser,
        "output_dir": _safe_text(st.session_state.get("output_dir")) or defaults["output_dir"],
        "downloads_dir": _safe_text(st.session_state.get("downloads_dir")) or defaults["downloads_dir"],
        "logs_dir": _safe_text(st.session_state.get("logs_dir")) or defaults["logs_dir"],
    }


def _persist_runtime_settings(*, force: bool = False) -> None:
    base_dir = _app_base_dir().resolve()
    settings_path = _runtime_settings_path(base_dir)
    payload = _runtime_settings_payload(base_dir)
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    if (not force) and (_safe_text(st.session_state.get("_runtime_settings_last_saved")) == serialized):
        return

    try:
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = settings_path.with_suffix(settings_path.suffix + ".tmp")
        temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp_path.replace(settings_path)
        st.session_state["_runtime_settings_last_saved"] = serialized
        st.session_state["_runtime_settings_needs_heal"] = False
    except Exception:  # noqa: BLE001
        # 설정 저장 실패는 실행을 막지 않는다.
        st.session_state["_runtime_settings_needs_heal"] = True
        return


def _inject_ui_css() -> None:
    st.markdown(
        """
        <style>
        div[class*="st-key-main_start_btn"] button {
            min-height: 3rem !important;
            font-size: 1.02rem !important;
            font-weight: 700 !important;
            letter-spacing: 0.01em;
        }
        .ga-main-card-title {
            font-size: 1.02rem;
            font-weight: 700;
            margin-bottom: 0.2rem;
            color: #1f2937;
        }
        .ga-caption-muted {
            color: #6b7280;
            font-size: 0.84rem;
        }
        .ga-guide-box {
            border: 1px solid #d8e0ec;
            border-radius: 10px;
            background: #f8fafc;
            padding: 0.72rem 0.9rem;
            margin-bottom: 0.7rem;
            color: #4b5563;
            font-size: 0.88rem;
            line-height: 1.35;
        }
        .ga-hint-text {
            color: #6b7280;
            font-size: 0.84rem;
            line-height: 1.25;
            margin-top: 0.1rem;
            margin-bottom: 0.35rem;
        }
        .ga-disabled-box {
            border: 1px dashed #cfd6e0;
            border-radius: 10px;
            background: #f8fafc;
            padding: 0.75rem 0.9rem;
            margin: 0.25rem 0 0.8rem 0;
            color: #6b7280;
            font-size: 0.84rem;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _init_session_state() -> None:
    base_dir = _app_base_dir().resolve()
    runtime_settings_raw = _load_runtime_settings(base_dir)
    runtime_settings, has_invalid_runtime_settings = _sanitize_loaded_runtime_settings(runtime_settings_raw)

    if "execution_store" not in st.session_state:
        st.session_state["execution_store"] = create_execution_store()

    st.session_state.setdefault("accounts", [])
    st.session_state.setdefault("selected_cids", set())
    st.session_state.setdefault("scan_results", {})
    st.session_state.setdefault("matching_ready", False)
    st.session_state.setdefault("started", False)
    st.session_state.setdefault("opened_output_for_run", "")
    st.session_state.setdefault("validated_output_count_run", "")
    st.session_state.setdefault("expected_output_count", 0)
    st.session_state.setdefault("run_output_dir", "")
    st.session_state.setdefault("run_downloads_dir", "")
    st.session_state.setdefault("run_logs_dir", "")
    st.session_state.setdefault("run_date_folder", "")
    st.session_state.setdefault("_runtime_settings_needs_heal", False)
    st.session_state.setdefault("_runtime_path_error", "")
    if has_invalid_runtime_settings:
        st.session_state["_runtime_settings_needs_heal"] = True
        st.session_state["_runtime_path_error"] = INVALID_RUNTIME_PATH_MESSAGE

    st.session_state.setdefault("env_file", ".env")
    st.session_state.setdefault(
        "browser",
        runtime_settings.get("browser") or _safe_text(os.getenv("GOOGLE_ADS_BROWSER", "msedge")).lower() or "msedge",
    )
    if _safe_text(st.session_state.get("browser")).lower() not in {"msedge", "chrome", "auto", "chromium"}:
        st.session_state["browser"] = runtime_settings["browser"]
        st.session_state["_runtime_settings_needs_heal"] = True
    st.session_state.setdefault(
        "target_map_path",
        _safe_text(os.getenv("GOOGLE_ADS_TARGET_MAP", str(_default_target_map_path()))),
    )
    st.session_state.setdefault(
        "runtime_dir",
        _safe_text(os.getenv("GOOGLE_ADS_RUNTIME_DIR", "")),
    )
    st.session_state.setdefault(
        "user_data_dir",
        _safe_text(os.getenv("GOOGLE_ADS_USER_DATA_DIR", "")),
    )
    st.session_state.setdefault(
        "downloads_dir",
        runtime_settings.get("downloads_dir")
        or _safe_text(os.getenv("GOOGLE_ADS_OUTPUT_DIR", str(DEFAULT_USER_DOWNLOADS_DIR))),
    )
    st.session_state.setdefault(
        "output_dir",
        runtime_settings.get("output_dir") or str(DEFAULT_USER_OUTPUT_DIR),
    )
    st.session_state.setdefault(
        "logs_dir",
        runtime_settings.get("logs_dir")
        or _safe_text(os.getenv("GOOGLE_ADS_LOGS_DIR", str(DEFAULT_USER_LOGS_DIR))),
    )

    for path_key in RUNTIME_PATH_KEYS:
        is_valid, normalized_or_message = _validate_runtime_path(
            st.session_state.get(path_key),
            check_writable=False,
        )
        if not is_valid:
            normalized = runtime_settings[path_key]
            st.session_state["_runtime_settings_needs_heal"] = True
            st.session_state["_runtime_path_error"] = INVALID_RUNTIME_PATH_MESSAGE
        else:
            normalized = normalized_or_message
        st.session_state[path_key] = normalized
        st.session_state[f"_runtime_valid_{path_key}"] = normalized
        input_key = RUNTIME_INPUT_KEY_BY_PATH_KEY[path_key]
        st.session_state.setdefault(input_key, normalized)

    st.session_state.setdefault(
        "_runtime_settings_last_saved",
        json.dumps(_runtime_settings_payload(base_dir), ensure_ascii=False, sort_keys=True),
    )


def _render_sidebar_execution_section(snapshot: dict[str, Any]) -> None:
    with st.sidebar:
        st.subheader("⚙️ Run Settings")
        browser_options = ["msedge", "chrome", "auto", "chromium"]
        current_browser = _safe_text(st.session_state.get("browser", "msedge")).lower()
        if current_browser not in browser_options:
            current_browser = "msedge"
        st.session_state["browser"] = st.selectbox(
            "브라우저",
            options=browser_options,
            index=browser_options.index(current_browser),
        )
        st.text_input(
            "결과 저장 경로",
            key=RUNTIME_INPUT_KEY_BY_PATH_KEY["output_dir"],
            on_change=_on_output_dir_input_change,
        )
        st.text_input(
            "다운로드 경로",
            key=RUNTIME_INPUT_KEY_BY_PATH_KEY["downloads_dir"],
            on_change=_on_downloads_dir_input_change,
        )
        st.text_input(
            "로그 경로",
            key=RUNTIME_INPUT_KEY_BY_PATH_KEY["logs_dir"],
            on_change=_on_logs_dir_input_change,
        )
        if bool(snapshot.get("is_running")):
            st.info("실행 중입니다...")


def _render_step_header(title: str, hint: str = "") -> None:
    st.subheader(title)
    if hint:
        st.markdown(
            f"<p class='ga-hint-text' title='{hint}'>{hint}</p>",
            unsafe_allow_html=True,
        )


def _render_account_selection_flow(snapshot: dict[str, Any]) -> None:
    with st.container(border=True):
        _render_main_login_section(snapshot)
        _render_account_export_section(snapshot)


def _render_main_login_section(snapshot: dict[str, Any]) -> None:
    is_running = bool(snapshot.get("is_running"))
    account_count = len(st.session_state.get("accounts", []))

    cols = st.columns([4.3, 1.2], vertical_alignment="center")
    with cols[0]:
        st.markdown("<div class='ga-main-card-title'>Google Ads 로그인</div>", unsafe_allow_html=True)
        st.markdown(
            "<p class='ga-caption-muted'>로그인하면 계정목록을 선택할 수 있습니다.</p>",
            unsafe_allow_html=True,
        )
        if account_count > 0:
            st.markdown(
                f"<p class='ga-caption-muted'>계정 {account_count}개를 불러왔습니다.</p>",
                unsafe_allow_html=True,
            )
    with cols[1]:
        if st.button(
            "로그인하기",
            type="primary",
            disabled=is_running,
            use_container_width=True,
            key="main_start_btn",
        ):
            with st.spinner("Google Ads 로그인 확인 및 계정 크롤링 중입니다..."):
                _handle_start()
            st.rerun()


def _render_account_export_section(snapshot: dict[str, Any]) -> None:
    st.markdown("<div class='ga-main-card-title'>Account 선택</div>", unsafe_allow_html=True)
    st.markdown(
        "<p class='ga-caption-muted'>선택한 계정에서 사전 세팅된 Report를 매칭해 다운로드합니다.</p>",
        unsafe_allow_html=True,
    )
    _render_account_selector(snapshot)
    _render_proceed_export(snapshot)


def _prepare_run_directories() -> dict[str, str]:
    run_date = dt.datetime.now().strftime("%Y%m%d")
    output_base = _safe_path(_safe_text(st.session_state.get("output_dir")))
    downloads_base = _safe_path(_safe_text(st.session_state.get("downloads_dir")))
    logs_base = _safe_path(_safe_text(st.session_state.get("logs_dir")))

    run_output_dir = (output_base / run_date / "output").resolve()
    run_downloads_dir = (downloads_base / run_date / "raw").resolve()
    run_logs_dir = (logs_base / run_date / "log").resolve()

    run_output_dir.mkdir(parents=True, exist_ok=True)
    run_downloads_dir.mkdir(parents=True, exist_ok=True)
    run_logs_dir.mkdir(parents=True, exist_ok=True)

    st.session_state["run_output_dir"] = str(run_output_dir)
    st.session_state["run_downloads_dir"] = str(run_downloads_dir)
    st.session_state["run_logs_dir"] = str(run_logs_dir)
    st.session_state["run_date_folder"] = run_date

    return {
        "run_date": run_date,
        "output_dir": str(run_output_dir),
        "downloads_dir": str(run_downloads_dir),
        "logs_dir": str(run_logs_dir),
    }


def _apply_runtime_settings(
    *,
    downloads_dir_override: str = "",
    logs_dir_override: str = "",
) -> None:
    env_file = _safe_text(st.session_state.get("env_file"))
    if env_file:
        load_env_file(env_file)

    downloads_dir = _safe_text(downloads_dir_override) or _safe_text(st.session_state.get("downloads_dir"))
    logs_dir = _safe_text(logs_dir_override) or _safe_text(st.session_state.get("logs_dir"))

    _apply_runtime_directory_overrides(
        runtime_dir=_safe_text(st.session_state.get("runtime_dir")),
        output_dir=downloads_dir,
        logs_dir=logs_dir,
        user_data_dir=_safe_text(st.session_state.get("user_data_dir")),
    )


def _selected_accounts() -> list[AdsAccount]:
    selected = set(st.session_state.get("selected_cids", set()))
    accounts: list[AdsAccount] = st.session_state.get("accounts", [])
    if not selected:
        return []
    return [account for account in accounts if account.cid_digits in selected]


def _handle_start() -> None:
    store = st.session_state["execution_store"]
    if store.is_running():
        return

    paths_valid, _ = _validate_runtime_paths_before_run()
    if not paths_valid:
        store.push_event(
            {
                "type": "run_warning",
                "message": INVALID_RUNTIME_PATH_MESSAGE,
            }
        )
        return

    run_dirs = _prepare_run_directories()
    _apply_runtime_settings(
        downloads_dir_override=run_dirs["downloads_dir"],
        logs_dir_override=run_dirs["logs_dir"],
    )
    _logger, log_path = setup_logger("google_ads_exporter.streamlit")
    store.push_event(
        {
            "type": "run_started",
            "run_id": _now_run_id(),
            "log_file": str(log_path),
            "run_status": "Preparing",
        }
    )

    try:
        accounts, _browser_used, worker_log_file = login_and_crawl_accounts_in_subprocess(
            browser=_safe_text(st.session_state.get("browser")) or "msedge",
            headless=False,
            target_map_path=_safe_text(st.session_state.get("target_map_path")),
            progress_cb=store.push_event,
        )
        st.session_state["accounts"] = accounts
        st.session_state["selected_cids"] = set()
        st.session_state["scan_results"] = {}
        st.session_state["matching_ready"] = False
        st.session_state["started"] = True
        st.session_state["opened_output_for_run"] = ""
        st.session_state["validated_output_count_run"] = ""
        st.session_state["expected_output_count"] = 0
        store.push_event({"type": "scan_results", "rows": []})

        store.push_event(
            {
                "type": "run_completed",
                "run_status": "Ready",
                "message": f"Account crawl completed: {len(accounts)}",
            }
        )
        if worker_log_file:
            store.push_event(
                {
                    "type": "run_warning",
                    "message": f"로그 파일 확인: {worker_log_file}",
                }
            )
    except Exception as exc:  # noqa: BLE001
        error_text = _exc_text(exc)
        _logger.exception("start flow failed: %s", error_text)
        st.session_state["started"] = False
        store.push_event(
            {
                "type": "run_failed",
                "error": f"Start failed: {error_text}",
            }
        )
        store.push_event(
            {
                "type": "run_warning",
                "message": f"로그 파일 확인: {log_path}",
            }
        )

    store.drain_events()


def _render_account_selector(snapshot: dict[str, Any]) -> None:
    if not st.session_state.get("started", False):
        st.markdown(
            "<div class='ga-disabled-box' title='로그인하기를 눌러 계정 목록을 불러오세요.'>"
            "로그인하면 계정 목록을 가져옵니다."
            "</div>",
            unsafe_allow_html=True,
        )
        return

    accounts: list[AdsAccount] = st.session_state.get("accounts", [])
    if not accounts:
        st.warning("크롤링된 계정이 없습니다.")
        return

    selected_cids: set[str] = st.session_state.get("selected_cids", set())
    rows: list[dict[str, Any]] = []
    for account in accounts:
        rows.append(
            {
                "export": account.cid_digits in selected_cids,
                "account_name": account.name,
                "cid": account.cid,
                "is_manager": account.is_manager,
                "cid_digits": account.cid_digits,
            }
        )

    edited = st.data_editor(
        pd.DataFrame(rows),
        use_container_width=True,
        hide_index=True,
        column_config={
            "export": st.column_config.CheckboxColumn("Export", default=False),
            "account_name": st.column_config.TextColumn("Account"),
            "cid": st.column_config.TextColumn("CID"),
            "is_manager": st.column_config.CheckboxColumn("Manager", disabled=True),
            "cid_digits": None,
        },
        disabled=["account_name", "cid", "is_manager", "cid_digits"],
    )

    new_selected = {
        row["cid_digits"]
        for row in edited.to_dict(orient="records")
        if bool(row.get("export"))
    }

    if new_selected != selected_cids:
        st.session_state["scan_results"] = {}
        st.session_state["matching_ready"] = False
        st.session_state["opened_output_for_run"] = ""
        st.session_state["validated_output_count_run"] = ""
        st.session_state["expected_output_count"] = 0
        st.session_state["execution_store"].push_event({"type": "scan_results", "rows": []})

    st.session_state["selected_cids"] = new_selected
    st.caption(f"Selected accounts: {len(new_selected)}")


def _render_proceed_export(snapshot: dict[str, Any]) -> None:
    disabled = (
        bool(snapshot.get("is_running"))
        or (not st.session_state.get("started", False))
        or (len(_selected_accounts()) == 0)
    )
    if st.session_state.get("started", False) and len(_selected_accounts()) == 0:
        st.markdown(
            "<div class='ga-disabled-box'>계정을 선택하면 Export하기가 활성화됩니다.</div>",
            unsafe_allow_html=True,
        )
    if st.button(
        "Export하기",
        type="primary",
        use_container_width=True,
        disabled=disabled,
        key="proceed_export_btn",
    ):
        with st.spinner("매칭 후 Export를 시작합니다..."):
            _handle_start_export()
        st.rerun()


def _handle_start_export() -> None:
    store = st.session_state["execution_store"]
    if store.is_running():
        return

    selected_accounts = _selected_accounts()
    if not selected_accounts:
        return

    paths_valid, _ = _validate_runtime_paths_before_run()
    if not paths_valid:
        store.push_event(
            {
                "type": "run_warning",
                "message": INVALID_RUNTIME_PATH_MESSAGE,
            }
        )
        return

    run_dirs = _prepare_run_directories()
    _apply_runtime_settings(
        downloads_dir_override=run_dirs["downloads_dir"],
        logs_dir_override=run_dirs["logs_dir"],
    )
    logger, log_path = setup_logger("google_ads_exporter.streamlit")

    st.session_state["expected_output_count"] = 0
    st.session_state["opened_output_for_run"] = ""
    st.session_state["validated_output_count_run"] = ""
    st.session_state["matching_ready"] = False

    store.initialize_rows(selected_accounts)
    store.push_event(
        {
            "type": "run_started",
            "run_id": _now_run_id(),
            "log_file": str(log_path),
            "run_status": "Matching",
        }
    )

    ok, message = start_export_execution(
        store=store,
        runner=run_scan_and_export_in_subprocess,
        runner_kwargs={
            "selected_accounts": selected_accounts,
            "browser": _safe_text(st.session_state.get("browser")) or "msedge",
            "headless": False,
            "target_map_path": _safe_text(st.session_state.get("target_map_path")),
            "final_output_dir": _safe_path(run_dirs["output_dir"]),
            "downloads_dir": _safe_path(run_dirs["downloads_dir"]),
        },
    )
    if not ok:
        store.push_event(
            {
                "type": "run_warning",
                "message": message,
            }
        )
    store.drain_events()


def _normalized_status_key(value: Any) -> str:
    text = _safe_text(value).replace("_", " ").replace("-", " ").lower()
    text = " ".join(text.split())
    if text == "excel written":
        return "excel_written"
    return text


def _ui_phase_key(value: Any) -> str:
    key = _normalized_status_key(value)
    if key in {"failed", "error"}:
        return "failed"
    if key in {"completed", "excel_written"}:
        return "completed"
    if key in {"downloading", "downloaded", "exporting"}:
        return "exporting"
    if key in {"pending", "scanning", "matched", "not found", "ready", "waiting"}:
        return "waiting"
    return "waiting"


def _status_label_text(value: Any) -> str:
    phase = _ui_phase_key(value)
    return STATUS_LABEL.get(phase, "Waiting")


def _status_style_text(value: Any) -> str:
    phase = _ui_phase_key(value)
    return STATUS_STYLE.get(phase, "")


def _style_status_column(df: pd.DataFrame, status_column: str = "상태"):
    if status_column not in df.columns:
        return df.style
    styler = df.style
    if hasattr(styler, "map"):
        return styler.map(_status_style_text, subset=[status_column])
    if hasattr(styler, "applymap"):
        return styler.applymap(_status_style_text, subset=[status_column])
    return styler


def _render_bottom_section(snapshot: dict[str, Any]) -> None:
    st.markdown("#### 실행 로그")
    rows = snapshot.get("rows") or []
    if rows:
        row_df = pd.DataFrame(
            [
                {
                    "account": row.account,
                    "cid": row.cid,
                    "activity": row.activity,
                    "target": row.target_display,
                    "status": _status_label_text(row.status),
                    "message": row.message,
                    "last_updated": row.last_updated,
                }
                for row in rows
                if isinstance(row, LogRow)
            ]
        ).rename(
            columns={
                "account": "계정",
                "cid": "CID",
                "activity": "액티비티",
                "target": "리포트",
                "status": "상태",
                "message": "메시지",
                "last_updated": "최종 갱신",
            }
        )
        styled_row_df = _style_status_column(row_df, "상태")
        st.dataframe(styled_row_df, use_container_width=True, hide_index=True)
    else:
        st.markdown(
            "<div class='ga-disabled-box'>실행 로그가 없습니다.</div>",
            unsafe_allow_html=True,
        )

    st.markdown("#### 통합본 생성 상태")
    account_stage_rows = snapshot.get("account_stage_rows") or []
    if account_stage_rows:
        def _account_key(name: str, cid: str, activity_key: str) -> str:
            return f"{_safe_text(name)}|{_safe_text(cid)}|{_safe_text(activity_key)}"

        progress_map: dict[str, dict[str, int]] = {}
        for row in rows:
            if not isinstance(row, LogRow):
                continue
            key = _account_key(row.account, row.cid, row.activity_key)
            if key not in progress_map:
                progress_map[key] = {"completed": 0, "total": 0}
            progress_map[key]["total"] += 1
            if _ui_phase_key(row.status) == "completed":
                progress_map[key]["completed"] += 1

        account_stage_df = pd.DataFrame(
            [
                {
                    "account": row.account,
                    "cid": row.cid,
                    "activity": row.activity,
                    "status": _status_label_text(row.status),
                    "processed_sheets": (
                        f"{progress_map.get(_account_key(row.account, row.cid, row.activity_key), {}).get('completed', 0)}/"
                        f"{progress_map.get(_account_key(row.account, row.cid, row.activity_key), {}).get('total', 0)}"
                    ),
                    "message": row.message,
                    "updated_at": row.updated_at,
                }
                for row in account_stage_rows
                if isinstance(row, AccountStageRow)
            ]
        ).rename(
            columns={
                "account": "계정",
                "cid": "CID",
                "activity": "액티비티",
                "status": "상태",
                "processed_sheets": "처리 시트 수",
                "message": "메시지",
                "updated_at": "시간",
            }
        )
        account_stage_df = account_stage_df.sort_values(by=["시간"], ascending=False)
        styled_account_stage_df = _style_status_column(account_stage_df, "상태")
        st.dataframe(styled_account_stage_df, use_container_width=True, hide_index=True)
    else:
        st.markdown(
            "<div class='ga-disabled-box'>다운로드 후 통합본 생성 상태가 표시됩니다.</div>",
            unsafe_allow_html=True,
        )


def _validate_output_count(snapshot: dict[str, Any]) -> None:
    run_id = _safe_text(snapshot.get("run_id"))
    run_status = _safe_text(snapshot.get("run_status"))
    if not run_id or run_status not in {"Completed", "Completed (With Failures)"}:
        return

    if _safe_text(st.session_state.get("validated_output_count_run")) == run_id:
        return

    expected = int(st.session_state.get("expected_output_count") or 0)
    if expected <= 0:
        scan_rows = snapshot.get("scan_result_rows") or []
        unique_activity_keys: set[str] = set()
        for row in scan_rows:
            if not isinstance(row, dict):
                continue
            account = _safe_text(row.get("account"))
            cid = _safe_text(row.get("cid"))
            activity_key = _safe_text(row.get("activity_key"))
            if account and cid and activity_key:
                unique_activity_keys.add(f"{account}|{cid}|{activity_key}")
        expected = len(unique_activity_keys)
        st.session_state["expected_output_count"] = expected

    actual = len(snapshot.get("outputs") or [])
    if run_status == "Completed" and expected > 0 and actual != expected:
        st.session_state["execution_store"].push_event(
            {
                "type": "run_warning",
                "message": f"Expected workbook count={expected}, actual={actual}.",
            }
        )
    st.session_state["validated_output_count_run"] = run_id


def _open_output_folder_for_completed_run(snapshot: dict[str, Any]) -> None:
    run_status = _safe_text(snapshot.get("run_status"))
    run_id = _safe_text(snapshot.get("run_id"))
    if run_status not in {"Completed", "Completed (With Failures)"} or not run_id:
        return

    if _safe_text(st.session_state.get("opened_output_for_run")) == run_id:
        return

    outputs = snapshot.get("outputs") or []
    opened = False
    for item in outputs:
        if not isinstance(item, dict):
            continue
        workbook_path = _safe_text(item.get("workbook_path"))
        if workbook_path:
            opened = open_file_in_explorer(Path(workbook_path)) or opened

    if not opened:
        run_output_dir = _safe_text(st.session_state.get("run_output_dir"))
        if run_output_dir:
            try:
                subprocess.Popen(["explorer", run_output_dir])  # noqa: S603
            except Exception:
                pass

    st.session_state["opened_output_for_run"] = run_id


if __name__ == "__main__":
    run_streamlit_app()
