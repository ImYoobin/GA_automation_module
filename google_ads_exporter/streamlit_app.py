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
    ActionLogRow,
    LogRow,
    create_execution_store,
    start_export_execution,
)
from google_ads_exporter.action_log_downloader import build_action_log_run_dir
from google_ads_exporter.google_adapter import (
    login_and_crawl_accounts_in_subprocess,
    run_scan_and_export_in_subprocess,
)
from google_ads_exporter.main import _apply_runtime_directory_overrides, _default_target_map_path
from google_ads_exporter.models import AdsAccount
from google_ads_exporter.utils import setup_logger

STATUS_LABEL = {
    "waiting": "Waiting",
    "exporting": "Exporting",
    "completed": "Completed",
    "failed": "Failed",
    "not_found": "Not Found",
}

STATUS_STYLE = {
    "waiting": "background-color: #f1f5f9; color: #64748b; font-weight: 600;",
    "exporting": "background-color: #dbeafe; color: #1d4ed8; font-weight: 700;",
    "completed": "background-color: #dff3e6; color: #166534; font-weight: 700;",
    "failed": "background-color: #fee2e2; color: #b91c1c; font-weight: 700;",
    "not_found": "background-color: #fff7ed; color: #c2410c; font-weight: 700;",
}

RUNTIME_SETTINGS_RELATIVE_PATH = Path("config") / "runtime_settings.json"
LEGACY_RUNTIME_PATH_KEYS: tuple[str, ...] = ("output_dir", "downloads_dir", "logs_dir")
BASE_PARENT_INPUT_KEY = "base_parent_dir_input"
EXPORT_ROOT_DIRNAME = "GoogleAdsExport"
DEFAULT_USER_PARENT_DIR = Path.home()
DEFAULT_USER_PARENT_DIR_TOKEN = "%USERPROFILE%"
INVALID_RUNTIME_PATH_MESSAGE = "올바르지 않은 부모 경로입니다. 로컬 PC의 폴더 경로를 입력해주세요."
_WINDOWS_ABS_DRIVE_RE = re.compile(r"^[A-Za-z]:\\")
_WINDOWS_DRIVE_TOKEN_RE = re.compile(r"[A-Za-z]:\\")


def run_streamlit_app() -> None:
    st.set_page_config(page_title="Google Ads Auto Download", layout="wide")
    _init_session_state()
    _inject_ui_css()

    store = st.session_state["execution_store"]
    store.drain_events()
    snapshot = store.snapshot()

    _validate_output_count(snapshot)
    store.drain_events()
    snapshot = store.snapshot()

    _open_output_folder_for_completed_run(snapshot)
    _apply_ready_login_result(snapshot)

    st.title("Google Ads Auto Download")
    st.markdown(
        "광고계정에 미리 리포트/뷰를 세팅해주세요.<br>'BCG_auto_리포트/뷰이름_액티비티' 이름을 기준으로 감지합니다.",
        unsafe_allow_html=True,
    )

    _render_sidebar_execution_section(snapshot)
    runtime_path_error = _safe_text(st.session_state.pop("_runtime_path_error", ""))
    if runtime_path_error:
        st.warning(runtime_path_error)
    _persist_runtime_settings(force=bool(st.session_state.get("_runtime_settings_needs_heal")))

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
        "base_parent_dir": DEFAULT_USER_PARENT_DIR_TOKEN,
    }


def _current_run_date_token() -> str:
    return _safe_text(st.session_state.get("run_date_folder")) or dt.datetime.now().strftime("%Y%m%d")


def _build_storage_roots(base_parent_dir_text: str = "") -> dict[str, Path]:
    parent_dir = _safe_path(
        _safe_text(base_parent_dir_text)
        or _safe_text(st.session_state.get("base_parent_dir"))
        or str(DEFAULT_USER_PARENT_DIR)
    )
    export_root = (parent_dir / EXPORT_ROOT_DIRNAME).resolve()
    output_root = (export_root / "output").resolve()
    return {
        "base": export_root,
        "raw_root": (export_root / "raw").resolve(),
        "trace_root": (export_root / "trace").resolve(),
        "output_root": output_root,
        "action_log_root": (output_root / "action_log").resolve(),
    }


