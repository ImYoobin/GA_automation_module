"""Saved reports scanner and target matching."""

from __future__ import annotations

import re

from playwright.sync_api import Frame, Page

from .config import TABLE_SCAN_TIMEOUT_MS
from .models import SavedReportItem
from .targets import REPORT_KEYS, TARGET_ORDER, find_matching_keys, match_target_key
from .utils import normalize_report_name

PANEL_SELECTOR = "material-expansionpanel"
SAVED_REPORTS_HEADER_SELECTOR = "div[role='button'][aria-label='Saved reports']"
SAVED_REPORTS_REGION_SELECTOR = "div.main[role='region'], div[role='region']"
SHOW_ROWS_BUTTON_SELECTOR = "div[role='button'][aria-label*='Show rows']"
SHOW_ROWS_LISTBOX_SELECTOR = (
    "material-list[role='listbox'][aria-label*='Choose number of rows to be displayed per page']"
)
SHOW_ROWS_BUTTON_FALLBACK_SELECTOR = "div[role='button']:has(span.button-text)"

SCOPED_ROW_SELECTORS = [
    ".particle-table-row",
    "div.particle-table-row",
    "[class*='particle-table-row']",
    "ess-row[role='row']",
    "particle-table-row",
    "div[role='row']",
    "[role='row']",
    "tbody tr",
]

GLOBAL_ROW_SELECTORS = [
    ".particle-table-row",
    "div.particle-table-row",
    "[class*='particle-table-row']",
    "section:has-text('Saved reports') [role='row']",
    "div:has-text('Saved reports') [role='row']",
    "section:has-text('Saved reports') tbody tr",
    "div:has-text('Saved reports') tbody tr",
    "particle-table-row",
    "tbody tr",
    "[role='row']",
]

MONTH_REGEX = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)\b",
    re.IGNORECASE,
)
EMAIL_REGEX = re.compile(r"\b[^@\s]+@[^@\s]+\.[^@\s]+\b")
CID_REGEX = re.compile(r"\b\d{3}-\d{3}-\d{4}\b")
BCG_LINE_REGEX = re.compile(r"\bBCG[\w\s\-\(\),]*", re.IGNORECASE)
ROW_COUNT_REGEX = re.compile(r"^(10|25|50|100|250|500|1000)$")


def scan_saved_reports(page: Page, logger=None) -> list[SavedReportItem]:
    roots = [page, *page.frames]
    source_url = page.url
    best_items: list[SavedReportItem] = []
    best_match_count = -1

    # Preferred path for observed Google Ads DOM: ess-cell with stable essfield attributes.
    for root in roots:
        if logger:
            logger.info("saved reports scan root=%s", _root_label(root))
        _ensure_saved_reports_expanded(root, logger=logger)
        _set_show_rows_to_500(root, logger=logger)
        _wait_for_table_area(root)
        _materialize_saved_reports_rows(root, logger=logger)
        _ensure_saved_reports_expanded(root, logger=logger)
        scopes = _build_scan_scopes(root, logger=logger)
        for scope_label, scope in scopes:
            items = _scan_by_essfield_columns(scope, logger=logger, scope_label=scope_label)
            if items:
                matched_count = sum(1 for item in items if item.matched_key)
                if matched_count > best_match_count:
                    best_match_count = matched_count
                    best_items = items
                if logger:
                    logger.info(
                        "saved reports scope result | scope=%s | row_count=%s | matched_count=%s",
                        scope_label,
                        len(items),
                        matched_count,
                    )
                if matched_count == 0:
                    # Keep trying other scopes/roots before returning.
                    continue
                try:
                    source_url = root.url
                except Exception:  # noqa: BLE001
                    source_url = page.url
                if logger:
                    logger.info(
                        "saved reports parsed via essfield columns. scope=%s row count=%s source_url=%s",
                        scope_label,
                        len(items),
                        source_url,
                    )
                return items

    if best_items:
        if logger:
            logger.info(
                "saved reports returning best unmatched set | row_count=%s | matched_count=%s",
                len(best_items),
                best_match_count,
            )
        return best_items

    # Fallback path: row-based parsing.
    rows = []
    for root in roots:
        if logger:
            logger.info("saved reports row-fallback root=%s", _root_label(root))
        _ensure_saved_reports_expanded(root, logger=logger)
        _set_show_rows_to_500(root, logger=logger)
        _wait_for_table_area(root)
        _materialize_saved_reports_rows(root, logger=logger)
        _ensure_saved_reports_expanded(root, logger=logger)
        scopes = _build_scan_scopes(root, logger=logger)
        for scope_label, scope in scopes:
            rows = _resolve_rows(scope, logger=logger, scope_label=scope_label)
            if rows:
                try:
                    source_url = root.url
                except Exception:  # noqa: BLE001
                    source_url = page.url
                break
        if rows:
            break

    items = _parse_row_locators(rows, logger=logger)
    if not items:
        for root in roots:
            scopes = _build_scan_scopes(root, logger=logger)
            for scope_label, scope in scopes:
                fallback_items = _scan_by_name_elements(scope, logger=logger, scope_label=scope_label)
                if fallback_items:
                    items = fallback_items
                    try:
                        source_url = root.url
                    except Exception:  # noqa: BLE001
                        source_url = page.url
                    break
            if items:
                break
    if not items:
        for root in roots:
            fallback_items = _scan_by_saved_reports_text(root, logger=logger)
            if fallback_items:
                items = fallback_items
                try:
                    source_url = root.url
                except Exception:  # noqa: BLE001
                    source_url = page.url
                break
    if logger:
        logger.info("saved reports row count=%s source_url=%s", len(items), source_url)
    return items


