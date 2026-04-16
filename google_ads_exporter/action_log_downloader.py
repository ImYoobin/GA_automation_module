"""Google Ads Change history action log download automation."""

from __future__ import annotations

import csv
import datetime as dt
import io
import re
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from playwright.sync_api import Locator, Page
else:
    Locator = Any
    Page = Any

from .config import SHORT_WAIT_MS
from .models import ActionLogResult, AdsAccount
from .report_editor import account_selector_visible, assert_current_account_context, click_account_in_selector
from .utils import sanitize_filename

CHANGE_HISTORY_URL = "https://ads.google.com/aw/changehistory"

WORKSPACE_FILTER_BAR_SELECTOR = "workspace-filter filter-bar[role='group']"
FILTER_CHIP_SELECTOR = f"{WORKSPACE_FILTER_BAR_SELECTOR} material-chip.predicateEditorChip"
FILTER_CHIP_BUTTON_SELECTOR = ".content[role='button']"
FILTER_DELETE_BUTTON_SELECTOR = ".delete-button[role='button']"
FILTER_LISTBOX_SELECTOR = "material-list[role='listbox'], material-select.material-select[role='listbox']"
FILTER_OPTION_SELECTOR = "material-select-item[role='option'], material-select-dropdown-item[role='option']"
ADD_FILTER_INPUT_SELECTOR = f"{WORKSPACE_FILTER_BAR_SELECTOR} input.search-box[role='combobox']"
ADD_FILTER_MENU_SELECTOR = "material-list.suggestion-list[role='menu']"
ADD_FILTER_MENU_ITEM_SELECTOR = "material-select-item[role='menuitem']"
POPUP_DIALOG_SELECTOR = ".popup-wrapper.visible[role='dialog']"
OPERATOR_BUTTON_SELECTOR = "material-dropdown-select.operator dropdown-button div[role='button']"
OPERATOR_LISTBOX_SELECTOR = "material-list[role='listbox'], material-select.material-select[role='listbox']"
OPERATOR_OPTION_SELECTOR = "material-select-dropdown-item[role='option']"
FILTER_VALUE_SELECTOR = "filter-editor-string textarea, textarea.textarea.input-area"
FILTER_APPLY_BUTTON_SELECTOR = "div.footer material-button[role='button'], div.footer material-button"
LAST_30_DAYS_BUTTON_SELECTOR = "material-button.go-to-last-30days-button"
ALL_CHANGES_CHIP_SELECTOR = "change-type-filter-bar material-chip[role='option']"
DOWNLOAD_BUTTON_SELECTOR = "material-menu.report-download-menu-item material-button[role='button'][aria-haspopup='menu'], material-menu.report-download-menu-item material-button"
DOWNLOAD_MENU_ITEM_SELECTOR = "[role='menu'] material-select-item[role='menuitem']"
EXACT_CSV_MENU_ITEM_SELECTOR = "material-select-item[role='menuitem'][aria-label='.csv']"
LOADING_OVERLAY_SELECTOR = "ipl-progress-indicator"
LOADING_CONTAINER_SELECTOR = "progress-indicator"
CHIPS_BUSY_SELECTOR = "material-chips[aria-busy]"

CHANGE_HISTORY_READY_TIMEOUT_MS = 20_000
CHANGE_HISTORY_DOWNLOAD_TIMEOUT_MS = 240_000
UI_WAIT_TIMEOUT_MS = 15_000
CHANGE_HISTORY_INITIAL_IDLE_TIMEOUT_MS = 45_000
UI_IDLE_TIMEOUT_MS = 30_000
UI_CLICK_TIMEOUT_MS = 15_000
POLL_INTERVAL_MS = 400

CAMPAIGN_NAME_REGEX = re.compile(r"Campaign name|\ucea0\ud398\uc778 \uc774\ub984", re.IGNORECASE)
STARTS_WITH_REGEX = re.compile(r"starts with|\uc2dc\uc791", re.IGNORECASE)
CSV_REGEX = re.compile(r"\.csv", re.IGNORECASE)

ActionLogProgressCallback = Callable[[AdsAccount, str, str, str, str], None]
ACTION_LOG_OUTPUT_HEADERS = ("User / Date & Time", "Tool", "Change", "Campaign", "Ad group")


def build_action_log_run_dir(output_base_dir: Path, run_date: str | None = None) -> Path:
    effective_run_date = str(run_date or dt.datetime.now().strftime("%Y%m%d")).strip()
    return Path(output_base_dir).expanduser().resolve() / "action_log" / effective_run_date


def build_action_log_output_path(
    *,
    action_log_dir: Path,
    account: AdsAccount,
    activity_name: str,
    run_date: str | None = None,
) -> Path:
    effective_run_date = str(run_date or dt.datetime.now().strftime("%Y%m%d")).strip()
    activity_fragment = str(activity_name or "").strip() or "activity"
    filename = f"{effective_run_date}_{account.name}_{activity_fragment}_action_log.csv"
    return Path(action_log_dir).expanduser().resolve() / sanitize_filename(filename)