def _build_run_storage_paths(base_parent_dir_text: str = "", run_date: str = "") -> dict[str, Path]:
    roots = _build_storage_roots(base_parent_dir_text)
    effective_run_date = _safe_text(run_date) or _current_run_date_token()
    return {
        "run_date": effective_run_date,
        "base": roots["base"],
        "raw_root": roots["raw_root"],
        "trace_root": roots["trace_root"],
        "output_root": roots["output_root"],
        "action_log_root": roots["action_log_root"],
        "raw_dir": (roots["raw_root"] / effective_run_date).resolve(),
        "trace_dir": (roots["trace_root"] / effective_run_date).resolve(),
        "output_dir": (roots["output_root"] / effective_run_date).resolve(),
        "action_log_dir": build_action_log_run_dir(roots["output_root"], effective_run_date),
    }


def _serialize_base_parent_dir_for_settings(base_parent_dir_text: str) -> str:
    normalized = _safe_path(base_parent_dir_text)
    if normalized == DEFAULT_USER_PARENT_DIR.resolve():
        return DEFAULT_USER_PARENT_DIR_TOKEN
    return str(normalized)


def _infer_base_parent_dir_from_legacy_settings(runtime_settings: dict[str, str]) -> str:
    for key in LEGACY_RUNTIME_PATH_KEYS:
        raw_value = _safe_text(runtime_settings.get(key))
        if not raw_value:
            continue
        try:
            candidate_path = _safe_path(raw_value)
        except Exception:  # noqa: BLE001
            continue
        for ancestor in (candidate_path, *candidate_path.parents):
            if ancestor.name.lower() == EXPORT_ROOT_DIRNAME.lower():
                return str(ancestor.parent)
    return ""


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
    sanitized = {
        "browser": "msedge",
        "base_parent_dir": str(DEFAULT_USER_PARENT_DIR),
    }
    has_invalid = False

    raw_browser = _safe_text(runtime_settings.get("browser")).lower()
    if raw_browser:
        if raw_browser in {"msedge", "chrome", "auto", "chromium"}:
            sanitized["browser"] = raw_browser
        else:
            has_invalid = True

    raw_parent_dir = _safe_text(runtime_settings.get("base_parent_dir")) or _infer_base_parent_dir_from_legacy_settings(
        runtime_settings
    )
    if raw_parent_dir:
        is_valid, normalized_or_message = _validate_runtime_path(raw_parent_dir, check_writable=False)
        if is_valid:
            sanitized["base_parent_dir"] = normalized_or_message
        else:
            has_invalid = True
    elif any(_safe_text(runtime_settings.get(key)) for key in LEGACY_RUNTIME_PATH_KEYS):
        has_invalid = True

    return sanitized, has_invalid


def _push_runtime_path_warning() -> None:
    st.session_state["_runtime_path_error"] = INVALID_RUNTIME_PATH_MESSAGE


def _on_base_parent_dir_input_change() -> None:
    candidate = _safe_text(st.session_state.get(BASE_PARENT_INPUT_KEY))
    is_valid, normalized_or_message = _validate_runtime_path(candidate, check_writable=False)
    if is_valid:
        st.session_state["base_parent_dir"] = normalized_or_message
        st.session_state["_runtime_valid_base_parent_dir"] = normalized_or_message
        st.session_state[BASE_PARENT_INPUT_KEY] = normalized_or_message
        return

    fallback = _safe_text(st.session_state.get("_runtime_valid_base_parent_dir"))
    if not fallback:
        fallback = str(DEFAULT_USER_PARENT_DIR)
    st.session_state["base_parent_dir"] = fallback
    st.session_state["_runtime_valid_base_parent_dir"] = fallback
    st.session_state[BASE_PARENT_INPUT_KEY] = fallback
    _push_runtime_path_warning()