def _build_scan_scopes(root, logger=None) -> list[tuple[str, object]]:
    """
    Return scan scopes in priority order:
    1) Saved reports panel region
    2) Saved reports panel container
    3) root (page/frame)
    """
    scopes: list[tuple[str, object]] = []
    panels = root.locator(PANEL_SELECTOR)
    panel_count = panels.count()
    if logger:
        logger.info("saved reports panel candidates count=%s", panel_count)

    for idx in range(panel_count):
        panel = panels.nth(idx)
        header = panel.locator(SAVED_REPORTS_HEADER_SELECTOR).first
        if header.count() == 0:
            # Fallback when aria-label is localized/changed.
            header = panel.get_by_text("Saved reports", exact=False).first
            if header.count() == 0:
                continue

        region = panel.locator(SAVED_REPORTS_REGION_SELECTOR).first
        if region.count() > 0:
            hidden = (region.get_attribute("aria-hidden") or "").strip().lower()
            scopes.append((f"panel[{idx}]/region(aria-hidden={hidden or 'n/a'})", region))
        scopes.append((f"panel[{idx}]", panel))

    scopes.append(("root", root))
    return scopes


def _materialize_saved_reports_rows(root, logger=None) -> None:
    """
    In some Google Ads layouts, Saved reports data rows are virtualized and do not
    get mounted until the panel/viewport receives focus and scroll events.
    """
    scopes = _build_scan_scopes(root, logger=logger)
    for scope_label, scope in scopes:
        if "panel[" not in scope_label:
            continue
        if logger:
            logger.info("saved reports materialize attempt | scope=%s", scope_label)
        try:
            scope.get_by_text("Reports", exact=True).first.click(timeout=1000)
        except Exception:  # noqa: BLE001
            pass

        for _ in range(2):
            try:
                scope.evaluate(
                    """(el) => {
                        try {
                            el.scrollTop = 0;
                            el.dispatchEvent(new Event('scroll', { bubbles: true }));
                            el.scrollTop = Math.max(0, el.scrollHeight);
                            el.dispatchEvent(new Event('scroll', { bubbles: true }));
                            el.scrollTop = 0;
                            el.dispatchEvent(new Event('scroll', { bubbles: true }));
                        } catch (e) {}
                    }"""
                )
            except Exception:  # noqa: BLE001
                pass

            _get_owner_page(root).wait_for_timeout(250)

    owner_page = _get_owner_page(root)
    try:
        owner_page.evaluate("() => window.scrollBy(0, 800)")
        owner_page.wait_for_timeout(150)
        owner_page.evaluate("() => window.scrollBy(0, -400)")
    except Exception:  # noqa: BLE001
        pass


def _get_owner_page(root) -> Page:
    if isinstance(root, Page):
        return root
    if isinstance(root, Frame):
        return root.page
    # fallback for Page-like objects
    return root.page


def _root_label(root) -> str:
    if isinstance(root, Page):
        return f"page(url={root.url})"
    if isinstance(root, Frame):
        name = root.name or "(no-name)"
        return f"frame(name={name}, url={root.url})"
    return f"root(type={type(root).__name__})"


def _scan_by_essfield_columns(scope, logger=None, scope_label: str = "root") -> list[SavedReportItem]:
    owner_page = _owner_page_from_scope(scope)
    _reset_scope_scroll(scope, owner_page)
    aggregated: list[SavedReportItem] = []
    seen_signatures: set[str] = set()
    no_new_rounds = 0
    no_move_rounds = 0
    stable_match_rounds = 0
    prev_matched_keys: set[str] = set()
    required_target_keys = set(TARGET_ORDER)
    max_rounds = 72

    for round_idx in range(max_rounds):
        batch = _scan_by_essfield_columns_once(scope, logger=logger, scope_label=scope_label)
        new_count = 0
        for item in batch:
            signature = _item_signature(item)
            if signature in seen_signatures:
                continue
            seen_signatures.add(signature)
            aggregated.append(item)
            new_count += 1

        if logger:
            logger.info(
                "essfield scroll round | scope=%s | round=%s | batch=%s | new=%s | total=%s",
                scope_label,
                round_idx + 1,
                len(batch),
                new_count,
                len(aggregated),
            )

        matched_keys_now = {item.matched_key for item in aggregated if item.matched_key}
        if matched_keys_now == prev_matched_keys and new_count == 0:
            stable_match_rounds += 1
        else:
            stable_match_rounds = 0
        prev_matched_keys = matched_keys_now

        if required_target_keys and required_target_keys.issubset(matched_keys_now):
            if logger:
                logger.info(
                    "essfield scan stop | scope=%s | round=%s | reason=matched_all_targets | matched=%s",
                    scope_label,
                    round_idx + 1,
                    sorted(matched_keys_now),
                )
            break

        if new_count == 0:
            no_new_rounds += 1
        else:
            no_new_rounds = 0

        moved = _scroll_scope_for_next_rows(scope, owner_page)
        if not moved:
            no_move_rounds += 1
            # Force a deeper virtualized scroll when normal scroll doesn't move.
            if no_new_rounds >= 2:
                forced = _force_scope_virtual_scroll(scope, owner_page, logger=logger, scope_label=scope_label)
                if forced:
                    moved = True
                    no_move_rounds = 0
        else:
            no_move_rounds = 0

        if no_new_rounds >= 4 and (no_move_rounds >= 3 or stable_match_rounds >= 3):
            if logger:
                logger.info(
                    "essfield scan stop | scope=%s | round=%s | reason=stagnated",
                    scope_label,
                    round_idx + 1,
                )
            break

    return aggregated