def build_action_log_raw_download_path(output_path: Path) -> Path:
    normalized_path = Path(output_path).expanduser().resolve()
    return normalized_path.with_name(f"{normalized_path.stem}.raw{normalized_path.suffix}")


def is_change_history_ready(page: Page) -> bool:
    current_url = str(getattr(page, "url", "") or "").lower()
    if "/aw/changehistory" not in current_url:
        return False
    for selector in (
        FILTER_CHIP_SELECTOR,
        LAST_30_DAYS_BUTTON_SELECTOR,
        ALL_CHANGES_CHIP_SELECTOR,
        DOWNLOAD_BUTTON_SELECTOR,
    ):
        try:
            if page.locator(selector).count() > 0:
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def download_action_logs_for_account(
    *,
    page: Page,
    account: AdsAccount,
    activities: Iterable[tuple[str, str]],
    action_log_dir: Path,
    logger=None,
    progress_callback: ActionLogProgressCallback | None = None,
) -> list[ActionLogResult]:
    results: list[ActionLogResult] = []
    normalized_dir = Path(action_log_dir).expanduser().resolve()

    for activity_key, activity_name in activities:
        effective_activity_name = str(activity_name or activity_key or "").strip() or activity_key or "activity"
        _emit_progress(
            progress_callback,
            account=account,
            activity_name=effective_activity_name,
            activity_key=activity_key,
            status="Exporting",
            message="\uc561\uc158\ub85c\uadf8 \ub2e4\uc6b4\ub85c\ub4dc\uc911",
        )
        try:
            ensure_change_history_ready(page=page, account=account, logger=logger)
            output_path = build_action_log_output_path(
                action_log_dir=normalized_dir,
                account=account,
                activity_name=effective_activity_name,
            )
            saved_path = download_action_log_for_activity(
                page=page,
                account=account,
                activity_name=effective_activity_name,
                activity_key=activity_key,
                output_path=output_path,
                logger=logger,
            )
            result = ActionLogResult(
                activity_name=effective_activity_name,
                activity_key=activity_key,
                success=True,
                filename=saved_path.name,
            )
            _emit_progress(
                progress_callback,
                account=account,
                activity_name=effective_activity_name,
                activity_key=activity_key,
                status="Completed",
                message=f"\uc561\uc158\ub85c\uadf8 \uc800\uc7a5\uc644\ub8cc:{saved_path.name}",
            )
        except Exception as exc:  # noqa: BLE001
            reason = _exc_text(exc)
            if logger:
                logger.exception(
                    "action log download failed | account=%s(%s) | activity=%s | reason=%s",
                    account.name,
                    account.cid,
                    effective_activity_name,
                    reason,
                )
            result = ActionLogResult(
                activity_name=effective_activity_name,
                activity_key=activity_key,
                success=False,
                reason=reason,
            )
            _emit_progress(
                progress_callback,
                account=account,
                activity_name=effective_activity_name,
                activity_key=activity_key,
                status="Failed",
                message=reason,
            )
        results.append(result)

    return results


def ensure_change_history_ready(*, page: Page, account: AdsAccount, logger=None) -> None:
    last_error: RuntimeError | None = None
    for attempt in range(3):
        try:
            page.goto(CHANGE_HISTORY_URL, wait_until="domcontentloaded")
            page.wait_for_timeout(SHORT_WAIT_MS)
            if account_selector_visible(page):
                switched = click_account_in_selector(page, account, logger=logger, ready_check=is_change_history_ready)
                if not switched:
                    raise RuntimeError("change history account switch failed")
            if _wait_for_change_history_ready(page, timeout_ms=CHANGE_HISTORY_READY_TIMEOUT_MS):
                verified = assert_current_account_context(
                    page,
                    account,
                    ready_check=is_change_history_ready,
                    logger=logger,
                )
                if verified:
                    _wait_for_ui_idle(
                        page,
                        timeout_ms=CHANGE_HISTORY_INITIAL_IDLE_TIMEOUT_MS,
                        logger=logger,
                        reason="change_history_ready",
                    )
                    return
                raise RuntimeError("change history account verification failed")
        except Exception as exc:  # noqa: BLE001
            last_error = RuntimeError(_exc_text(exc))
            if logger:
                logger.warning(
                    "change history ready attempt failed | attempt=%s | account=%s(%s) | reason=%s",
                    attempt + 1,
                    account.name,
                    account.cid,
                    last_error,
                )
            page.wait_for_timeout(SHORT_WAIT_MS)

    raise last_error or RuntimeError("change history load failed")


