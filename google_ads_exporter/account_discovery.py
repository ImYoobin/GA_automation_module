"""Account discovery from Google Ads account selection surfaces."""

from __future__ import annotations

import re

from playwright.sync_api import Page

from .config import ACCOUNT_LIST_LOAD_RETRIES, REPORT_EDITOR_URL, SELECT_ACCOUNT_URL, SHORT_WAIT_MS
from .models import AdsAccount
from .utils import cid_to_digits, dedupe_accounts, extract_cid

SELECTACCOUNT_POLL_TIMEOUT_MS = 12000
SELECTACCOUNT_POLL_INTERVAL_MS = 700


def collect_accounts(page: Page, logger=None) -> list[AdsAccount]:
    accounts: list[AdsAccount] = []
    current_url = (page.url or "").lower()
    on_selectaccount = "/nav/selectaccount" in current_url

    if on_selectaccount:
        accounts = _collect_from_selectaccount_with_retries(page, logger=logger)
        if not accounts:
            if logger:
                logger.info("Trying fallback parser via top-right profile picker.")
            accounts = _collect_from_profile_picker(page, logger=logger)
    else:
        if logger:
            logger.info(
                "current page is not selectaccount. starting fallback parser first | url=%s",
                page.url,
            )
        accounts = _collect_from_profile_picker(page, logger=logger)
        if not accounts:
            if logger:
                logger.info("Fallback parser returned no accounts. Trying selectaccount parser.")
            accounts = _collect_from_selectaccount_with_retries(page, logger=logger)

    accounts = dedupe_accounts(accounts)
    if logger:
        logger.info("Account discovery complete. count=%s", len(accounts))
        for account in accounts:
            logger.info(
                "account parsed | source=final | name=%s | cid=%s | manager=%s",
                account.name,
                account.cid,
                account.is_manager,
            )

    if not accounts:
        raise RuntimeError("no account list can be collected")
    return accounts


def _collect_from_selectaccount_with_retries(page: Page, logger=None) -> list[AdsAccount]:
    accounts: list[AdsAccount] = []
    for attempt in range(ACCOUNT_LIST_LOAD_RETRIES + 1):
        accounts = _collect_from_selectaccount(page, logger=logger)
        if accounts:
            break
        if logger:
            logger.warning("selectaccount parser returned no accounts (attempt=%s)", attempt + 1)
        page.wait_for_timeout(SHORT_WAIT_MS)
    return accounts