def _scan_by_essfield_columns_once(scope, logger=None, scope_label: str = "root") -> list[SavedReportItem]:
    name_cells = scope.locator("[essfield='definition.report_name']")
    name_count = name_cells.count()
    if logger:
        logger.info("essfield parser | scope=%s | report_name count=%s", scope_label, name_count)
    if name_count == 0:
        return []

    owner_cells = scope.locator("[essfield='owner_name'], [essfield='definition.owner_name']")
    creation_cells = scope.locator("[essfield='definition.creation_timestamp']")
    last_access_cells = scope.locator("[essfield='last_access_timestamp'], [essfield='definition.last_access_timestamp']")
    date_range_cells = scope.locator("[essfield='definition.date_range']")
    created_by_cells = scope.locator("[essfield='created_by'], [essfield='definition.created_by']")

    items: list[SavedReportItem] = []
    for i in range(name_count):
        name_cell = name_cells.nth(i)
        row = _as_row_container(name_cell)
        row_class = (row.get_attribute("class") or "").strip().lower() if row else ""
        if "particle-table-header" in row_class:
            if logger:
                logger.info("essfield row skipped (header class) index=%s scope=%s", i + 1, scope_label)
            continue

        visible_name = _extract_name_from_name_cell(name_cell)
        if not visible_name:
            continue

        normalized_name = normalize_report_name(visible_name)
        if normalized_name in {"reports", "saved reports"}:
            if logger:
                logger.info("essfield row skipped (header name) index=%s scope=%s", i + 1, scope_label)
            continue

        has_download_text = _has_download_text(name_cell)
        has_load_button = name_cell.locator("div.load-report-button").count() > 0
        has_download_icon = (
            has_load_button
            or name_cell.locator("img.report-icon").count() > 0
            or name_cell.locator("[aria-label*='Download']").count() > 0
        )

        key, ambiguous = match_target_key(normalized_name)
        matched_key = None if ambiguous else key
        inferred_type = _infer_type(
            normalized_name=normalized_name,
            has_download_text=has_download_text,
            has_load_button=has_load_button,
            matched_key=matched_key,
        )

        owner_text = _text_in_row_or_nth(row, owner_cells, i, "[essfield='owner_name'], [essfield='definition.owner_name']")
        creation_date = _text_in_row_or_nth(row, creation_cells, i, "[essfield='definition.creation_timestamp']")
        last_accessed = _text_in_row_or_nth(
            row,
            last_access_cells,
            i,
            "[essfield='last_access_timestamp'], [essfield='definition.last_access_timestamp']",
        )
        date_range = _text_in_row_or_nth(row, date_range_cells, i, "[essfield='definition.date_range']")
        created_by = _text_in_row_or_nth(row, created_by_cells, i, "[essfield='created_by'], [essfield='definition.created_by']")

        raw_row_text = _safe_inner_text(row) if row is not None else ""
        cells = _extract_cells(row) if row is not None else []
        if not _looks_like_saved_reports_row(raw_row_text, cells, visible_name):
            if logger:
                snippet = raw_row_text.replace("\n", " ")[:140]
                logger.info(
                    "essfield row skipped (not data row) index=%s scope=%s visible_name=%s snippet=%s",
                    i + 1,
                    scope_label,
                    visible_name,
                    snippet,
                )
            continue

        row_text = " | ".join(
            part
            for part in [visible_name, owner_text, creation_date, last_accessed, date_range, created_by]
            if part
        )
        item = SavedReportItem(
            visible_name=visible_name,
            normalized_name=normalized_name,
            inferred_type=inferred_type,
            row_text=row_text,
            matched_key=matched_key,
            owner_text=owner_text,
            created_by=created_by,
            creation_date=creation_date,
            last_accessed=last_accessed,
            date_range=date_range,
            has_download_text=has_download_text,
            has_download_icon=has_download_icon,
        )
        items.append(item)
        if logger:
            logger.info(
                "essfield row=%s | name=%s | type=%s | matched_key=%s | owner=%s | creation=%s | date_range=%s | created_by=%s",
                i + 1,
                item.visible_name,
                item.inferred_type,
                item.matched_key,
                item.owner_text,
                item.creation_date,
                item.date_range,
                item.created_by,
            )

    return items


def _item_signature(item: SavedReportItem) -> str:
    return "|".join(
        [
            item.normalized_name or "",
            item.owner_text or "",
            item.creation_date or "",
            item.created_by or "",
        ]
    )


