"""Google Ads CSV -> unified Excel workbook transformer."""

from __future__ import annotations

import csv
import io
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from openpyxl import Workbook

from .models import AdsAccount, DownloadResult
from .targets import TARGET_DISPLAY_NAMES, TARGET_ORDER
from .utils import sanitize_filename


@dataclass(frozen=True, slots=True)
class TemplateSheetConfig:
    sheet_name: str
    headers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CsvProcessSummary:
    target_key: str
    target_display: str
    sheet_name: str
    csv_path: str | None
    csv_rows: int
    written_rows: int
    mapped_columns: tuple[str, ...]
    missing_columns: tuple[str, ...]
    status: str
    reason: str | None = None


GOOGLE_TEMPLATE_SHEETS: tuple[TemplateSheetConfig, ...] = (
    TemplateSheetConfig(
        sheet_name="Statistics",
        headers=(
            "Day",
            "Campaign ID",
            "Campaign Name",
            "Campaign Status",
            "Ad Group ID",
            "Ad Group Name",
            "Ad ID",
            "Ad Name",
            "Ad Status",
            "Status Reasons",
            "Currency Code",
            "Cost",
            "Impressions",
            "Clicks",
            "Conversions",
            "Unique Users",
            "Avg. Impr. Freq. / User",
            "Viewable Impr.",
            "Video Played to 25%",
            "Video Played to 50%",
            "Video Played to 75%",
            "Video Played to 100%",
            "Purchase Conversions",
            "Max. CPV",
            "TrueView Target CPV",
            "Max. CPM",
            "Target CPA",
            "Target CPM",
            "Fixed CPM",
            "Objective",
            "Bid Strategy Type",
            "Call to Action Text",
            "Campaign Budget",
        ),
    ),
    TemplateSheetConfig(
        sheet_name="Campaign Ad Group",
        headers=(
            "Day",
            "Campaign ID",
            "Campaign Name",
            "Campaign Status",
            "Budget",
            "Budget Name",
            "Budget Type",
            "Currency Code",
        ),
    ),
    TemplateSheetConfig(
        sheet_name="Ad",
        headers=(
            "Day",
            "Campaign ID",
            "Campaign Name",
            "Ad Group ID",
            "Ad Group Name",
            "Ad ID",
            "Ad Name",
            "Ad Status",
            "Status Reasons",
            "Currency Code",
            "Cost",
            "Impressions",
            "Clicks",
            "Conversions",
        ),
    ),
    TemplateSheetConfig(
        sheet_name="Demographics",
        headers=(
            "Day",
            "Campaign ID",
            "Campaign Name",
            "Ad Group ID",
            "Ad Group Name",
            "Age",
            "Gender",
            "Currency Code",
            "Cost",
            "Impressions",
            "Clicks",
            "Conversions",
        ),
    ),
    TemplateSheetConfig(
        sheet_name="Device",
        headers=(
            "Day",
            "Campaign ID",
            "Campaign Name",
            "Ad Group ID",
            "Ad Group Name",
            "Device",
            "Currency Code",
            "Cost",
            "Impressions",
            "Clicks",
            "Conversions",
        ),
    ),
    TemplateSheetConfig(
        sheet_name="Hour of Day",
        headers=(
            "Day",
            "Hour of the Day",
            "Campaign ID",
            "Campaign Name",
            "Ad Group ID",
            "Ad Group Name",
            "Currency Code",
            "Cost",
            "Impressions",
            "Clicks",
            "Conversions",
        ),
    ),
    TemplateSheetConfig(
        sheet_name="Placement",
        headers=(
            "Day",
            "Campaign ID",
            "Campaign Name",
            "Ad Group ID",
            "Ad Group Name",
            "Placement (Group)",
            "Currency Code",
            "Cost",
            "Impressions",
            "Clicks",
            "Conversions",
        ),
    ),
    TemplateSheetConfig(
        sheet_name="Ad Format",
        headers=(
            "Ad Group ID",
            "Ad Group Name",
            "Ad Format",
        ),
    ),
)

