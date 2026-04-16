"""Report editor navigation and account context switching."""

from __future__ import annotations

from collections.abc import Callable

from playwright.sync_api import Page

from .config import (
    ACCOUNT_SWITCH_RETRIES,
    ACCOUNT_VERIFY_TIMEOUT_MS,
    REPORT_EDITOR_URL,
    SELECTOR_DETECT_TIMEOUT_MS,
    SHORT_WAIT_MS,
)
from .models import AdsAccount
from .utils import extract_cid

PageReadyCheck = Callable[[Page], bool]


def open_report_editor(page: Page, logger=None) -> bool:
    page.goto(REPORT_EDITOR_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(SHORT_WAIT_MS)
    seen = account_selector_visible(page)
    if logger:
        logger.info("Report editor opened. selector_seen=%s url=%s", seen, page.url)
    return seen


def click_account_in_reporteditor_selector(page: Page, account: AdsAccount, logger=None) -> bool:
    return click_account_in_selector(page, account, logger=logger, ready_check=is_report_editor_ready)


def click_account_in_selector(
    page: Page,
    account: AdsAccount,
    logger=None,
    ready_check: PageReadyCheck | None = None,
) -> bool:
    """
    Click the account inside selector surfaces by strict matching priority:
    1) exact CID
    2) exact name+CID
    3) exact unique name
    """
    for attempt in range(ACCOUNT_SWITCH_RETRIES + 1):
        candidates = _collect_selector_candidates(page)
        if logger:
            logger.info("selector candidates=%s attempt=%s", len(candidates), attempt + 1)
            for c in candidates:
                logger.info(
                    "selector row | name=%s | cid=%s | text=%s",
                    c["name"],
                    c["cid"],
                    c["raw_text"][:120],
                )

        target = _choose_selector_candidate(candidates, account)
        if not target:
            page.wait_for_timeout(SHORT_WAIT_MS)
            continue

        try:
            target["locator"].click(timeout=4000)
            page.wait_for_timeout(SHORT_WAIT_MS)
            if _wait_for_selector_close(page):
                if ready_check is None:
                    return True
                if assert_current_account_context(page, account, ready_check=ready_check, logger=logger):
                    return True
            # Selector may stay in DOM but account still switched.
            if assert_current_account_context(page, account, ready_check=ready_check, logger=logger):
                return True
        except Exception as exc:  # noqa: BLE001
            if logger:
                logger.warning("selector click failed attempt=%s reason=%s", attempt + 1, exc)
            page.wait_for_timeout(SHORT_WAIT_MS)

    return False


def assert_current_account(page: Page, expected: AdsAccount, logger=None) -> bool:
    return assert_current_account_context(page, expected, ready_check=is_report_editor_ready, logger=logger)


def assert_current_account_context(
    page: Page,
    expected: AdsAccount,
    ready_check: PageReadyCheck | None = None,
    logger=None,
) -> bool:
    """
    Verify account context with multiple signals.
    Hard signal: expected CID visible.
    Soft signal: selector closed + page-specific ready check passed.
    """
    attempts = max(1, int(ACCOUNT_VERIFY_TIMEOUT_MS / 700))
    cid_visible = False
    selector_present = True
    page_ready = False
    name_visible = False
    ready_label = getattr(ready_check, "__name__", "page_ready") if ready_check else "page_ready"

    for _ in range(attempts):
        cid_visible = _is_expected_cid_visible(page, expected)
        selector_present = account_selector_visible(page)
        page_ready = ready_check(page) if ready_check is not None else True
        name_visible = _is_account_name_visible(page, expected.name)

        # Prevent false positive on /nav/selectaccount rows where CID is visible
        # but target page context is not established yet.
        if cid_visible and (page_ready or not selector_present):
            break

        # Soft-success path to prevent false retry loops:
        # account selector is gone and target page is ready.
        if not selector_present and page_ready:
            if logger:
                logger.warning(
                    "account verification soft-pass | reason=%s_without_selector | expected=%s (%s)",
                    ready_label,
                    expected.name,
                    expected.cid,
                )
            return True

        page.wait_for_timeout(700)

    if logger:
        logger.info(
            "account verification | expected=%s (%s) | verified=%s | cid_visible=%s | selector_present=%s | %s=%s | name_visible=%s",
            expected.name,
            expected.cid,
            bool(cid_visible and (page_ready or not selector_present)),
            cid_visible,
            selector_present,
            ready_label,
            page_ready,
            name_visible,
        )
    return bool(cid_visible and (page_ready or not selector_present))


def is_report_editor_ready(page: Page) -> bool:
    url = (page.url or "").lower()
    if "ads.google.com/aw/reporteditor" not in url:
        return False

    heading = page.get_by_text("Saved reports", exact=False)
    try:
        if heading.count() > 0 and heading.first.is_visible(timeout=900):
            return True
    except Exception:  # noqa: BLE001
        pass

    # Fallback: at least one plausible reports row is visible.
    for selector in (
        "section:has-text('Saved reports') tbody tr",
        "div:has-text('Saved reports') tbody tr",
        "particle-table-row",
        "[role='row']",
    ):
        rows = page.locator(selector)
        try:
            if rows.count() > 0:
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def account_selector_visible(page: Page) -> bool:
    if "/nav/selectaccount" in (page.url or ""):
        return True

    header = page.get_by_text("Select a Google Ads account", exact=False)
    try:
        if header.first.is_visible(timeout=SELECTOR_DETECT_TIMEOUT_MS):
            return True
    except Exception:  # noqa: BLE001
        pass

    candidates = _collect_selector_candidates(page, cap=5)
    return any(c["cid"] for c in candidates)


def _collect_selector_candidates(page: Page, cap: int | None = None) -> list[dict]:
    selectors = [
        "material-list-item[role='menuitem']",
        "multi-account-picker[role='listbox'] material-select-item[role='option']",
        "material-select-item[role='option']",
    ]
    candidates: list[dict] = []
    for selector in selectors:
        rows = page.locator(selector)
        count = rows.count()
        for i in range(count):
            row = rows.nth(i)
            raw_text = _safe_inner_text(row)
            cid = extract_cid(raw_text)
            if not raw_text and not cid:
                continue
            name = _safe_inner_text(row.locator(".customer-name").first)
            if not name:
                name = _safe_inner_text(row.locator("span.name").first)
            if not name:
                name = _derive_name(raw_text, cid)
            candidates.append(
                {
                    "locator": row,
                    "name": name,
                    "cid": cid,
                    "raw_text": raw_text,
                }
            )
            if cap and len(candidates) >= cap:
                return candidates
        if candidates:
            return candidates
    return candidates


def _choose_selector_candidate(candidates: list[dict], account: AdsAccount) -> dict | None:
    # 1) exact CID
    for c in candidates:
        if c.get("cid") == account.cid:
            return c

    # 2) exact name + CID
    for c in candidates:
        if c.get("name") == account.name and c.get("cid") == account.cid:
            return c

    # 3) exact unique name when CID is missing
    same_name = [c for c in candidates if c.get("name") == account.name]
    if len(same_name) == 1:
        return same_name[0]

    return None


def _wait_for_selector_close(page: Page) -> bool:
    for _ in range(10):
        if not account_selector_visible(page):
            return True
        page.wait_for_timeout(500)
    return False


def _wait_for_report_editor_ready(page: Page, timeout_ms: int = 12000) -> bool:
    elapsed = 0
    while elapsed < timeout_ms:
        if is_report_editor_ready(page):
            return True
        page.wait_for_timeout(600)
        elapsed += 600
    return False


def _is_expected_cid_visible(page: Page, expected: AdsAccount) -> bool:
    candidates = [expected.cid]
    if expected.cid_digits and len(expected.cid_digits) == 10:
        candidates.append(
            f"{expected.cid_digits[0:3]}-{expected.cid_digits[3:6]}-{expected.cid_digits[6:10]}"
        )
        candidates.append(expected.cid_digits)

    for candidate in candidates:
        if not candidate:
            continue
        try:
            if page.locator(f"text={candidate}").first.is_visible(timeout=900):
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _is_account_name_visible(page: Page, account_name: str) -> bool:
    if not account_name:
        return False
    try:
        return page.locator(f"text={account_name}").first.is_visible(timeout=900)
    except Exception:  # noqa: BLE001
        return False


def _safe_inner_text(locator) -> str:
    try:
        return (locator.inner_text(timeout=1200) or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _derive_name(raw_text: str, cid: str | None) -> str:
    if not raw_text:
        return ""
    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    for line in lines:
        if cid and cid in line:
            continue
        if "manager" in line.lower():
            continue
        return line
    return ""