def _parse_row_locators(rows, logger=None) -> list[SavedReportItem]:
    items: list[SavedReportItem] = []
    for index, row in enumerate(rows, start=1):
        try:
            row_text = _safe_inner_text(row)
            if not row_text:
                continue

            visible_name = _extract_visible_name(row, row_text)
            normalized_name = normalize_report_name(visible_name)
            has_download_text = _has_download_text(row)
            has_load_button = row.locator("div.load-report-button").count() > 0
            has_download_icon = (
                has_load_button
                or row.locator("img.report-icon").count() > 0
                or row.locator("[aria-label*='Download']").count() > 0
            )

            key, ambiguous = match_target_key(normalized_name)
            matched_key = None if ambiguous else key
            inferred_type = _infer_type(
                normalized_name=normalized_name,
                has_download_text=has_download_text,
                has_load_button=has_load_button,
                matched_key=matched_key,
            )

            owner_text, creation_date, last_accessed, date_range, created_by = _extract_columns(row)
            item = SavedReportItem(
                visible_name=visible_name,
                normalized_name=normalized_name,
                inferred_type=inferred_type,
                row_text=row_text,
                matched_key=matched_key,
                owner_text=owner_text,
                created_by=created_by,
                creation_date=creation_date,
                last_accessed=last_accessed,
                date_range=date_range,
                has_download_text=has_download_text,
                has_download_icon=has_download_icon,
            )
            items.append(item)
            if logger:
                logger.info(
                    "row=%s | name=%s | type=%s | download_text=%s | download_icon=%s | matched_key=%s | owner=%s | created_by=%s",
                    index,
                    item.visible_name,
                    item.inferred_type,
                    item.has_download_text,
                    item.has_download_icon,
                    item.matched_key,
                    item.owner_text,
                    item.created_by,
                )
        except Exception as exc:  # noqa: BLE001
            snippet = _safe_inner_text(row)[:180]
            if logger:
                logger.warning("row parse failed index=%s snippet=%s reason=%s", index, snippet, exc)
    return items


def match_targets(items: list[SavedReportItem], logger=None) -> dict[str, SavedReportItem]:
    """
    Return unique matches by target_key.
    If same key appears multiple times, mark ambiguous and exclude from output map.
    """
    mapping: dict[str, SavedReportItem] = {}
    ambiguous_keys: set[str] = set()

    for item in items:
        if not item.matched_key:
            continue
        key = item.matched_key
        if key in mapping:
            ambiguous_keys.add(key)
            mapping[key].matched_key = None
            item.matched_key = None
            continue
        mapping[key] = item

    for key in ambiguous_keys:
        mapping.pop(key, None)
        if logger:
            logger.warning("ambiguous target key detected: %s", key)
    return mapping


def _wait_for_table_area(root) -> None:
    root.wait_for_timeout(1200)
    try:
        root.get_by_text("Saved reports", exact=False).first.wait_for(
            state="visible",
            timeout=TABLE_SCAN_TIMEOUT_MS,
        )
    except Exception:  # noqa: BLE001
        # Saved reports label can be missing in some layouts.
        pass


def _set_show_rows_to_500(root, logger=None) -> None:
    try:
        if "/aw/reporteditor/view" in (root.url or "").lower():
            return
    except Exception:  # noqa: BLE001
        pass

    panels = root.locator(PANEL_SELECTOR)
    for idx in range(panels.count()):
        panel = panels.nth(idx)
        header = panel.locator(SAVED_REPORTS_HEADER_SELECTOR).first
        if header.count() == 0:
            header = panel.get_by_text("Saved reports", exact=False).first
            if header.count() == 0:
                continue

        button = _resolve_show_rows_button(panel)
        if button.count() == 0:
            if logger:
                logger.info("show rows button not found | panel=%s", idx)
            continue

        try:
            current_text = _safe_inner_text(button.locator("span.button-text").first)
            current_aria = (button.get_attribute("aria-label") or "").strip()
            if current_text == "500" or "500 selected" in current_aria:
                if logger:
                    logger.info("show rows already 500 | panel=%s", idx)
                return
        except Exception:  # noqa: BLE001
            pass

        try:
            button.click(timeout=2000)
        except Exception as exc:  # noqa: BLE001
            if logger:
                logger.info("show rows button click failed | panel=%s | reason=%s", idx, exc)
            continue

        owner_page = _get_owner_page(root)
        listbox = owner_page.locator(SHOW_ROWS_LISTBOX_SELECTOR).first
        if listbox.count() == 0:
            listbox = owner_page.locator("material-list[role='listbox']").first
        try:
            listbox.wait_for(state="visible", timeout=3500)
        except Exception as exc:  # noqa: BLE001
            if logger:
                logger.info("show rows listbox not visible | panel=%s | reason=%s", idx, exc)
            continue

        option = listbox.locator("[role='option']").filter(has_text="500").first
        if option.count() == 0:
            if logger:
                logger.info("show rows option 500 not found | panel=%s", idx)
            continue

        try:
            option.click(timeout=2000)
        except Exception as exc:  # noqa: BLE001
            if logger:
                logger.info("show rows option click failed | panel=%s | reason=%s", idx, exc)
            continue

        owner_page.wait_for_timeout(550)
        try:
            button_aria = (button.get_attribute("aria-label") or "").strip()
            button_text = _safe_inner_text(button.locator("span.button-text").first)
            success = button_text == "500" or "500 selected" in button_aria
            if logger:
                logger.info(
                    "show rows set | panel=%s | success=%s | text=%s | aria=%s",
                    idx,
                    success,
                    button_text,
                    button_aria,
                )
        except Exception:  # noqa: BLE001
            if logger:
                logger.info("show rows set | panel=%s | success=unknown", idx)
        return