def download_action_log_for_activity(
    *,
    page: Page,
    account: AdsAccount,
    activity_name: str,
    activity_key: str,
    output_path: Path,
    logger=None,
) -> Path:
    activity_prefix = str(activity_name or activity_key or "").strip()
    if not activity_prefix:
        raise RuntimeError("activity prefix missing")

    _wait_for_ui_idle(page, timeout_ms=UI_IDLE_TIMEOUT_MS, logger=logger, reason="before_action_log_filters")
    _set_status_filter_all(page, chip_index=0, logger=logger)
    _set_status_filter_all(page, chip_index=1, logger=logger)
    _remove_existing_campaign_name_filter(page, logger=logger)
    _add_campaign_name_filter(page, value=f"{activity_prefix}_", logger=logger)
    _set_last_30_days(page, logger=logger)
    _ensure_all_changes_selected(page, logger=logger)
    return _download_action_log_csv(page, output_path=output_path, logger=logger)


def _set_status_filter_all(page: Page, *, chip_index: int, logger=None) -> None:
    chips = page.locator(FILTER_CHIP_SELECTOR)
    if chips.count() <= chip_index:
        raise RuntimeError(f"status filter chip not found index={chip_index}")

    chip = chips.nth(chip_index).locator(FILTER_CHIP_BUTTON_SELECTOR).first
    _click_with_retry(chip, page=page, description=f"status filter chip[{chip_index}]", logger=logger)
    dialog = _wait_for_visible_dialog(page, timeout_ms=UI_WAIT_TIMEOUT_MS)
    listbox = _wait_for_visible_locator(dialog.locator(FILTER_LISTBOX_SELECTOR), timeout_ms=UI_WAIT_TIMEOUT_MS)
    options = listbox.locator(FILTER_OPTION_SELECTOR)
    if options.count() == 0:
        raise RuntimeError("status filter options not found")
    _click_with_retry(options.nth(0), page=page, description=f"status filter option[{chip_index}].all", logger=logger)
    _wait_for_locator_gone(dialog, timeout_ms=UI_WAIT_TIMEOUT_MS)
    _wait_for_ui_idle(page, timeout_ms=UI_IDLE_TIMEOUT_MS, logger=logger, reason=f"after_status_filter_{chip_index}")


def _remove_existing_campaign_name_filter(page: Page, logger=None) -> bool:
    chips = page.locator(FILTER_CHIP_SELECTOR)
    if chips.count() < 3:
        return False
    target_chip = chips.nth(2)
    delete_button = target_chip.locator(FILTER_DELETE_BUTTON_SELECTOR).first
    if delete_button.count() == 0:
        return False
    _click_with_retry(delete_button, page=page, description="campaign-name filter delete", logger=logger)
    _wait_for_ui_idle(page, timeout_ms=UI_IDLE_TIMEOUT_MS, logger=logger, reason="after_campaign_name_filter_delete")
    if logger:
        logger.info("existing campaign-name filter chip removed")
    return True


def _add_campaign_name_filter(page: Page, *, value: str, logger=None) -> None:
    add_filter_input = _wait_for_visible_locator(page.locator(ADD_FILTER_INPUT_SELECTOR), timeout_ms=UI_WAIT_TIMEOUT_MS)
    _click_with_retry(
        add_filter_input,
        page=page,
        description="add-filter input",
        logger=logger,
    )
    menu = _wait_for_visible_locator(page.locator(ADD_FILTER_MENU_SELECTOR), timeout_ms=UI_WAIT_TIMEOUT_MS)
    menu_items = menu.locator(ADD_FILTER_MENU_ITEM_SELECTOR)
    campaign_name_item = _find_locator_by_text(menu_items, CAMPAIGN_NAME_REGEX, visible_only=True)
    if campaign_name_item is None:
        raise RuntimeError("campaign-name filter suggestion not found")
    _click_with_retry(campaign_name_item, page=page, description="campaign-name filter suggestion", logger=logger)

    editor_root = _wait_for_visible_dialog(page, timeout_ms=UI_WAIT_TIMEOUT_MS)
    operator_button = _wait_for_visible_locator(
        editor_root.locator(OPERATOR_BUTTON_SELECTOR),
        timeout_ms=UI_WAIT_TIMEOUT_MS,
    )
    _click_with_retry(operator_button, page=page, description="filter operator dropdown", logger=logger)
    operator_listbox = _wait_for_visible_locator(page.locator(OPERATOR_LISTBOX_SELECTOR), timeout_ms=UI_WAIT_TIMEOUT_MS)
    operator_options = operator_listbox.locator(OPERATOR_OPTION_SELECTOR)
    starts_with_option = _find_locator_by_text(
        operator_options,
        STARTS_WITH_REGEX,
        preferred_index=5,
        visible_only=True,
    )
    if starts_with_option is None:
        raise RuntimeError("starts-with operator not found")
    _click_with_retry(starts_with_option, page=page, description="starts-with operator", logger=logger)
    _wait_for_locator_gone(operator_listbox, timeout_ms=UI_WAIT_TIMEOUT_MS)

    value_input = _wait_for_visible_locator(editor_root.locator(FILTER_VALUE_SELECTOR), timeout_ms=UI_WAIT_TIMEOUT_MS)
    _wait_for_ui_idle(page, timeout_ms=UI_IDLE_TIMEOUT_MS, logger=logger, reason="before_filter_value_input")
    value_input.fill("")
    value_input.type(str(value), delay=20)

    apply_button = _wait_for_visible_locator(
        editor_root.locator(FILTER_APPLY_BUTTON_SELECTOR),
        timeout_ms=UI_WAIT_TIMEOUT_MS,
    )
    _click_with_retry(apply_button, page=page, description="campaign-name apply button", logger=logger)
    _wait_for_locator_gone(editor_root, timeout_ms=UI_WAIT_TIMEOUT_MS)
    _wait_for_ui_idle(page, timeout_ms=UI_IDLE_TIMEOUT_MS, logger=logger, reason="after_campaign_name_apply")
    if logger:
        logger.info("campaign-name filter applied | value=%s", value)