TARGET_TO_SHEET: dict[str, str] = {
    "campaign_ad_group": "Campaign Ad Group",
    "ad": "Ad",
    "demographics": "Demographics",
    "device": "Device",
    "hourofday": "Hour of Day",
    "placements": "Placement",
    "adformat": "Ad Format",
}

TARGET_TO_HEADERS: dict[str, tuple[str, ...]] = {
    cfg.sheet_name: cfg.headers for cfg in GOOGLE_TEMPLATE_SHEETS
}

SCIENTIFIC_NOTATION_REGEX = re.compile(r"^[+-]?(?:\d+(?:\.\d+)?|\.\d+)[eE][+-]?\d+$")
SCIENTIFIC_WARNING_SAMPLE_LIMIT = 5
ILLEGAL_EXCEL_CHAR_REGEX = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F]")
HEADER_ALIAS_BY_TARGET_TOKEN: dict[str, tuple[str, ...]] = {
    "campaignname": ("campaign",),
    "adgroupname": ("adgroup",),
    "impressions": ("impr",),
}


def build_google_template_workbook() -> Workbook:
    workbook = Workbook()
    default_sheet = workbook.active
    workbook.remove(default_sheet)
    for config in GOOGLE_TEMPLATE_SHEETS:
        ws = workbook.create_sheet(config.sheet_name)
        for col_idx, header in enumerate(config.headers, start=1):
            ws.cell(row=1, column=col_idx, value=header)
            # Keep row 2 present to mirror the existing pilot template layout.
            ws.cell(row=2, column=col_idx, value="")
    return workbook


def create_unified_workbook_for_account(
    *,
    account: AdsAccount,
    download_results: list[DownloadResult],
    output_dir: Path,
    csv_dir: Path | None = None,
    logger=None,
) -> tuple[Path, list[CsvProcessSummary]]:
    workbook = build_google_template_workbook()
    results_by_key = {result.target_key: result for result in download_results}
    summaries: list[CsvProcessSummary] = []
    csv_source_dir = Path(csv_dir or output_dir).expanduser().resolve()

    for target_key in TARGET_ORDER:
        if target_key not in TARGET_TO_SHEET:
            continue

        sheet_name = TARGET_TO_SHEET[target_key]
        target_display = TARGET_DISPLAY_NAMES.get(target_key, target_key)
        result = results_by_key.get(target_key)
        summary = _write_target_to_sheet(
            workbook=workbook,
            target_key=target_key,
            target_display=target_display,
            sheet_name=sheet_name,
            result=result,
            csv_source_dir=csv_source_dir,
            logger=logger,
        )
        summaries.append(summary)

    run_date = datetime.now().strftime("%Y%m%d")
    output_name = sanitize_filename(
        f"{run_date}_{account.name}_{account.cid_digits}_Google_Unified.xlsx"
    )
    output_path = output_dir / output_name
    workbook.save(output_path)
    return output_path, summaries


def open_file_in_explorer(file_path: Path, logger=None) -> bool:
    target = Path(file_path).resolve()
    try:
        subprocess.Popen(["explorer", f"/select,{target}"])  # noqa: S603
        return True
    except Exception as exc:  # noqa: BLE001
        if logger:
            logger.warning("explorer select failed path=%s reason=%s", target, exc)

    try:
        subprocess.Popen(["explorer", str(target.parent)])  # noqa: S603
        return True
    except Exception as exc:  # noqa: BLE001
        if logger:
            logger.warning("explorer open folder failed path=%s reason=%s", target.parent, exc)
    return False


def summaries_as_rows(summaries: list[CsvProcessSummary], account_label: str) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for item in summaries:
        rows.append(
            {
                "account": account_label,
                "target_key": item.target_key,
                "target_display": item.target_display,
                "sheet_name": item.sheet_name,
                "csv_path": item.csv_path or "",
                "csv_rows": item.csv_rows,
                "written_rows": item.written_rows,
                "mapped_columns": len(item.mapped_columns),
                "missing_columns": len(item.missing_columns),
                "status": item.status,
                "reason": item.reason or "",
            }
        )
    return rows