def _resolve_show_rows_button(panel):
    # Primary selector (English aria label).
    button = panel.locator(SHOW_ROWS_BUTTON_SELECTOR).first
    if button.count() > 0:
        return button

    # Fallback for localized UIs: numeric row-count dropdown/button.
    candidates = panel.locator(SHOW_ROWS_BUTTON_FALLBACK_SELECTOR)
    count = candidates.count()
    for i in range(count):
        candidate = candidates.nth(i)
        text = _safe_inner_text(candidate.locator("span.button-text").first)
        if ROW_COUNT_REGEX.match(text):
            return candidate

    # Last fallback: any role=button containing the numeric value 500.
    return panel.locator("div[role='button']").filter(has_text="500").first


def _owner_page_from_scope(scope) -> Page:
    if isinstance(scope, Page):
        return scope
    if isinstance(scope, Frame):
        return scope.page
    try:
        return scope.page
    except Exception:  # noqa: BLE001
        raise RuntimeError("unable to resolve owner page from scope")


def _reset_scope_scroll(scope, owner_page: Page) -> None:
    try:
        scope.evaluate(
            """(el) => {
                try {
                    el.scrollTop = 0;
                    el.dispatchEvent(new Event('scroll', { bubbles: true }));
                } catch (e) {}
            }"""
        )
    except Exception:  # noqa: BLE001
        pass
    try:
        owner_page.evaluate("() => window.scrollTo(0, 0)")
    except Exception:  # noqa: BLE001
        pass
    owner_page.wait_for_timeout(180)


def _scroll_scope_for_next_rows(scope, owner_page: Page) -> bool:
    moved = False
    try:
        moved = bool(
            scope.evaluate(
                """(el) => {
                    try {
                        const candidates = [el, ...Array.from(el.querySelectorAll(
                            "div.main[role='region'], .particle-table-body, .particle-table-scroll-container, .cdk-virtual-scroll-viewport, [role='grid'], [role='rowgroup'], [class*='scroll']"
                        ))];
                        for (const node of candidates) {
                            if (!node) continue;
                            const sh = Number(node.scrollHeight || 0);
                            const ch = Number(node.clientHeight || 0);
                            if (sh <= ch + 2) continue;
                            const before = Number(node.scrollTop || 0);
                            const delta = Math.max(320, Math.floor((ch || 600) * 0.85));
                            node.scrollTop = Math.min(sh, before + delta);
                            node.dispatchEvent(new Event('scroll', { bubbles: true }));
                            const after = Number(node.scrollTop || 0);
                            if (Math.abs(after - before) > 2) {
                                return true;
                            }
                        }
                        return false;
                    } catch (e) {
                        return false;
                    }
                }"""
            )
        )
    except Exception:  # noqa: BLE001
        moved = False

    if not moved:
        try:
            moved = bool(
                owner_page.evaluate(
                    """() => {
                        const before = window.scrollY || 0;
                        window.scrollBy(0, 760);
                        return Math.abs((window.scrollY || 0) - before) > 2;
                    }"""
                )
            )
        except Exception:  # noqa: BLE001
            moved = False

    owner_page.wait_for_timeout(220)
    return moved


def _force_scope_virtual_scroll(scope, owner_page: Page, logger=None, scope_label: str = "root") -> bool:
    moved = False
    try:
        moved = bool(
            scope.evaluate(
                """(el) => {
                    try {
                        const candidates = [el, ...Array.from(el.querySelectorAll(
                            "div.main[role='region'], .particle-table-body, .particle-table-scroll-container, .cdk-virtual-scroll-viewport, [role='grid'], [role='rowgroup'], [class*='scroll']"
                        ))];
                        let changed = false;
                        for (const node of candidates) {
                            if (!node) continue;
                            const sh = Number(node.scrollHeight || 0);
                            const ch = Number(node.clientHeight || 0);
                            if (sh <= ch + 2) continue;
                            const before = Number(node.scrollTop || 0);
                            node.scrollTop = sh;
                            node.dispatchEvent(new Event('scroll', { bubbles: true }));
                            node.scrollTop = Math.max(0, sh - Math.max(320, Math.floor(ch * 0.75)));
                            node.dispatchEvent(new Event('scroll', { bubbles: true }));
                            const after = Number(node.scrollTop || 0);
                            if (Math.abs(after - before) > 2) {
                                changed = true;
                            }
                        }
                        return changed;
                    } catch (e) {
                        return false;
                    }
                }"""
            )
        )
    except Exception:  # noqa: BLE001
        moved = False

    if not moved:
        try:
            before_y = float(owner_page.evaluate("() => window.scrollY || 0"))
            owner_page.mouse.wheel(0, 1500)
            owner_page.wait_for_timeout(140)
            owner_page.mouse.wheel(0, 1500)
            after_y = float(owner_page.evaluate("() => window.scrollY || 0"))
            moved = abs(after_y - before_y) > 2
        except Exception:  # noqa: BLE001
            moved = False

    if logger:
        logger.info(
            "essfield forced scroll | scope=%s | moved=%s",
            scope_label,
            moved,
        )
    owner_page.wait_for_timeout(260)
    return moved