def _set_last_30_days(page: Page, logger=None) -> None:
    button = _wait_for_locator(page.locator(LAST_30_DAYS_BUTTON_SELECTOR), timeout_ms=UI_WAIT_TIMEOUT_MS)
    _click_with_retry(button, page=page, description="last-30-days button", logger=logger)
    page.wait_for_timeout(POLL_INTERVAL_MS)
    _wait_for_ui_idle(page, timeout_ms=UI_IDLE_TIMEOUT_MS, logger=logger, reason="after_last_30_days")
    if logger:
        logger.info("change history date range set to last 30 days")


def _ensure_all_changes_selected(page: Page, logger=None) -> None:
    chips = page.locator(ALL_CHANGES_CHIP_SELECTOR)
    if chips.count() == 0:
        raise RuntimeError("all-changes chip not found")

    chip = chips.nth(0)
    selected = str(chip.get_attribute("aria-selected") or "").strip().lower() == "true"
    if not selected:
        class_name = str(chip.get_attribute("class") or "").strip()
        selected = "mdc-chip--selected" in class_name or "selected" in class_name
    if selected:
        return

    _click_with_retry(chip, page=page, description="all-changes chip", logger=logger)
    page.wait_for_timeout(POLL_INTERVAL_MS)
    _wait_for_ui_idle(page, timeout_ms=UI_IDLE_TIMEOUT_MS, logger=logger, reason="after_all_changes")
    if logger:
        logger.info("all-changes chip reselected")


def _download_action_log_csv(page: Page, *, output_path: Path, logger=None) -> Path:
    _wait_for_ui_idle(page, timeout_ms=UI_IDLE_TIMEOUT_MS, logger=logger, reason="before_download_button")
    button = _find_download_button(page)
    if button is None:
        raise RuntimeError("change-history download button not found")
    _click_with_retry(button, page=page, description="change-history download button", logger=logger)

    csv_item = _first_visible_locator(page.locator(EXACT_CSV_MENU_ITEM_SELECTOR))
    if csv_item is None:
        menu_items = _wait_for_locator(page.locator(DOWNLOAD_MENU_ITEM_SELECTOR), timeout_ms=UI_WAIT_TIMEOUT_MS)
        csv_item = _first_visible_locator(page.locator(EXACT_CSV_MENU_ITEM_SELECTOR))
        if csv_item is None:
            csv_item = _find_locator_by_text(menu_items, CSV_REGEX, preferred_index=1, visible_only=True)
    if csv_item is None:
        raise RuntimeError("change-history csv menu item not found")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        output_path.unlink()
    raw_download_path = build_action_log_raw_download_path(output_path)
    if raw_download_path.exists():
        raw_download_path.unlink()

    with page.expect_download(timeout=CHANGE_HISTORY_DOWNLOAD_TIMEOUT_MS) as download_info:
        _click_with_retry(csv_item, page=page, description="change-history csv menu item", logger=logger)
    download_info.value.save_as(str(raw_download_path))
    _transform_action_log_csv(raw_download_path=raw_download_path, output_path=output_path)
    try:
        raw_download_path.unlink()
    except FileNotFoundError:
        pass
    if logger:
        logger.info("action log saved | path=%s", output_path)
    return output_path


def _find_download_button(page: Page) -> Locator | None:
    candidates = [
        page.locator(DOWNLOAD_BUTTON_SELECTOR),
        page.locator("material-button[aria-label='Download'][role='button']"),
        page.locator("[aria-label='Download'][role='button']"),
        page.get_by_role("button", name=re.compile(r"Download|\ub2e4\uc6b4\ub85c\ub4dc", re.IGNORECASE)),
    ]
    for locator in candidates:
        try:
            visible_candidate = _first_visible_locator(locator)
            if visible_candidate is not None:
                return visible_candidate
            if locator.count() > 0:
                return locator.first
        except Exception:  # noqa: BLE001
            continue
    return None