def _write_target_to_sheet(
    *,
    workbook: Workbook,
    target_key: str,
    target_display: str,
    sheet_name: str,
    result: DownloadResult | None,
    csv_source_dir: Path,
    logger=None,
) -> CsvProcessSummary:
    headers = TARGET_TO_HEADERS.get(sheet_name, tuple())
    worksheet = workbook[sheet_name]

    if not result:
        return CsvProcessSummary(
            target_key=target_key,
            target_display=target_display,
            sheet_name=sheet_name,
            csv_path=None,
            csv_rows=0,
            written_rows=0,
            mapped_columns=tuple(),
            missing_columns=headers,
            status="failed",
            reason="download result missing",
        )

    if not result.success or not result.filename:
        return CsvProcessSummary(
            target_key=target_key,
            target_display=target_display,
            sheet_name=sheet_name,
            csv_path=None,
            csv_rows=0,
            written_rows=0,
            mapped_columns=tuple(),
            missing_columns=headers,
            status="failed",
            reason=result.reason or "download failed",
        )

    csv_path = (csv_source_dir / result.filename).resolve()
    if not csv_path.exists():
        return CsvProcessSummary(
            target_key=target_key,
            target_display=target_display,
            sheet_name=sheet_name,
            csv_path=str(csv_path),
            csv_rows=0,
            written_rows=0,
            mapped_columns=tuple(),
            missing_columns=headers,
            status="failed",
            reason="csv file not found",
        )

    try:
        csv_rows, source_headers = _read_csv_dict_rows(csv_path)
    except Exception as exc:  # noqa: BLE001
        if logger:
            logger.warning("csv read failed path=%s reason=%s", csv_path, exc)
        return CsvProcessSummary(
            target_key=target_key,
            target_display=target_display,
            sheet_name=sheet_name,
            csv_path=str(csv_path),
            csv_rows=0,
            written_rows=0,
            mapped_columns=tuple(),
            missing_columns=headers,
            status="failed",
            reason=f"csv read failed: {exc}",
        )

    _warn_scientific_notation_values(
        csv_path=csv_path,
        source_headers=source_headers,
        csv_rows=csv_rows,
        logger=logger,
    )

    source_lookup = _build_source_header_lookup(source_headers)
    source_token_lookup = _build_source_header_token_lookup(source_headers)
    mapped_columns: list[str] = []
    missing_columns: list[str] = []
    mapped_source_by_target_header: dict[str, str] = {}
    for header in headers:
        source_header = _resolve_source_header(
            source_lookup=source_lookup,
            source_token_lookup=source_token_lookup,
            target_header=header,
        )
        if source_header:
            mapped_columns.append(header)
            mapped_source_by_target_header[header] = source_header
        else:
            missing_columns.append(header)
            mapped_source_by_target_header[header] = ""

    written_rows = _write_csv_rows_to_sheet(
        worksheet=worksheet,
        headers=headers,
        csv_rows=csv_rows,
        mapped_source_by_target_header=mapped_source_by_target_header,
    )

    return CsvProcessSummary(
        target_key=target_key,
        target_display=target_display,
        sheet_name=sheet_name,
        csv_path=str(csv_path),
        csv_rows=len(csv_rows),
        written_rows=written_rows,
        mapped_columns=tuple(mapped_columns),
        missing_columns=tuple(missing_columns),
        status="excel_written",
    )