def _ensure_saved_reports_expanded(root, logger=None) -> None:
    """
    Expand the Saved reports expansion panel when collapsed.
    Google Ads often keeps table DOM hidden until this panel is expanded.
    """
    panel = root.locator(PANEL_SELECTOR)
    if panel.count() == 0:
        return

    for idx in range(panel.count()):
        item = panel.nth(idx)
        header = item.locator(SAVED_REPORTS_HEADER_SELECTOR).first
        if header.count() == 0:
            header = item.get_by_text("Saved reports", exact=False).first
            if header.count() == 0:
                continue
        region = item.locator(SAVED_REPORTS_REGION_SELECTOR).first

        try:
            header.scroll_into_view_if_needed(timeout=1000)
        except Exception:  # noqa: BLE001
            pass

        if _is_panel_expanded(header, region):
            if logger:
                logger.info("saved reports panel state | expanded=true")
            continue

        if logger:
            logger.info("saved reports panel state | expanded=false | action=expand_click")

        clicked = False
        for click_target in (
            header,
            item.locator("material-icon.expand-button").first,
            item.get_by_text("Saved reports", exact=False).first,
        ):
            if click_target.count() == 0:
                continue
            try:
                click_target.click(timeout=1500)
                clicked = True
                break
            except Exception:  # noqa: BLE001
                continue

        if not clicked:
            if logger:
                logger.warning("saved reports panel expand click failed")
            continue

        # Wait for aria-expanded/aria-hidden to flip.
        elapsed = 0
        timeout_ms = 6000
        while elapsed < timeout_ms:
            if _is_panel_expanded(header, region):
                if logger:
                    logger.info("saved reports panel expand success")
                break
            root.wait_for_timeout(400)
            elapsed += 400


def _is_panel_expanded(header, region) -> bool:
    # Prefer region visibility signal when available.
    try:
        if region.count() > 0:
            hidden_attr = (region.get_attribute("aria-hidden") or "").strip().lower()
            if hidden_attr == "false":
                return True
            if hidden_attr == "true":
                return False
    except Exception:  # noqa: BLE001
        pass

    try:
        expanded_attr = (header.get_attribute("aria-expanded") or "").strip().lower()
        if expanded_attr == "true":
            return True
    except Exception:  # noqa: BLE001
        pass

    return False


def _resolve_rows(scope, logger=None, scope_label: str = "root"):
    selectors = SCOPED_ROW_SELECTORS if scope_label != "root" else GLOBAL_ROW_SELECTORS
    for selector in selectors:
        locator = scope.locator(selector)
        count = locator.count()
        if logger:
            logger.info(
                "saved reports selector count | scope=%s | selector=%s | count=%s",
                scope_label,
                selector,
                count,
            )
        if count == 0:
            continue
        rows = [locator.nth(i) for i in range(count)]
        filtered = []
        for row in rows:
            row_text = _safe_inner_text(row)
            if len(row_text) <= 3:
                continue
            cells = _extract_cells(row)
            visible_name = _extract_visible_name(row, row_text)
            accepted = _looks_like_saved_reports_row(row_text, cells, visible_name)
            if accepted:
                filtered.append(row)
            elif logger:
                snippet = row_text.replace("\n", " ")[:140]
                logger.info(
                    "saved reports row filtered out | visible_name=%s | snippet=%s",
                    visible_name,
                    snippet,
                )
        if filtered:
            return filtered
    return []


def _extract_visible_name(row, row_text: str) -> str:
    first_cell_name = _extract_first_cell_name(row)
    if first_cell_name:
        return first_cell_name

    candidates = [
        "span.report-name-text",
        "div.load-report-button span.report-name-text",
        "report-name span",
        "report-name",
    ]
    for selector in candidates:
        loc = row.locator(selector)
        if loc.count() == 0:
            continue
        text = _safe_inner_text(loc.first)
        if text:
            return text.splitlines()[0].strip()

    lines = [line.strip() for line in row_text.splitlines() if line.strip()]
    return lines[0] if lines else "unknown"


def _extract_name_from_name_cell(name_cell) -> str:
    selectors = (
        ".report-name-text",
        "span.ess-cell-link.report-name-text",
        "span.report-name-text",
        "div.report-name-text",
        "report-name span",
        "a",
    )
    for selector in selectors:
        loc = name_cell.locator(selector)
        if loc.count() == 0:
            continue
        text = _safe_inner_text(loc.first)
        cleaned = _clean_name_text(text)
        if cleaned:
            return cleaned
    return _clean_name_text(_safe_inner_text(name_cell))