def _validate_runtime_paths_before_run() -> tuple[bool, dict[str, str]]:
    is_valid, normalized_or_message = _validate_runtime_path(
        st.session_state.get("base_parent_dir"),
        check_writable=True,
    )
    if not is_valid:
        _push_runtime_path_warning()
        return False, {}

    st.session_state["base_parent_dir"] = normalized_or_message
    st.session_state["_runtime_valid_base_parent_dir"] = normalized_or_message
    return True, {"base_parent_dir": normalized_or_message}


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


def _account_to_payload(account: AdsAccount) -> dict[str, Any]:
    return {
        "name": account.name,
        "cid": account.cid,
        "cid_digits": account.cid_digits,
        "is_manager": account.is_manager,
        "raw_text": account.raw_text,
    }


def _accounts_from_payload(payloads: list[dict[str, Any]]) -> list[AdsAccount]:
    accounts: list[AdsAccount] = []
    for item in payloads:
        if not isinstance(item, dict):
            continue
        cid = _safe_text(item.get("cid"))
        cid_digits = _safe_text(item.get("cid_digits"))
        if not cid or not cid_digits:
            continue
        accounts.append(
            AdsAccount(
                name=_safe_text(item.get("name")) or cid,
                cid=cid,
                cid_digits=cid_digits,
                is_manager=bool(item.get("is_manager")),
                raw_text=_safe_text(item.get("raw_text")),
            )
        )
    return accounts


def _run_login_and_crawl_background(
    *,
    browser: str,
    headless: bool,
    target_map_path: str,
    progress_cb,
) -> None:
    accounts, browser_used, worker_log_file = login_and_crawl_accounts_in_subprocess(
        browser=browser,
        headless=headless,
        target_map_path=target_map_path,
        progress_cb=progress_cb,
    )
    progress_cb(
        {
            "type": "login_accounts_ready",
            "accounts": [_account_to_payload(account) for account in accounts],
            "browser_used": browser_used,
            "log_file": worker_log_file,
        }
    )
    progress_cb({"type": "scan_results", "rows": []})
    progress_cb(
        {
            "type": "run_completed",
            "run_status": "Ready",
            "message": f"Account crawl completed: {len(accounts)}",
        }
    )
    if worker_log_file:
        progress_cb(
            {
                "type": "run_warning",
                "message": f"로그 파일 확인: {worker_log_file}",
            }
        )


def _apply_ready_login_result(snapshot: dict[str, Any]) -> None:
    run_id = _safe_text(snapshot.get("run_id"))
    if not run_id:
        return
    if _safe_text(st.session_state.get("applied_login_result_run_id")) == run_id:
        return
    payloads = snapshot.get("login_accounts_payload")
    if not isinstance(payloads, list) or not payloads:
        return

    st.session_state["accounts"] = _accounts_from_payload(payloads)
    st.session_state["selected_cids"] = set()
    st.session_state["scan_results"] = {}
    st.session_state["matching_ready"] = False
    st.session_state["started"] = True
    st.session_state["opened_output_for_run"] = ""
    st.session_state["validated_output_count_run"] = ""
    st.session_state["expected_output_count"] = 0
    st.session_state["applied_login_result_run_id"] = run_id