def _wait_for_change_history_ready(page: Page, *, timeout_ms: int) -> bool:
    elapsed = 0
    while elapsed <= timeout_ms:
        if is_change_history_ready(page):
            return True
        page.wait_for_timeout(POLL_INTERVAL_MS)
        elapsed += POLL_INTERVAL_MS
    return False


def _click_with_retry(
    locator: Locator,
    *,
    page: Page,
    description: str,
    timeout_ms: int = UI_CLICK_TIMEOUT_MS,
    logger=None,
) -> None:
    last_error: Exception | None = None
    for attempt in range(3):
        _wait_for_ui_idle(page, timeout_ms=UI_IDLE_TIMEOUT_MS, logger=logger, reason=description)
        try:
            locator.click(timeout=timeout_ms)
            return
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            reason = _exc_text(exc)
            if not _is_retryable_click_error(reason):
                raise
            if logger:
                logger.info(
                    "click retry due to blocking overlay | target=%s | attempt=%s | reason=%s",
                    description,
                    attempt + 1,
                    reason,
                )
            page.wait_for_timeout(POLL_INTERVAL_MS)
    raise RuntimeError(f"{description} click failed after waiting for idle: {_exc_text(last_error)}") from last_error


def _wait_for_ui_idle(page: Page, *, timeout_ms: int, logger=None, reason: str = "") -> None:
    effective_timeout_ms = max(int(timeout_ms), 0)
    if effective_timeout_ms > 0:
        try:
            page.wait_for_function(
                r"""({ containerSelector, chipsSelector }) => {
                    const container = document.querySelector(containerSelector);
                    const chips = document.querySelector(chipsSelector);
                    const html = container ? String(container.innerHTML || '').trim() : '';
                    const normalizedHtml = html.replace(/\\s+/g, '');
                    const role = container ? String(container.getAttribute('role') || '').trim().toLowerCase() : '';
                    const progressNode = container
                        ? container.querySelector('material-progress, [role="progressbar"], .progress-container')
                        : null;
                    const progressStyle = progressNode ? window.getComputedStyle(progressNode) : null;
                    const progressRect = progressNode ? progressNode.getBoundingClientRect() : null;
                    const visibleProgress = Boolean(
                        progressNode &&
                        progressStyle &&
                        progressStyle.display !== 'none' &&
                        progressStyle.visibility !== 'hidden' &&
                        Number.parseFloat(progressStyle.opacity || '1') > 0 &&
                        progressRect &&
                        progressRect.width > 0 &&
                        progressRect.height > 0
                    );
                    const shellOnly = Boolean(
                        container &&
                        container.children.length === 1 &&
                        normalizedHtml === '<ipl-progress-indicator></ipl-progress-indicator>'
                    );
                    const containerEmpty = (
                        !container ||
                        container.children.length === 0 ||
                        html === '' ||
                        html === '<!---->' ||
                        shellOnly
                    );
                    const chipsBusy = chips ? String(chips.getAttribute('aria-busy') || '').trim().toLowerCase() : 'false';
                    const idleContainer = containerEmpty || (role === 'none' && !visibleProgress);
                    return idleContainer && !visibleProgress && chipsBusy !== 'true';
                }""",
                {
                    "containerSelector": LOADING_CONTAINER_SELECTOR,
                    "chipsSelector": CHIPS_BUSY_SELECTOR,
                },
                timeout=effective_timeout_ms,
            )
            return
        except Exception:  # noqa: BLE001
            pass

    last_snapshot = _capture_loading_state(page)
    if not _is_loading_state_blocking(last_snapshot):
        return

    if logger:
        logger.warning(
            "change history loading wait timeout | timeout_ms=%s | reason=%s | url=%s | blocking=%s | shell_only=%s | visible_progress=%s | progress_role=%s | progress_container_empty=%s | overlay_present=%s | overlay_visible=%s | overlay_pointer_events=%s | progress_exists=%s | progress_children=%s | progress_html=%s | progress_pointer_events=%s | progress_box=%s | chips_busy=%s",
            effective_timeout_ms,
            reason,
            getattr(page, "url", ""),
            last_snapshot.get("blocking"),
            last_snapshot.get("shell_only"),
            last_snapshot.get("visible_progress"),
            last_snapshot.get("progress_role"),
            last_snapshot.get("progress_container_empty"),
            last_snapshot.get("overlay_present"),
            last_snapshot.get("overlay_visible"),
            last_snapshot.get("overlay_pointer_events"),
            last_snapshot.get("progress_exists"),
            last_snapshot.get("progress_child_count"),
            last_snapshot.get("progress_html"),
            last_snapshot.get("progress_pointer_events"),
            last_snapshot.get("progress_bounding_box"),
            last_snapshot.get("chips_busy"),
        )
    raise RuntimeError(f"change history loading overlay did not clear within {effective_timeout_ms}ms")