def _extract_first_cell_name(row) -> str:
    for selector in ("td", "[role='gridcell']", "ess-cell"):
        first_cell = row.locator(selector).first
        if first_cell.count() == 0:
            continue

        for inner_selector in (
            ".report-name-text",
            "a",
            "span.report-name-text",
            "div.report-name-text",
            "div.load-report-button span",
            "span.name",
        ):
            loc = first_cell.locator(inner_selector)
            if loc.count() == 0:
                continue
            text = _safe_inner_text(loc.first)
            cleaned = _clean_name_text(text)
            if cleaned:
                return cleaned

        fallback = _clean_name_text(_safe_inner_text(first_cell))
        if fallback:
            return fallback
    return ""


def _clean_name_text(text: str) -> str:
    if not text:
        return ""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    lines = [line for line in lines if line.lower() != "download"]
    if not lines:
        return ""
    return lines[0]


def _nth_text(locator, idx: int) -> str | None:
    try:
        if locator.count() <= idx:
            return None
        value = _safe_inner_text(locator.nth(idx))
        return value or None
    except Exception:  # noqa: BLE001
        return None


def _has_download_text(row) -> bool:
    try:
        if row.get_by_text("Download", exact=True).count() > 0:
            return True
    except Exception:  # noqa: BLE001
        pass
    return row.locator("span.text-and-action").filter(has_text="Download").count() > 0


def _infer_type(
    normalized_name: str,
    has_download_text: bool,
    has_load_button: bool,
    matched_key: str | None,
) -> str:
    # Report prefixes are authoritative even if row has load/download controls.
    if matched_key in REPORT_KEYS:
        return "report"
    if normalized_name.startswith("bcg_demographics") or normalized_name.startswith("bcg_placements"):
        return "report"
    if has_download_text:
        return "view"
    if has_load_button:
        return "view"
    return "unknown"


def _extract_columns(row) -> tuple[str | None, str | None, str | None, str | None, str | None]:
    cells = _extract_cells(row)

    if not cells:
        return None, None, None, None, None

    owner = cells[1] if len(cells) > 1 else None
    creation_date = cells[2] if len(cells) > 2 else None
    last_accessed = cells[3] if len(cells) > 3 else None
    date_range = cells[4] if len(cells) > 4 else None
    created_by = cells[5] if len(cells) > 5 else None
    return (
        owner or None,
        creation_date or None,
        last_accessed or None,
        date_range or None,
        created_by or None,
    )


def _extract_cells(row) -> list[str]:
    for selector in ("[essfield]", "ess-cell", "[role='gridcell']", "td"):
        loc = row.locator(selector)
        if loc.count() >= 4:
            return [_safe_inner_text(loc.nth(i)) for i in range(loc.count())]
    return []


def _looks_like_saved_reports_row(row_text: str, cells: list[str], visible_name: str = "") -> bool:
    lower = row_text.lower()
    if "reports" in lower and "owner" in lower and "creation date" in lower:
        return False
    if "campaign status" in lower or "ad group status" in lower:
        return False

    normalized_name = normalize_report_name(visible_name)
    if normalized_name in {"", "reports", "saved reports"}:
        return False

    # If the row carries explicit report-name markup, keep it.
    if "bcg" in normalized_name or "_" in normalized_name:
        return True
    if find_matching_keys(normalized_name):
        return True

    if cells:
        # Header rows usually have exact column labels without CID/email/date payload.
        joined = " ".join(cells).lower()
        if "creation date" in joined and "last accessed" in joined:
            return False

        # Saved reports rows are expected to have at least one structured signal.
        has_date_signal = any(MONTH_REGEX.search(c or "") for c in cells[2:4])
        has_owner_cid = CID_REGEX.search(cells[1] if len(cells) > 1 else "") is not None
        has_creator_email = EMAIL_REGEX.search(cells[5] if len(cells) > 5 else "") is not None
        has_custom_range = len(cells) > 4 and bool(cells[4].strip())
        if has_date_signal or has_owner_cid or has_creator_email or has_custom_range:
            return True

    # Fallback text-based checks
    if CID_REGEX.search(row_text) and (MONTH_REGEX.search(row_text) or EMAIL_REGEX.search(row_text)):
        return True
    # Last resort: name-like row with at least 3 chars and not obvious header.
    if len(normalized_name) >= 3 and "date range" not in normalized_name and "created by" not in normalized_name:
        return True
    return False


def _scan_by_name_elements(scope, logger=None, scope_label: str = "root") -> list[SavedReportItem]:
    name_locator = scope.locator(
        ".report-name-text, "
        "span.report-name-text, "
        "span.ess-cell-link.report-name-text, "
        "report-name span, "
        "report-name .create-similar-report-name-angular span, "
        "report-name .create-similar-report-name-angular div.report-name-text, "
        "div.create-similar-report-name-angular span.report-name-text"
    )
    count = name_locator.count()
    if logger:
        logger.info("name-element fallback count=%s scope=%s", count, scope_label)
    if count == 0:
        return []

    items: list[SavedReportItem] = []
    seen_names: set[str] = set()
    for i in range(count):
        name_el = name_locator.nth(i)
        visible_name = _clean_name_text(_safe_inner_text(name_el))
        normalized_name = normalize_report_name(visible_name)
        if normalized_name in {"", "reports", "saved reports"}:
            continue
        if normalized_name in seen_names:
            continue
        seen_names.add(normalized_name)

        key, ambiguous = match_target_key(normalized_name)
        matched_key = None if ambiguous else key
        inferred_type = _infer_type(
            normalized_name=normalized_name,
            has_download_text=False,
            has_load_button=False,
            matched_key=matched_key,
        )
        item = SavedReportItem(
            visible_name=visible_name,
            normalized_name=normalized_name,
            inferred_type=inferred_type,
            row_text=visible_name,
            matched_key=matched_key,
            owner_text=None,
            created_by=None,
            creation_date=None,
            last_accessed=None,
            date_range=None,
            has_download_text=False,
            has_download_icon=False,
        )
        items.append(item)
        if logger:
            logger.info(
                "fallback row=%s | name=%s | matched_key=%s | type=%s",
                i + 1,
                item.visible_name,
                item.matched_key,
                item.inferred_type,
            )
    return items


