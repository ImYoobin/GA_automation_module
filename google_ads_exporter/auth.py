"""Browser launch and login/session management."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence

from playwright.sync_api import BrowserContext, Page, Playwright

from .config import BROWSER_AUTO_ORDER, LOGIN_URL, REPORT_EDITOR_URL, SELECT_ACCOUNT_URL
from .utils import get_user_data_dir

SIGNIN_URL_PATTERN = re.compile(r"(accounts\.google\.com|servicelogin|signin)", re.IGNORECASE)


def _resolve_browser_order(browser_preference: str) -> Sequence[str]:
    normalized = (browser_preference or "msedge").strip().lower()
    if normalized == "auto":
        return BROWSER_AUTO_ORDER
    if normalized == "chromium":
        return ("chromium",)
    if normalized in {"msedge", "chrome"}:
        # Keep the preferred browser first, but allow fallback launches.
        order: list[str] = [normalized]
        for name in BROWSER_AUTO_ORDER:
            if name not in order:
                order.append(name)
        return tuple(order)
    raise ValueError(f"Unsupported --browser value: {browser_preference}")


def launch_ads_context(
    playwright: Playwright,
    headless: bool = False,
    browser_preference: str = "msedge",
    logger=None,
) -> tuple[BrowserContext, Page, str]:
    """
    Launch a persistent context with browser fallback.
    Returns (context, page, browser_used).
    """
    last_error: Exception | None = None
    order = _resolve_browser_order(browser_preference)

    for browser_name in order:
        user_data_dir = get_user_data_dir() / browser_name
        user_data_dir.mkdir(parents=True, exist_ok=True)
        launch_kwargs = {
            "user_data_dir": str(user_data_dir),
            "headless": headless,
            "accept_downloads": True,
            "no_viewport": True,
        }
        if browser_name != "chromium":
            launch_kwargs["channel"] = browser_name

        try:
            if logger:
                logger.info(
                    "Attempting browser launch: %s | profile_dir=%s",
                    browser_name,
                    user_data_dir,
                )
            context = playwright.chromium.launch_persistent_context(**launch_kwargs)
            page = context.pages[0] if context.pages else context.new_page()
            if logger:
                logger.info(
                    "Browser launch success: %s | profile_dir=%s",
                    browser_name,
                    user_data_dir,
                )
            return context, page, browser_name
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if logger:
                logger.warning(
                    "Browser launch failed: %s | profile_dir=%s | reason=%s",
                    browser_name,
                    user_data_dir,
                    exc,
                )

    error_text = str(last_error or "").strip() or repr(last_error) or "unknown"
    raise RuntimeError(f"browser cannot open: {error_text}")


def minimize_browser_window(page: Page, logger=None) -> bool:
    """
    Minimize current Chromium-based window via CDP.
    Returns True on success, False otherwise.
    """
    return _set_browser_window_state(page, "minimized", logger=logger)


def maximize_browser_window(page: Page, logger=None) -> bool:
    """
    Maximize current Chromium-based window via CDP.
    Returns True on success, False otherwise.
    """
    return _set_browser_window_state(page, "maximized", logger=logger)


def get_browser_window_state(page: Page, logger=None) -> str:
    """
    Return current window state (e.g. minimized/maximized/normal/fullscreen).
    Returns empty string on failure.
    """
    try:
        session = page.context.new_cdp_session(page)
        window_info = session.send("Browser.getWindowForTarget")
        bounds = window_info.get("bounds", {}) if isinstance(window_info, dict) else {}
        state = str(bounds.get("windowState") or "").strip().lower()
        return state
    except Exception as exc:  # noqa: BLE001
        if logger:
            logger.info("browser get window state skipped reason=%s", exc)
        return ""


def _set_browser_window_state(page: Page, state: str, logger=None) -> bool:
    normalized = str(state or "").strip().lower()
    if normalized not in {"minimized", "maximized", "normal", "fullscreen"}:
        if logger:
            logger.info("browser window state change skipped: unsupported state=%s", state)
        return False

    try:
        session = page.context.new_cdp_session(page)
        window_info = session.send("Browser.getWindowForTarget")
        window_id = window_info.get("windowId")
        if not window_id:
            if logger:
                logger.info("browser window state change skipped: windowId unavailable")
            return False
        session.send(
            "Browser.setWindowBounds",
            {
                "windowId": window_id,
                "bounds": {"windowState": normalized},
            },
        )
        if logger:
            logger.info("browser window state changed state=%s", normalized)
        return True
    except Exception as exc:  # noqa: BLE001
        if logger:
            logger.warning("browser window state change failed state=%s reason=%s", normalized, exc)
        return False


def ensure_logged_in(
    page: Page,
    logger=None,
    on_manual_login_required: Callable[[Page], None] | None = None,
) -> Page:
    """
    Provider-agnostic login validation:
      1) wait until sign-in URL is cleared
      2) verify Google Ads capability (shell/selectaccount/reporteditor access)
    Returns the live Page to continue automation with.
    """
    manual_login_required = False
    final_login_verified = False

    current_page = _resolve_live_page(page, logger=logger, create_if_missing=True)
    current_page.goto(LOGIN_URL, wait_until="domcontentloaded")
    current_page.wait_for_timeout(1200)

    max_rounds = 8
    for _ in range(max_rounds):
        current_page = _resolve_live_page(current_page, logger=logger, create_if_missing=True)
        current_url = _page_url(current_page)

        if _is_signin_url(current_url):
            if not manual_login_required and on_manual_login_required is not None:
                try:
                    on_manual_login_required(current_page)
                except Exception as exc:  # noqa: BLE001
                    if logger:
                        logger.info("manual login callback skipped reason=%s", exc)
            manual_login_required = True
            if logger:
                logger.info("manual login required. current_url=%s", current_url)

            cleared, current_page = _wait_until_signin_url_clears(current_page, logger=logger)
            if not cleared:
                break
            current_url = _page_url(current_page)

        landed_ads, current_page = _wait_for_ads_landing_after_manual_login(
            current_page,
            logger=logger,
            timeout_ms=15000,
        )
        if landed_ads:
            final_login_verified = _verify_logged_in_state(current_page, logger=logger)
            if final_login_verified:
                break

        if not _is_signin_url(_page_url(current_page)):
            final_login_verified = _attempt_ads_entrypoints(current_page, logger=logger)
            if final_login_verified:
                break

    current_page = _resolve_live_page(current_page, logger=logger, create_if_missing=True)
    if logger:
        logger.info(
            "login check result | manual_login_required=%s | final_login_verified=%s | current_url=%s",
            str(manual_login_required).lower(),
            str(final_login_verified).lower(),
            _page_url(current_page),
        )

    if not final_login_verified:
        raise RuntimeError("login required")
    return current_page


def assert_session_active(page: Page, logger=None) -> None:
    """Stop immediately if a login page appears during automation."""
    if _is_login_required(page):
        if logger:
            logger.error("Session expired during automation. Current URL: %s", page.url)
        raise RuntimeError("session expired, please log in again")


def _is_login_required(page: Page) -> bool:
    if _is_signin_url(_page_url(page)):
        return True

    url = _page_url(page).lower()
    if "/nav/login" not in url:
        return False

    login_inputs = page.locator("#identifierId, input[type='email'], input[type='password']")
    if login_inputs.count() > 0:
        return True

    sign_in_text = page.get_by_text("Sign in", exact=False)
    return sign_in_text.count() > 0


def _is_signin_url(url: str) -> bool:
    return bool(SIGNIN_URL_PATTERN.search(url or ""))


def _is_ads_non_signin_url(url: str) -> bool:
    lower = (url or "").lower()
    return ("ads.google.com" in lower) and (not _is_signin_url(lower))


def _page_url(page: Page) -> str:
    try:
        return page.url or ""
    except Exception:  # noqa: BLE001
        return ""


def _is_page_closed(page: Page) -> bool:
    try:
        return page.is_closed()
    except Exception:  # noqa: BLE001
        return True


def _resolve_live_page(page: Page, logger=None, create_if_missing: bool = False) -> Page:
    context = None
    try:
        context = page.context
    except Exception:  # noqa: BLE001
        context = None

    candidates: list[Page] = []
    if page and not _is_page_closed(page):
        candidates.append(page)

    if context is not None:
        try:
            for candidate in context.pages:
                if candidate in candidates:
                    continue
                if _is_page_closed(candidate):
                    continue
                candidates.append(candidate)
        except Exception:  # noqa: BLE001
            pass

    if candidates:
        ads_candidates = [p for p in candidates if _is_ads_non_signin_url(_page_url(p))]
        if ads_candidates:
            return ads_candidates[0]
        non_signin_candidates = [p for p in candidates if not _is_signin_url(_page_url(p))]
        if non_signin_candidates:
            return non_signin_candidates[0]
        return candidates[0]

    if create_if_missing and context is not None:
        try:
            new_page = context.new_page()
            if logger:
                logger.info("created new page because previous page was closed")
            return new_page
        except Exception as exc:  # noqa: BLE001
            if logger:
                logger.warning("failed to create replacement page: %s", exc)

    raise RuntimeError("browser page is closed")


def _wait_until_signin_url_clears(
    page: Page,
    logger=None,
    timeout_ms: int = 1_800_000,
) -> tuple[bool, Page]:
    """
    Wait until URL leaves Google sign-in surface.
    Returns (cleared, live_page).
    """
    elapsed = 0
    interval = 900
    current_page = page

    while elapsed < timeout_ms:
        try:
            current_page = _resolve_live_page(current_page, logger=logger, create_if_missing=False)
        except Exception as exc:  # noqa: BLE001
            if logger:
                logger.warning("manual login wait failed: no live page (%s)", exc)
            return False, page

        current_url = _page_url(current_page)
        if not _is_signin_url(current_url):
            if logger:
                logger.info("manual login completed (signin url cleared). current_url=%s", current_url)
            return True, current_page

        switched, current_page = _switch_to_better_context_page(current_page, logger=logger)
        if switched and not _is_signin_url(_page_url(current_page)):
            if logger:
                logger.info(
                    "manual login completed (switched context page). current_url=%s",
                    _page_url(current_page),
                )
            return True, current_page

        try:
            current_page.wait_for_timeout(interval)
        except Exception as exc:  # noqa: BLE001
            if logger:
                logger.info("manual login wait recovered from page transition: %s", exc)
        elapsed += interval

    if logger:
        logger.warning("manual login wait timeout. current_url=%s", _page_url(current_page))
    return (not _is_signin_url(_page_url(current_page))), current_page


def _wait_for_ads_landing_after_manual_login(
    page: Page,
    logger=None,
    timeout_ms: int = 15000,
) -> tuple[bool, Page]:
    """
    After signin URL is cleared, wait briefly for redirects to land on ads.google.com.
    Returns (landed_ads, live_page).
    """
    elapsed = 0
    interval = 700
    current_page = page

    while elapsed < timeout_ms:
        try:
            current_page = _resolve_live_page(current_page, logger=logger, create_if_missing=False)
        except Exception as exc:  # noqa: BLE001
            if logger:
                logger.warning("ads landing wait failed: no live page (%s)", exc)
            return False, page

        current_url = _page_url(current_page)
        if _is_ads_non_signin_url(current_url):
            return True, current_page
        if _is_signin_url(current_url):
            return False, current_page

        switched, current_page = _switch_to_better_context_page(current_page, logger=logger)
        if switched and _is_ads_non_signin_url(_page_url(current_page)):
            return True, current_page

        try:
            current_page.wait_for_timeout(interval)
        except Exception as exc:  # noqa: BLE001
            if logger:
                logger.info("ads landing wait recovered from page transition: %s", exc)
        elapsed += interval

    return _is_ads_non_signin_url(_page_url(current_page)), current_page


def _switch_to_better_context_page(page: Page, logger=None) -> tuple[bool, Page]:
    """
    Switch working page reference to a better candidate in the same context:
      ads non-signin > non-signin > any live page
    """
    try:
        context_pages = [p for p in page.context.pages if not _is_page_closed(p)]
    except Exception:  # noqa: BLE001
        return False, page

    if not context_pages:
        return False, page

    def _rank(p: Page) -> int:
        url = _page_url(p)
        if _is_ads_non_signin_url(url):
            return 0
        if not _is_signin_url(url):
            return 1
        return 2

    best = sorted(context_pages, key=_rank)[0]
    if best == page:
        return False, page

    if logger:
        logger.info(
            "switched active page | from=%s | to=%s",
            _page_url(page),
            _page_url(best),
        )
    return True, best


def _verify_logged_in_state(page: Page, logger=None) -> bool:
    """
    Final login success requires Google Ads capability proof:
      1) URL is not sign-in URL
      2) at least one of:
         - ads shell/header/account control visible
         - nav/selectaccount account rows available
         - report editor openable without sign-in redirect
    """
    current_url = _page_url(page)
    if _is_signin_url(current_url):
        if logger:
            logger.info("login verification failed: sign-in URL detected (%s)", current_url)
        return False

    if _has_ads_shell(page):
        return True

    if _check_selectaccount_rows(page):
        return True

    if _check_report_editor_openable(page):
        return True

    if logger:
        logger.info("login verification failed: ads capability not yet proven. current_url=%s", current_url)
    return False


def _has_ads_shell(page: Page) -> bool:
    url = _page_url(page).lower()
    if "ads.google.com" not in url:
        return False
    try:
        account_btn = page.get_by_role("button", name="Google account")
        if account_btn.count() > 0 and account_btn.first.is_visible(timeout=1200):
            return True
    except Exception:  # noqa: BLE001
        pass

    try:
        if page.get_by_text("Google Ads", exact=False).first.is_visible(timeout=1200):
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


def _check_selectaccount_rows(page: Page) -> bool:
    previous_url = _page_url(page)
    try:
        page.goto(SELECT_ACCOUNT_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(900)
        if _is_signin_url(_page_url(page)):
            return False
        rows = page.locator("material-list-item[role='menuitem']")
        if rows.count() > 0:
            return True
        generic_rows = page.locator("[role='menuitem']")
        return generic_rows.count() > 0
    except Exception:  # noqa: BLE001
        return False
    finally:
        if previous_url and _page_url(page) != previous_url and "ads.google.com" in previous_url:
            try:
                page.goto(previous_url, wait_until="domcontentloaded")
            except Exception:  # noqa: BLE001
                pass


def _check_report_editor_openable(page: Page) -> bool:
    previous_url = _page_url(page)
    try:
        page.goto(REPORT_EDITOR_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(900)
        return _is_ads_non_signin_url(_page_url(page))
    except Exception:  # noqa: BLE001
        return False
    finally:
        if previous_url and _page_url(page) != previous_url and "ads.google.com" in previous_url:
            try:
                page.goto(previous_url, wait_until="domcontentloaded")
            except Exception:  # noqa: BLE001
                pass


def _attempt_ads_entrypoints(page: Page, logger=None) -> bool:
    entrypoints = (SELECT_ACCOUNT_URL, REPORT_EDITOR_URL, LOGIN_URL)
    current_page = page
    for url in entrypoints:
        try:
            current_page = _resolve_live_page(current_page, logger=logger, create_if_missing=True)
            current_page.goto(url, wait_until="domcontentloaded")
            _wait_for_post_login_settle(current_page, logger=logger)
            if _verify_logged_in_state(current_page, logger=logger):
                if logger:
                    logger.info("login verification passed via entrypoint=%s", url)
                return True
        except Exception as exc:  # noqa: BLE001
            if logger:
                logger.warning("entrypoint check failed url=%s reason=%s", url, exc)
    return False


def _wait_for_post_login_settle(page: Page, logger=None, timeout_ms: int = 12000) -> None:
    elapsed = 0
    interval = 800
    current_page = page
    while elapsed < timeout_ms:
        try:
            current_page = _resolve_live_page(current_page, logger=logger, create_if_missing=False)
        except Exception:
            return
        if not _is_signin_url(_page_url(current_page)):
            break
        try:
            current_page.wait_for_timeout(interval)
        except Exception:
            pass
        elapsed += interval