def _collect_from_selectaccount(page: Page, logger=None) -> list[AdsAccount]:
    current_url = (page.url or "").lower()
    if "/nav/selectaccount" not in current_url:
        page.goto(SELECT_ACCOUNT_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(SHORT_WAIT_MS)

    accounts = _poll_selectaccount_accounts(page, logger=logger)
    if accounts:
        return accounts

    # Hard reload fallback on same endpoint when initial surface is stale/empty.
    page.goto(SELECT_ACCOUNT_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(SHORT_WAIT_MS)
    return _poll_selectaccount_accounts(page, logger=logger)


def _collect_from_profile_picker(page: Page, logger=None) -> list[AdsAccount]:
    trigger = page.get_by_role("button", name="Google account")
    if trigger.count() == 0:
        trigger = page.locator('div[role="button"][aria-label="Google account"]')
    if trigger.count() == 0:
        try:
            page.goto(REPORT_EDITOR_URL, wait_until="domcontentloaded")
            page.wait_for_timeout(SHORT_WAIT_MS)
            trigger = page.get_by_role("button", name="Google account")
            if trigger.count() == 0:
                trigger = page.locator('div[role="button"][aria-label="Google account"]')
        except Exception:  # noqa: BLE001
            pass
        if trigger.count() == 0:
            if logger:
                logger.warning("Profile picker trigger not found.")
            return []

    trigger.first.click()
    page.wait_for_timeout(SHORT_WAIT_MS)

    row_locator = page.locator(
        'multi-account-picker[aria-label="List of customers"][role="listbox"] '
        'material-select-item[role="option"]'
    )
    if row_locator.count() == 0:
        row_locator = page.locator('material-select-item[role="option"]')

    accounts: list[AdsAccount] = []
    for i in range(row_locator.count()):
        row = row_locator.nth(i)
        raw_text = safe_inner_text(row)
        cid = extract_cid(raw_text)
        if not cid:
            continue

        name = safe_inner_text(row.locator("span.name").first)
        if not name:
            name = safe_inner_text(row.locator("div.line-1").first)
        if not name:
            name = _name_from_row_text(raw_text, cid)

        account = AdsAccount(
            name=name or cid,
            cid=cid,
            cid_digits=cid_to_digits(cid),
            is_manager=bool(re.search(r"\bmanager\b", raw_text, flags=re.IGNORECASE)),
            raw_text=raw_text,
        )
        accounts.append(account)
        if logger:
            logger.info(
                "account row | source=profile_popup | name=%s | cid=%s | manager=%s",
                account.name,
                account.cid,
                account.is_manager,
            )

    page.keyboard.press("Escape")
    return accounts


def _poll_selectaccount_accounts(page: Page, logger=None) -> list[AdsAccount]:
    elapsed = 0
    while elapsed <= SELECTACCOUNT_POLL_TIMEOUT_MS:
        accounts = _parse_selectaccount_accounts_from_roots(page, logger=logger)
        if accounts:
            return accounts
        page.wait_for_timeout(SELECTACCOUNT_POLL_INTERVAL_MS)
        elapsed += SELECTACCOUNT_POLL_INTERVAL_MS

    if logger:
        logger.warning(
            "selectaccount polling exhausted | url=%s | body_has_selectaccount_text=%s",
            page.url,
            "select a google ads account" in safe_inner_text(page.locator("body").first).lower(),
        )
        _log_selector_counts(page, logger=logger)
    return []


def _parse_selectaccount_accounts_from_roots(page: Page, logger=None) -> list[AdsAccount]:
    roots = [page]
    for frame in page.frames:
        # page.frames often includes main frame; avoid duplicate scanning.
        if frame == page.main_frame:
            continue
        roots.append(frame)
    accounts: list[AdsAccount] = []

    for root in roots:
        parsed = _parse_selectaccount_accounts(root, logger=logger)
        if parsed:
            accounts.extend(parsed)

    accounts = dedupe_accounts(accounts)
    return accounts


def _parse_selectaccount_accounts(root, logger=None) -> list[AdsAccount]:
    row_selectors = [
        "material-list-item.user-customer-list-item.item[role='menuitem']",
        "material-list-item.user-customer-list-item[role='menuitem']",
        "material-list-item[role='menuitem']",
        "material-list-item.user-customer-list-item.item",
        "material-list-item.user-customer-list-item",
        "material-list-item",
        "[role='menuitem']",
    ]

    rows = None
    selector_used = None
    for selector in row_selectors:
        loc = root.locator(selector)
        count = loc.count()
        if count > 0:
            rows = loc
            selector_used = selector
            break
    if rows is None:
        return []

    accounts: list[AdsAccount] = []
    for i in range(rows.count()):
        row = rows.nth(i)
        raw_text = safe_inner_text(row)
        cid = extract_cid(raw_text)
        if not cid:
            secondary = safe_inner_text(row.locator("span.material-list-item-secondary").first)
            cid = extract_cid(secondary)
        if not cid:
            continue

        name = safe_inner_text(row.locator("div.customer-wrapper .customer-name").first)
        if not name:
            name = safe_inner_text(row.locator(".customer-name").first)
        if not name:
            name = _name_from_row_text(raw_text, cid)

        is_manager = bool(re.search(r"\bmanager\b", raw_text, flags=re.IGNORECASE))
        account = AdsAccount(
            name=name or cid,
            cid=cid,
            cid_digits=cid_to_digits(cid),
            is_manager=is_manager,
            raw_text=raw_text,
        )
        accounts.append(account)
        if logger:
            logger.info(
                "account row | source=selectaccount | selector=%s | name=%s | cid=%s | manager=%s",
                selector_used,
                account.name,
                account.cid,
                account.is_manager,
            )
    return accounts


def _log_selector_counts(page: Page, logger=None) -> None:
    if not logger:
        return
    selectors = [
        "material-list-item.user-customer-list-item.item[role='menuitem']",
        "material-list-item[role='menuitem']",
        "[role='menuitem']",
        "material-list-item",
        "multi-account-picker[aria-label='List of customers'][role='listbox'] material-select-item[role='option']",
    ]
    for selector in selectors:
        try:
            count_main = page.locator(selector).count()
            count_frames = 0
            for frame in page.frames:
                try:
                    count_frames += frame.locator(selector).count()
                except Exception:  # noqa: BLE001
                    continue
            logger.info(
                "selectaccount selector count | selector=%s | main=%s | frames=%s",
                selector,
                count_main,
                count_frames,
            )
        except Exception as exc:  # noqa: BLE001
            logger.info("selectaccount selector count failed | selector=%s | reason=%s", selector, exc)


def safe_inner_text(locator) -> str:
    try:
        return (locator.inner_text(timeout=1500) or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _name_from_row_text(raw_text: str, cid: str) -> str:
    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    for line in lines:
        if cid in line:
            continue
        if re.search(r"\bmanager\b", line, flags=re.IGNORECASE):
            continue
        return line
    return ""