def _login_progress_helper_text(snapshot: dict[str, Any]) -> str:
    if (not bool(snapshot.get("is_running"))) or (_safe_text(snapshot.get("run_status")).lower() != "preparing"):
        return ""

    login_status = _safe_text(snapshot.get("login_status")).lower()
    if login_status == "crawling accounts":
        return "계정 크롤링중입니다."
    return "로그인 대기중입니다."


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
    for key in ("browser", "base_parent_dir", "output_dir", "downloads_dir", "logs_dir"):
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
        "base_parent_dir": _serialize_base_parent_dir_for_settings(
            _safe_text(st.session_state.get("base_parent_dir")) or str(DEFAULT_USER_PARENT_DIR)
        ),
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
    st.session_state.setdefault("applied_login_result_run_id", "")
    st.session_state.setdefault("enable_report_download", True)
    st.session_state.setdefault("enable_action_log_download", True)
    st.session_state.setdefault("run_enable_report_download", True)
    st.session_state.setdefault("run_enable_action_log_download", True)
    st.session_state.setdefault("opened_output_for_run", "")
    st.session_state.setdefault("validated_output_count_run", "")
    st.session_state.setdefault("expected_output_count", 0)
    st.session_state.setdefault("run_output_root_dir", "")
    st.session_state.setdefault("run_raw_dir", "")
    st.session_state.setdefault("run_trace_dir", "")
    st.session_state.setdefault("run_output_dir", "")
    st.session_state.setdefault("run_downloads_dir", "")
    st.session_state.setdefault("run_logs_dir", "")
    st.session_state.setdefault("run_action_log_dir", "")
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
        "base_parent_dir",
        runtime_settings.get("base_parent_dir") or str(DEFAULT_USER_PARENT_DIR),
    )

    is_valid, normalized_or_message = _validate_runtime_path(
        st.session_state.get("base_parent_dir"),
        check_writable=False,
    )
    if not is_valid:
        normalized = runtime_settings.get("base_parent_dir") or str(DEFAULT_USER_PARENT_DIR)
        st.session_state["_runtime_settings_needs_heal"] = True
        st.session_state["_runtime_path_error"] = INVALID_RUNTIME_PATH_MESSAGE
    else:
        normalized = normalized_or_message
    st.session_state["base_parent_dir"] = normalized
    st.session_state["_runtime_valid_base_parent_dir"] = normalized
    st.session_state.setdefault(BASE_PARENT_INPUT_KEY, normalized)

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
        st.text_input("저장 부모 경로", key=BASE_PARENT_INPUT_KEY, on_change=_on_base_parent_dir_input_change)
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
    login_helper_text = _login_progress_helper_text(snapshot)

    cols = st.columns([4.3, 1.2], vertical_alignment="center")
    with cols[0]:
        st.markdown("<div class='ga-main-card-title'>Google Ads 로그인</div>", unsafe_allow_html=True)
        st.markdown(
            "<p class='ga-caption-muted'>Google Ads에 로그인하면 등록된 광고계정을 불러옵니다.</p>",
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
            width="stretch",
            key="main_start_btn",
        ):
            _handle_start()
            st.rerun()
        if login_helper_text:
            st.markdown(
                f"<p class='ga-caption-muted'>{login_helper_text}</p>",
                unsafe_allow_html=True,
            )


def _render_account_export_section(snapshot: dict[str, Any]) -> None:
    st.markdown("<div class='ga-main-card-title'>광고계정 선택</div>", unsafe_allow_html=True)
    st.markdown(
        (
            "<p class='ga-caption-muted'>선택한 계정의 Saved reports에서 액티비티를 감지해서 "
            "캠페인 데이터/액션 로그를 다운로드합니다.</p>"
        ),
        unsafe_allow_html=True,
    )
    _render_account_selector(snapshot)
    _render_execution_options(snapshot)
    _render_proceed_export(snapshot)


def _prepare_run_directories() -> dict[str, str]:
    run_date = dt.datetime.now().strftime("%Y%m%d")
    run_paths = _build_run_storage_paths(run_date=run_date)
    run_output_dir = run_paths["output_dir"]
    run_raw_dir = run_paths["raw_dir"]
    run_trace_dir = run_paths["trace_dir"]
    run_action_log_dir = run_paths["action_log_dir"]

    run_output_dir.mkdir(parents=True, exist_ok=True)
    run_raw_dir.mkdir(parents=True, exist_ok=True)
    run_trace_dir.mkdir(parents=True, exist_ok=True)

    st.session_state["run_output_root_dir"] = str(run_paths["output_root"])
    st.session_state["run_output_dir"] = str(run_output_dir)
    st.session_state["run_raw_dir"] = str(run_raw_dir)
    st.session_state["run_trace_dir"] = str(run_trace_dir)
    st.session_state["run_downloads_dir"] = str(run_raw_dir)
    st.session_state["run_logs_dir"] = str(run_trace_dir)
    st.session_state["run_action_log_dir"] = str(run_action_log_dir)
    st.session_state["run_date_folder"] = run_date

    return {
        "run_date": run_date,
        "output_dir": str(run_output_dir),
        "output_root_dir": str(run_paths["output_root"]),
        "raw_dir": str(run_raw_dir),
        "trace_dir": str(run_trace_dir),
        "action_log_dir": str(run_action_log_dir),
    }