def _transform_action_log_csv(*, raw_download_path: Path, output_path: Path) -> None:
    normalized_raw_path = Path(raw_download_path).expanduser().resolve()
    normalized_output_path = Path(output_path).expanduser().resolve()
    encoding = _detect_action_log_csv_encoding(normalized_raw_path)
    preview_text = _read_action_log_csv_preview(path=normalized_raw_path, encoding=encoding)
    delimiter = _detect_action_log_csv_delimiter(preview_text)
    rows = _parse_action_log_csv_rows(text=preview_text, path=normalized_raw_path, encoding=encoding, delimiter=delimiter)

    normalized_output_path.parent.mkdir(parents=True, exist_ok=True)
    with normalized_output_path.open("w", encoding="utf-8-sig", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=list(ACTION_LOG_OUTPUT_HEADERS))
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _parse_action_log_csv_rows(*, text: str, path: Path, encoding: str, delimiter: str) -> list[dict[str, str]]:
    header_row_index = _detect_action_log_header_row_index(text=text, delimiter=delimiter)
    rows: list[dict[str, str]] = []
    with path.open("r", encoding=encoding, errors="replace", newline="") as fp:
        reader = csv.reader(fp, delimiter=delimiter)
        for _ in range(max(0, header_row_index)):
            next(reader, None)

        header_row = next(reader, [])
        headers = [str(name or "").strip() for name in header_row]
        source_lookup = {_normalize_action_log_header(header): idx for idx, header in enumerate(headers)}
        required_tokens = ("datetime", "user", "campaign", "adgroup", "changes")
        missing_tokens = [token for token in required_tokens if token not in source_lookup]
        if missing_tokens:
            raise RuntimeError(f"action log csv header missing required columns: {', '.join(missing_tokens)}")

        for raw_row in reader:
            if not raw_row:
                continue
            row_dict = _build_action_log_row_dict(headers=headers, raw_row=raw_row)
            if _is_action_log_row_blank(row_dict):
                continue
            rows.append(
                {
                    "User / Date & Time": _combine_action_log_user_datetime(
                        user=_get_action_log_value(row_dict, "user"),
                        date_time=_get_action_log_value(row_dict, "datetime"),
                    ),
                    "Tool": "",
                    "Change": _get_action_log_value(row_dict, "changes"),
                    "Campaign": _get_action_log_value(row_dict, "campaign"),
                    "Ad group": _get_action_log_value(row_dict, "adgroup"),
                }
            )
    return rows


def _detect_action_log_csv_encoding(path: Path) -> str:
    with path.open("rb") as fp:
        raw = fp.read(4096)
    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    if raw.startswith(b"\xff\xfe"):
        return "utf-16"
    if raw.startswith(b"\xfe\xff"):
        return "utf-16"
    if b"\x00" in raw:
        return "utf-16"

    for encoding in ("utf-8-sig", "utf-8", "cp949"):
        try:
            raw.decode(encoding)
            return encoding
        except Exception:  # noqa: BLE001
            continue
    return "utf-8-sig"


def _read_action_log_csv_preview(*, path: Path, encoding: str, max_lines: int = 80, max_chars: int = 65536) -> str:
    lines: list[str] = []
    total_chars = 0
    with path.open("r", encoding=encoding, errors="replace", newline="") as fp:
        for _ in range(max_lines):
            line = fp.readline()
            if not line:
                break
            lines.append(line)
            total_chars += len(line)
            if total_chars >= max_chars:
                break
    return "".join(lines)


def _detect_action_log_csv_delimiter(text: str) -> str:
    sample = "\n".join(text.splitlines()[:60])
    if not sample.strip():
        return ","
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters="\t,;|")
        return str(dialect.delimiter)
    except Exception:  # noqa: BLE001
        counts: dict[str, int] = {}
        for delimiter in ("\t", ",", ";", "|"):
            counts[delimiter] = sum(line.count(delimiter) for line in sample.splitlines()[:20])
        best = max(counts.items(), key=lambda item: item[1])[0]
        return best if counts[best] > 0 else ","


def _detect_action_log_header_row_index(*, text: str, delimiter: str) -> int:
    best_index = 0
    best_score = -1
    with io.StringIO(text) as fp:
        reader = csv.reader(fp, delimiter=delimiter)
        for row_idx, row in enumerate(reader):
            if row_idx >= 40:
                break
            score = _score_action_log_header_candidate([str(cell or "").strip() for cell in row])
            if score > best_score:
                best_index = row_idx
                best_score = score
    return best_index


def _score_action_log_header_candidate(row: list[str]) -> int:
    if len([cell for cell in row if cell]) < 3:
        return -1
    tokens = {_normalize_action_log_header(cell) for cell in row if cell}
    anchors = ("datetime", "user", "campaign", "adgroup", "changes")
    return sum(30 for anchor in anchors if anchor in tokens) + len(tokens)