def _read_csv_dict_rows(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    encodings = ("utf-8-sig", "utf-8", "cp949", "utf-16", "utf-16le", "utf-16be")
    last_error: Exception | None = None
    for encoding in encodings:
        try:
            text = path.read_text(encoding=encoding)
            candidates = _build_delimiter_candidates(text)
            primary = candidates[0] if candidates else ","
            rows, headers = _parse_csv_text(text, delimiter=primary)
            best_rows = rows
            best_headers = headers
            best_score = _score_parsed_csv(headers=headers, rows=rows)

            # Fallback only when primary parse looks suspiciously under-split.
            if len(best_headers) <= 1:
                for delimiter in candidates[1:]:
                    alt_rows, alt_headers = _parse_csv_text(text, delimiter=delimiter)
                    alt_score = _score_parsed_csv(headers=alt_headers, rows=alt_rows)
                    if alt_score > best_score:
                        best_rows = alt_rows
                        best_headers = alt_headers
                        best_score = alt_score

            return best_rows, best_headers
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            continue
    raise RuntimeError(f"unable to parse csv using {encodings}: {last_error}")


def _parse_csv_text(text: str, *, delimiter: str) -> tuple[list[dict[str, str]], list[str]]:
    header_row_index = _detect_header_row_index(text=text, delimiter=delimiter, scan_limit=60)
    rows: list[dict[str, str]] = []
    with io.StringIO(text) as fp:
        reader = csv.reader(fp, delimiter=delimiter)

        for _ in range(header_row_index):
            next(reader, None)

        header_row = next(reader, [])
        headers = [str(name or "").strip() for name in header_row]

        for raw_row in reader:
            if not raw_row:
                continue
            normalized = _build_row_dict(headers=headers, raw_row=raw_row)
            if _is_row_completely_blank(normalized):
                continue
            rows.append(normalized)
    return rows, headers


def _build_row_dict(*, headers: list[str], raw_row: list[str]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for idx, header in enumerate(headers):
        key = str(header or "").strip()
        value = str(raw_row[idx] if idx < len(raw_row) else "").strip()
        normalized[key] = value
    return normalized


def _detect_header_row_index(*, text: str, delimiter: str, scan_limit: int) -> int:
    preview_rows: list[list[str]] = []
    with io.StringIO(text) as fp:
        reader = csv.reader(fp, delimiter=delimiter)
        for row_idx, row in enumerate(reader):
            preview_rows.append([str(cell or "").strip() for cell in row])
            if row_idx + 1 >= scan_limit:
                break

    best_index = 0
    best_score = -1
    for row_idx, row in enumerate(preview_rows):
        score = _score_header_candidate(row)
        if score > best_score:
            best_index = row_idx
            best_score = score
    return best_index


def _score_header_candidate(row: list[str]) -> int:
    non_empty_cells = [cell for cell in row if cell]
    if len(non_empty_cells) < 2:
        return -1

    tokenized = {_header_token(cell) for cell in non_empty_cells}
    anchors = (
        "day",
        "campaign",
        "campaignid",
        "adgroup",
        "adgroupid",
        "adid",
        "cost",
        "impr",
        "clicks",
        "conversions",
        "currencycode",
        "placementgroup",
        "houroftheday",
        "adformat",
    )
    anchor_hits = sum(1 for anchor in anchors if anchor in tokenized)
    return (len(non_empty_cells) * 10) + (anchor_hits * 25)


def _header_token(value: str) -> str:
    return "".join(char for char in str(value or "").strip().lower() if char.isalnum())


def _build_delimiter_candidates(text: str) -> tuple[str, ...]:
    candidates: list[str] = []
    sample = _preview_csv_text(text, max_lines=60, max_chars=32768)
    sniffed = _sniff_delimiter(sample)
    if sniffed:
        candidates.append(sniffed)
    for delimiter in ("\t", ",", ";", "|"):
        if delimiter not in candidates:
            candidates.append(delimiter)
    return tuple(candidates)


def _sniff_delimiter(sample: str) -> str | None:
    if not sample.strip():
        return None
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters="\t,;|")
        return str(dialect.delimiter)
    except Exception:  # noqa: BLE001
        return _heuristic_delimiter(sample)


def _heuristic_delimiter(sample: str) -> str | None:
    lines = [line for line in sample.splitlines() if line.strip()]
    if not lines:
        return None
    best_delimiter: str | None = None
    best_score = -1
    for delimiter in ("\t", ",", ";", "|"):
        counts = [line.count(delimiter) for line in lines[:20]]
        non_zero = [count for count in counts if count > 0]
        if not non_zero:
            continue
        score = (sum(non_zero) * 10) - (max(non_zero) - min(non_zero))
        if score > best_score:
            best_score = score
            best_delimiter = delimiter
    return best_delimiter


def _preview_csv_text(text: str, *, max_lines: int, max_chars: int) -> str:
    if len(text) <= max_chars:
        return "\n".join(text.splitlines()[:max_lines])

    preview = text[:max_chars]
    lines = preview.splitlines()
    return "\n".join(lines[:max_lines])


def _score_parsed_csv(*, headers: list[str], rows: list[dict[str, str]]) -> int:
    header_count = sum(1 for header in headers if str(header or "").strip())
    if header_count == 0:
        return -1
    row_count = len(rows)
    # Prefer parses with more columns first, then row volume.
    return (header_count * 100000) + min(row_count, 99999)


def _write_csv_rows_to_sheet(
    *,
    worksheet,
    headers: tuple[str, ...],
    csv_rows: list[dict[str, str]],
    mapped_source_by_target_header: dict[str, str],
) -> int:
    for row_idx, csv_row in enumerate(csv_rows, start=2):
        for col_idx, header in enumerate(headers, start=1):
            source_header = mapped_source_by_target_header.get(header, "")
            value = _as_trimmed_string(csv_row.get(source_header, "")) if source_header else ""
            worksheet.cell(row=row_idx, column=col_idx, value=value)
    return len(csv_rows)


def _as_trimmed_string(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return ILLEGAL_EXCEL_CHAR_REGEX.sub("", text)


def _is_row_completely_blank(row: dict[str, str]) -> bool:
    for value in row.values():
        if str(value or "").strip():
            return False
    return True


def _warn_scientific_notation_values(
    *,
    csv_path: Path,
    source_headers: list[str],
    csv_rows: list[dict[str, str]],
    logger=None,
) -> None:
    if logger is None:
        return

    id_headers = [header for header in source_headers if _is_id_like_header(header)]
    if not id_headers:
        return

    detections: list[tuple[int, str, str]] = []
    for row_idx, row in enumerate(csv_rows, start=2):
        for header in id_headers:
            raw = _as_trimmed_string(row.get(header, ""))
            if not raw:
                continue
            if SCIENTIFIC_NOTATION_REGEX.match(raw):
                detections.append((row_idx, header, raw))

    if not detections:
        return

    sample = ", ".join(
        [
            f"r{row_idx}:{header}={value}"
            for row_idx, header, value in detections[:SCIENTIFIC_WARNING_SAMPLE_LIMIT]
        ]
    )
    logger.warning(
        "scientific notation detected in ID-like fields | csv=%s | count=%s | sample=%s",
        csv_path,
        len(detections),
        sample,
    )


def _is_id_like_header(header: str) -> bool:
    normalized = _normalize_header(header)
    return normalized.endswith("id")


def _build_source_header_lookup(headers: list[str]) -> dict[str, str]:
    lookup: dict[str, str] = {}
    for header in headers:
        key = _normalize_header(header)
        if key and key not in lookup:
            lookup[key] = header
    return lookup


def _build_source_header_token_lookup(headers: list[str]) -> dict[str, str]:
    lookup: dict[str, str] = {}
    for header in headers:
        token = _header_token(header)
        if token and token not in lookup:
            lookup[token] = header
    return lookup


def _resolve_source_header(
    *,
    source_lookup: dict[str, str],
    source_token_lookup: dict[str, str],
    target_header: str,
) -> str:
    exact = source_lookup.get(_normalize_header(target_header), "")
    if exact:
        return exact

    target_token = _header_token(target_header)
    aliases = HEADER_ALIAS_BY_TARGET_TOKEN.get(target_token, tuple())
    for alias in aliases:
        matched = source_token_lookup.get(alias, "")
        if matched:
            return matched
    return ""


def _normalize_header(value: str) -> str:
    return "".join(str(value or "").strip().lower().split())