def _apply_runtime_settings(
    *,
    output_dir_override: str = "",
    trace_dir_override: str = "",
) -> None:
    env_file = _safe_text(st.session_state.get("env_file"))
    if env_file:
        load_env_file(env_file)

    output_dir = _safe_text(output_dir_override) or str(_build_storage_roots()["output_root"])
    trace_dir = _safe_text(trace_dir_override) or str(_build_storage_roots()["trace_root"])

    _apply_runtime_directory_overrides(
        runtime_dir=_safe_text(st.session_state.get("runtime_dir")),
        output_dir=output_dir,
        logs_dir=trace_dir,
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
        output_dir_override=run_dirs["output_dir"],
        trace_dir_override=run_dirs["trace_dir"],
    )
    logger, log_path = setup_logger("google_ads_exporter.streamlit")
    st.session_state["accounts"] = []
    st.session_state["selected_cids"] = set()
    st.session_state["scan_results"] = {}
    st.session_state["matching_ready"] = False
    st.session_state["started"] = False
    st.session_state["opened_output_for_run"] = ""
    st.session_state["validated_output_count_run"] = ""
    st.session_state["expected_output_count"] = 0
    st.session_state["applied_login_result_run_id"] = ""
    store.push_event({"type": "scan_results", "rows": []})
    store.push_event(
        {
            "type": "run_started",
            "run_id": _now_run_id(),
            "log_file": str(log_path),
            "run_status": "Preparing",
        }
    )

    try:
        ok, message = start_export_execution(
            store=store,
            runner=_run_login_and_crawl_background,
            runner_kwargs={
                "browser": _safe_text(st.session_state.get("browser")) or "msedge",
                "headless": False,
                "target_map_path": _safe_text(st.session_state.get("target_map_path")),
            },
        )
        if not ok:
            store.push_event(
                {
                    "type": "run_warning",
                    "message": message,
                }
            )
    except Exception as exc:  # noqa: BLE001
        error_text = _exc_text(exc)
        logger.exception("start flow failed: %s", error_text)
        st.session_state["started"] = False
        store.push_event(
            {
                "type": "run_failed",
                "error": f"Start failed: {error_text}",
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
        width="stretch",
        hide_index=True,
        column_config={
            "export": st.column_config.CheckboxColumn("선택", default=False),
            "account_name": st.column_config.TextColumn("광고계정"),
            "cid": st.column_config.TextColumn("CID"),
            "is_manager": st.column_config.CheckboxColumn("매니저", disabled=True),
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
        st.session_state["run_action_log_dir"] = ""
        st.session_state["execution_store"].push_event({"type": "scan_results", "rows": []})

    st.session_state["selected_cids"] = new_selected
    st.caption(f"선택한 광고계정: {len(new_selected)}")


def _execution_modes_enabled() -> tuple[bool, bool]:
    return (
        bool(st.session_state.get("enable_report_download", True)),
        bool(st.session_state.get("enable_action_log_download", True)),
    )


def _render_execution_options(snapshot: dict[str, Any]) -> None:
    del snapshot
    st.markdown("<div class='ga-main-card-title'>실행 옵션</div>", unsafe_allow_html=True)
    cols = st.columns(2)
    with cols[0]:
        st.checkbox("캠페인 데이터 다운로드", key="enable_report_download")
    with cols[1]:
        st.checkbox("액션 로그 다운로드", key="enable_action_log_download")
    report_enabled, action_log_enabled = _execution_modes_enabled()
    if not report_enabled and not action_log_enabled:
        st.markdown(
            "<div class='ga-disabled-box'>최소 한 개의 실행 항목을 선택해야 합니다.</div>",
            unsafe_allow_html=True,
        )


def _render_proceed_export(snapshot: dict[str, Any]) -> None:
    report_enabled, action_log_enabled = _execution_modes_enabled()
    disabled = (
        bool(snapshot.get("is_running"))
        or (not st.session_state.get("started", False))
        or (len(_selected_accounts()) == 0)
        or (not report_enabled and not action_log_enabled)
    )
    if st.session_state.get("started", False) and len(_selected_accounts()) == 0:
        st.markdown(
            "<div class='ga-disabled-box'>광고계정을 선택하면 다운로드하기가 활성화됩니다.</div>",
            unsafe_allow_html=True,
        )
    if st.button(
        "다운로드하기",
        type="primary",
        width="stretch",
        disabled=disabled,
        key="proceed_export_btn",
    ):
        with st.spinner("매칭 후 실행을 시작합니다..."):
            _handle_start_export()
        st.rerun()


def _handle_start_export() -> None:
    store = st.session_state["execution_store"]
    if store.is_running():
        return

    report_enabled, action_log_enabled = _execution_modes_enabled()
    if not report_enabled and not action_log_enabled:
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
        output_dir_override=run_dirs["output_dir"],
        trace_dir_override=run_dirs["trace_dir"],
    )
    logger, log_path = setup_logger("google_ads_exporter.streamlit")

    st.session_state["expected_output_count"] = 0
    st.session_state["opened_output_for_run"] = ""
    st.session_state["validated_output_count_run"] = ""
    st.session_state["matching_ready"] = False
    st.session_state["run_enable_report_download"] = report_enabled
    st.session_state["run_enable_action_log_download"] = action_log_enabled

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
            "downloads_dir": _safe_path(run_dirs["raw_dir"]),
            "action_log_dir": _safe_path(run_dirs["action_log_dir"]),
            "enable_report_download": report_enabled,
            "enable_action_log_download": action_log_enabled,
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
    if key in {"not found", "not_found"}:
        return "not_found"
    if key in {"failed", "error"}:
        return "failed"
    if key in {"downloaded"}:
        return "completed"
    if key in {"completed", "excel_written"}:
        return "completed"
    if key in {"downloading", "exporting"}:
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


def _missing_columns_style_text(value: Any) -> str:
    return "color: #b91c1c; font-weight: 700;" if _safe_text(value) else ""


def _map_styler(styler, func, subset: list[str]):
    if hasattr(styler, "map"):
        return styler.map(func, subset=subset)
    if hasattr(styler, "applymap"):
        return styler.applymap(func, subset=subset)
    return styler


def _style_status_column(df: pd.DataFrame, status_column: str = "상태"):
    styler = df.style
    if status_column in df.columns:
        styler = _map_styler(styler, _status_style_text, subset=[status_column])
    return styler


def _render_bottom_section(snapshot: dict[str, Any]) -> None:
    run_report_enabled = bool(
        st.session_state.get("run_enable_report_download", st.session_state.get("enable_report_download", True))
    )
    run_action_log_enabled = bool(
        st.session_state.get("run_enable_action_log_download", st.session_state.get("enable_action_log_download", True))
    )
    st.markdown("#### 캠페인 데이터 다운로드")
    rows = snapshot.get("rows") or []
    if not run_report_enabled:
        st.markdown(
            "<div class='ga-disabled-box'>캠페인 데이터 다운로드를 켜면 캠페인 데이터 다운로드 상태가 표시됩니다.</div>",
            unsafe_allow_html=True,
        )
    elif rows:
        row_df = pd.DataFrame(
            [
                {
                    "account": row.account,
                    "cid": row.cid,
                    "activity": row.activity,
                    "target": row.target_display,
                    "status": _status_label_text(row.status),
                    "message": row.message,
                    "missing_columns": row.missing_columns_text,
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
                "missing_columns": "누락 컬럼",
                "last_updated": "최종 갱신",
            }
        )
        styled_row_df = _style_status_column(row_df, "상태")
        styled_row_df = _map_styler(styled_row_df, _missing_columns_style_text, subset=["누락 컬럼"])
        st.dataframe(styled_row_df, width="stretch", hide_index=True)
    else:
        st.markdown(
            "<div class='ga-disabled-box'>액티비티 매칭 후 캠페인 데이터 다운로드 상태가 표시됩니다.</div>",
            unsafe_allow_html=True,
        )

    st.markdown("#### 캠페인 데이터 통합본 생성")
    account_stage_rows = snapshot.get("account_stage_rows") or []
    if not run_report_enabled:
        st.markdown(
            "<div class='ga-disabled-box'>캠페인 데이터 다운로드를 켜면 캠페인 데이터 통합본 생성 상태가 표시됩니다.</div>",
            unsafe_allow_html=True,
        )
    elif account_stage_rows:
        account_stage_df = pd.DataFrame(
            [
                {
                    "account": row.account,
                    "cid": row.cid,
                    "activity": row.activity,
                    "status": _status_label_text(row.status),
                    "processed_sheets": f"{int(getattr(row, 'processed_sheet_count', 0) or 0)}/"
                    f"{int(getattr(row, 'total_sheet_count', 0) or 0)}",
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
        st.dataframe(styled_account_stage_df, width="stretch", hide_index=True)
    else:
        st.markdown(
            "<div class='ga-disabled-box'>캠페인 데이터 다운로드 후 통합본 생성 상태가 표시됩니다.</div>",
            unsafe_allow_html=True,
        )

    st.markdown("#### 액션 로그 다운로드")
    action_log_rows = snapshot.get("action_log_rows") or []
    if not run_action_log_enabled:
        st.markdown(
            "<div class='ga-disabled-box'>액션 로그 다운로드를 켜면 액션 로그 다운로드 상태가 표시됩니다.</div>",
            unsafe_allow_html=True,
        )
    elif action_log_rows:
        action_log_df = pd.DataFrame(
            [
                {
                    "account": row.account,
                    "cid": row.cid,
                    "activity": row.activity,
                    "status": _status_label_text(row.status),
                    "message": row.message,
                    "updated_at": row.updated_at,
                }
                for row in action_log_rows
                if isinstance(row, ActionLogRow)
            ]
        ).rename(
            columns={
                "account": "계정",
                "cid": "CID",
                "activity": "액티비티",
                "status": "상태",
                "message": "메시지",
                "updated_at": "시간",
            }
        )
        action_log_df = action_log_df.sort_values(by=["시간"], ascending=False)
        styled_action_log_df = _style_status_column(action_log_df, "상태")
        st.dataframe(styled_action_log_df, width="stretch", hide_index=True)
    else:
        st.markdown(
            "<div class='ga-disabled-box'>액티비티 매칭 후 액션 로그 다운로드 상태가 표시됩니다.</div>",
            unsafe_allow_html=True,
        )


def _validate_output_count(snapshot: dict[str, Any]) -> None:
    run_id = _safe_text(snapshot.get("run_id"))
    run_status = _safe_text(snapshot.get("run_status"))
    if not run_id or run_status not in {"Completed", "Completed (With Failures)"}:
        return

    if not bool(st.session_state.get("run_enable_report_download", True)):
        st.session_state["validated_output_count_run"] = run_id
        st.session_state["expected_output_count"] = 0
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

    workbook_outputs = snapshot.get("outputs") or []
    action_log_outputs = snapshot.get("action_log_outputs") or []
    if (not workbook_outputs) and (not action_log_outputs):
        return

    if _safe_text(st.session_state.get("opened_output_for_run")) == run_id:
        return

    output_root_dir = _safe_text(st.session_state.get("run_output_root_dir"))
    if not output_root_dir:
        output_root_dir = str(_build_storage_roots()["output_root"])

    try:
        subprocess.Popen(["explorer", output_root_dir])  # noqa: S603
    except Exception:
        fallback_dir = _safe_text(st.session_state.get("run_output_dir")) or output_root_dir
        if fallback_dir:
            try:
                subprocess.Popen(["explorer", fallback_dir])  # noqa: S603
            except Exception:
                pass

    st.session_state["opened_output_for_run"] = run_id


if __name__ == "__main__":
    run_streamlit_app()