def _scan_by_saved_reports_text(root, logger=None) -> list[SavedReportItem]:
    """
    Last-resort parser: extract BCG-like names from Saved reports panel text.
    Used when table-like rows are not discoverable due dynamic rendering variants.
    """
    collected_names: list[str] = []
    panel_count = 0
    text_sources: list[tuple[str, object]] = []

    panels = root.locator(PANEL_SELECTOR)
    panel_count = panels.count()
    for i in range(panel_count):
        panel = panels.nth(i)
        header = panel.locator(SAVED_REPORTS_HEADER_SELECTOR).first
        if header.count() == 0:
            header = panel.get_by_text("Saved reports", exact=False).first
            if header.count() == 0:
                continue
        region = panel.locator(SAVED_REPORTS_REGION_SELECTOR).first
        if region.count() > 0:
            text_sources.append((f"panel[{i}]/region", region))
        text_sources.append((f"panel[{i}]", panel))

    # root fallback when panel-based probing produced nothing.
    if not text_sources:
        text_sources.append(("root", root))

    if logger:
        logger.info(
            "saved reports text fallback sources=%s panel_candidates=%s",
            len(text_sources),
            panel_count,
        )

    for source_label, source in text_sources:
        text = _safe_inner_text(source)
        if logger:
            logger.info(
                "saved reports text fallback source=%s text_len=%s",
                source_label,
                len(text),
            )
        if not text or "saved reports" not in text.lower():
            continue
        names = _extract_bcg_names_from_text(text)
        if logger and not names:
            snippet = " ".join(text.split())[:240]
            logger.info(
                "saved reports text fallback no target-like names | source=%s | snippet=%s",
                source_label,
                snippet,
            )
        collected_names.extend(names)

    unique: list[str] = []
    seen: set[str] = set()
    for name in collected_names:
        normalized = normalize_report_name(name)
        if normalized in seen:
            continue
        seen.add(normalized)
        unique.append(name)

    items: list[SavedReportItem] = []
    for idx, name in enumerate(unique, start=1):
        normalized_name = normalize_report_name(name)
        key, ambiguous = match_target_key(normalized_name)
        matched_key = None if ambiguous else key
        inferred_type = _infer_type(
            normalized_name=normalized_name,
            has_download_text=False,
            has_load_button=False,
            matched_key=matched_key,
        )
        item = SavedReportItem(
            visible_name=name,
            normalized_name=normalized_name,
            inferred_type=inferred_type,
            row_text=name,
            matched_key=matched_key,
            owner_text=None,
            created_by=None,
            creation_date=None,
            last_accessed=None,
            date_range=None,
            has_download_text=False,
            has_download_icon=False,
        )
        items.append(item)
        if logger:
            logger.info(
                "text fallback row=%s | name=%s | matched_key=%s",
                idx,
                item.visible_name,
                item.matched_key,
            )

    if logger:
        logger.info("saved reports text fallback parsed count=%s", len(items))
    return items


def _extract_bcg_names_from_text(text: str) -> list[str]:
    names: list[str] = []
    for raw_line in text.splitlines():
        line = " ".join(raw_line.split()).strip()
        if not line:
            continue
        if "campaign status" in line.lower() or "ad group status" in line.lower():
            continue
        if "saved reports" in line.lower():
            continue
        if "reports owner creation date" in line.lower():
            continue
        match = BCG_LINE_REGEX.search(line)
        if not match:
            continue
        candidate = match.group(0).strip(" -|")
        # trim trailing non-name tokens from flattened text lines
        candidate = re.sub(r"\s+(download|owner|creation|last|date|created|schedule)\b.*$", "", candidate, flags=re.IGNORECASE)
        candidate = candidate.strip()
        if len(candidate) >= 4:
            names.append(candidate)
    return names


def _as_row_container(locator):
    try:
        row = locator.locator("xpath=ancestor::*[@role='row'][1]")
        if row.count() > 0:
            return row.first
    except Exception:  # noqa: BLE001
        pass
    return None


def _text_in_row_or_nth(row, global_locator, idx: int, row_selector: str) -> str | None:
    try:
        if row is not None:
            local = row.locator(row_selector)
            if local.count() > 0:
                value = _safe_inner_text(local.first)
                if value:
                    return value
    except Exception:  # noqa: BLE001
        pass
    return _nth_text(global_locator, idx)


def _safe_inner_text(locator) -> str:
    try:
        return (locator.inner_text(timeout=1000) or "").strip()
    except Exception:  # noqa: BLE001
        return ""