def _build_action_log_row_dict(*, headers: list[str], raw_row: list[str]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for idx, header in enumerate(headers):
        normalized[str(header or "").strip()] = str(raw_row[idx] if idx < len(raw_row) else "").strip()
    return normalized


def _is_action_log_row_blank(row: dict[str, str]) -> bool:
    return not any(str(value or "").strip() for value in row.values())


def _normalize_action_log_header(value: str) -> str:
    header = "".join(char for char in str(value or "").strip().lower() if char.isalnum())
    aliases = {
        "dateandtime": "datetime",
        "datetime": "datetime",
        "date/time": "datetime",
        "user": "user",
        "campaign": "campaign",
        "adgroup": "adgroup",
        "changes": "changes",
        "change": "changes",
        "tool": "tool",
    }
    return aliases.get(header, header)


def _get_action_log_value(row: dict[str, str], token: str) -> str:
    normalized_token = _normalize_action_log_header(token)
    for key, value in row.items():
        if _normalize_action_log_header(key) == normalized_token:
            return str(value or "").strip()
    return ""


def _combine_action_log_user_datetime(*, user: str, date_time: str) -> str:
    parts = [str(user or "").strip(), str(date_time or "").strip()]
    return "\n".join(part for part in parts if part)


def _capture_loading_state(page: Page) -> dict[str, Any]:
    try:
        result = page.evaluate(
            r"""({ overlaySelector, containerSelector, chipsSelector }) => {
                const overlay = document.querySelector(overlaySelector);
                const container = document.querySelector(containerSelector);
                const chips = document.querySelector(chipsSelector);
                const busyNode = (
                    (container && container.querySelector('material-progress, [role="progressbar"], .progress-container'))
                );
                const html = container ? (container.innerHTML || '').trim() : '';
                const normalizedHtml = html.replace(/\s+/g, '');
                const trimmedHtml = html.length > 180 ? html.slice(0, 180) : html;
                const role = container ? (container.getAttribute('role') || '') : '';
                const overlayStyle = overlay ? window.getComputedStyle(overlay) : null;
                const progressStyle = busyNode ? window.getComputedStyle(busyNode) : null;
                const overlayRect = overlay ? overlay.getBoundingClientRect() : null;
                const progressRect = busyNode ? busyNode.getBoundingClientRect() : null;
                const shellOnly = Boolean(
                    container &&
                    container.children.length === 1 &&
                    normalizedHtml === '<ipl-progress-indicator></ipl-progress-indicator>'
                );
                const progressContainerEmpty = (
                    !container ||
                    container.children.length === 0 ||
                    html === '' ||
                    html === '<!---->' ||
                    shellOnly
                );
                const visibleProgress = Boolean(
                    busyNode &&
                    progressStyle &&
                    progressStyle.display !== 'none' &&
                    progressStyle.visibility !== 'hidden' &&
                    Number.parseFloat(progressStyle.opacity || '1') > 0 &&
                    progressRect &&
                    progressRect.width > 0 &&
                    progressRect.height > 0
                );
                const chipsBusy = chips ? (chips.getAttribute('aria-busy') || '') : '';
                const blocking = (!progressContainerEmpty && visibleProgress) || chipsBusy === 'true';
                return {
                    overlay_present: Boolean(overlay),
                    overlay_visible: Boolean(
                        overlay &&
                        overlayStyle &&
                        overlayStyle.display !== 'none' &&
                        overlayStyle.visibility !== 'hidden' &&
                        overlayRect &&
                        overlayRect.width > 0 &&
                        overlayRect.height > 0
                    ),
                    overlay_pointer_events: overlayStyle ? (overlayStyle.pointerEvents || '') : '',
                    progress_exists: Boolean(container),
                    progress_role: role,
                    progress_container_empty: progressContainerEmpty,
                    shell_only: shellOnly,
                    progress_child_count: container ? container.children.length : 0,
                    progress_html: trimmedHtml,
                    visible_progress: visibleProgress,
                    progress_pointer_events: progressStyle ? (progressStyle.pointerEvents || '') : '',
                    progress_bounding_box: progressRect
                        ? `${Math.round(progressRect.x)},${Math.round(progressRect.y)},${Math.round(progressRect.width)},${Math.round(progressRect.height)}`
                        : '',
                    chips_busy: chipsBusy,
                    blocking: blocking,
                };
            }""",
            {
                "overlaySelector": LOADING_OVERLAY_SELECTOR,
                "containerSelector": LOADING_CONTAINER_SELECTOR,
                "chipsSelector": CHIPS_BUSY_SELECTOR,
            },
        )
        if isinstance(result, dict):
            return result
    except Exception:  # noqa: BLE001
        pass
    return {
        "overlay_present": None,
        "overlay_visible": None,
        "overlay_pointer_events": "",
        "progress_exists": None,
        "progress_role": "",
        "progress_container_empty": None,
        "shell_only": None,
        "progress_child_count": None,
        "progress_html": "",
        "visible_progress": None,
        "progress_pointer_events": "",
        "progress_bounding_box": "",
        "chips_busy": "",
        "blocking": True,
    }


def _is_loading_state_blocking(snapshot: dict[str, Any]) -> bool:
    progress_container_empty = snapshot.get("progress_container_empty")
    shell_only = snapshot.get("shell_only")
    visible_progress = snapshot.get("visible_progress")
    progress_role = str(snapshot.get("progress_role") or "").strip().lower()
    chips_busy = str(snapshot.get("chips_busy") or "").strip().lower() == "true"
    if (progress_container_empty is True or shell_only is True) and not chips_busy:
        return False
    if visible_progress is True:
        return True
    if chips_busy:
        return True
    if progress_role == "none" and not chips_busy:
        return False
    return False


def _is_retryable_click_error(reason: str) -> bool:
    normalized = str(reason or "").strip().lower()
    return any(
        token in normalized
        for token in (
            "intercepts pointer events",
            "another element would receive the click",
            "element is not attached to the dom",
            "element is outside of the viewport",
        )
    )


def _wait_for_locator(locator: Locator, *, timeout_ms: int) -> Locator:
    elapsed = 0
    while elapsed <= timeout_ms:
        try:
            if locator.count() > 0:
                return locator
        except Exception:  # noqa: BLE001
            pass
        time.sleep(POLL_INTERVAL_MS / 1000)
        elapsed += POLL_INTERVAL_MS
    raise RuntimeError("expected locator not found in time")


def _wait_for_visible_locator(locator: Locator, *, timeout_ms: int) -> Locator:
    elapsed = 0
    while elapsed <= timeout_ms:
        candidate = _first_visible_locator(locator)
        if candidate is not None:
            return candidate
        time.sleep(POLL_INTERVAL_MS / 1000)
        elapsed += POLL_INTERVAL_MS
    raise RuntimeError("expected visible locator not found in time")


def _wait_for_visible_dialog(page: Page, *, timeout_ms: int) -> Locator:
    return _wait_for_visible_locator(page.locator(POPUP_DIALOG_SELECTOR), timeout_ms=timeout_ms)


def _wait_for_locator_gone(locator: Locator, *, timeout_ms: int) -> None:
    elapsed = 0
    while elapsed <= timeout_ms:
        try:
            if locator.count() == 0:
                return
            if _first_visible_locator(locator) is None:
                return
        except Exception:  # noqa: BLE001
            return
        time.sleep(POLL_INTERVAL_MS / 1000)
        elapsed += POLL_INTERVAL_MS
    raise RuntimeError("expected locator to disappear in time")


def _first_visible_locator(locator: Locator) -> Locator | None:
    try:
        count = locator.count()
    except Exception:  # noqa: BLE001
        return None

    for idx in range(count):
        candidate = locator.nth(idx)
        if _locator_is_visible(candidate):
            return candidate
    return None

def _find_locator_by_text(
    locator: Locator,
    pattern: re.Pattern[str],
    *,
    preferred_index: int | None = None,
    visible_only: bool = False,
) -> Locator | None:
    try:
        count = locator.count()
    except Exception:  # noqa: BLE001
        return None

    if preferred_index is not None and count > preferred_index:
        candidate = locator.nth(preferred_index)
        if (not visible_only or _locator_is_visible(candidate)) and _locator_text_matches(candidate, pattern):
            return candidate

    for idx in range(count):
        candidate = locator.nth(idx)
        if visible_only and (not _locator_is_visible(candidate)):
            continue
        if _locator_text_matches(candidate, pattern):
            return candidate
    return None


def _locator_text_matches(locator: Locator, pattern: re.Pattern[str]) -> bool:
    text_parts = [
        _safe_inner_text(locator),
        str(locator.get_attribute("aria-label") or "").strip(),
        str(locator.get_attribute("textContent") or "").strip(),
    ]
    combined = "\n".join(part for part in text_parts if part)
    return bool(pattern.search(combined))


def _safe_inner_text(locator: Locator) -> str:
    try:
        return str(locator.inner_text(timeout=1_000) or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _locator_is_visible(locator: Locator) -> bool:
    try:
        return bool(locator.is_visible())
    except Exception:  # noqa: BLE001
        return False


def _emit_progress(
    callback: ActionLogProgressCallback | None,
    *,
    account: AdsAccount,
    activity_name: str,
    activity_key: str,
    status: str,
    message: str,
) -> None:
    if callback is None:
        return
    try:
        callback(account, activity_name, activity_key, status, message)
    except Exception:  # noqa: BLE001
        return


def _exc_text(exc: Exception) -> str:
    text = str(exc or "").strip()
    if text:
        return text
    rep = repr(exc)
    if rep:
        return rep
    return exc.__class__.__name__
