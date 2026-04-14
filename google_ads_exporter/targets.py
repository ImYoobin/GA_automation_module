"""Target naming rules and matching logic."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from typing import Iterable


DEFAULT_TARGET_PREFIXES: dict[str, list[str]] = {
    "placements": ["bcg_auto_placements"],
    "demographics": ["bcg_auto_demographics"],
    "campaign_ad_group": ["bcg_auto_campaignadgroup"],
    "adformat": ["bcg_auto_adformat"],
    "hourofday": ["bcg_auto_hourofday"],
    "ad": ["bcg_auto_ad"],
    "device": ["bcg_auto_device", "bcg_auto_devices"],
}

DEFAULT_TARGET_ORDER: list[str] = [
    "placements",
    "demographics",
    "campaign_ad_group",
    "adformat",
    "hourofday",
    "ad",
    "device",
]

DEFAULT_TARGET_DISPLAY_NAMES: dict[str, str] = {
    "placements": "Placement",
    "demographics": "Demographics",
    "campaign_ad_group": "Campaign Ad Group",
    "adformat": "Ad format",
    "hourofday": "Hour of Day",
    "ad": "Ad",
    "device": "Device",
}

DEFAULT_REPORT_KEYS = {"placements", "demographics"}

# Mutable globals intentionally kept for runtime reconfiguration.
TARGET_PREFIXES: dict[str, list[str]] = {
    key: list(prefixes) for key, prefixes in DEFAULT_TARGET_PREFIXES.items()
}
TARGET_ORDER: list[str] = list(DEFAULT_TARGET_ORDER)
TARGET_DISPLAY_NAMES: dict[str, str] = dict(DEFAULT_TARGET_DISPLAY_NAMES)
REPORT_KEYS: set[str] = set(DEFAULT_REPORT_KEYS)

WHITESPACE_REGEX = re.compile(r"\s+")
NON_ALNUM_REGEX = re.compile(r"[^a-z0-9]+")


class TargetMappingError(ValueError):
    """Raised when target mapping JSON is invalid."""


def _as_string(value: Any) -> str:
    return str(value or "").strip()


def _as_str_list(value: Any) -> list[str]:
    if isinstance(value, list):
        parsed = [_as_string(item) for item in value if _as_string(item)]
        return parsed
    one = _as_string(value)
    return [one] if one else []


def reset_target_mapping() -> None:
    """Restore built-in target mapping defaults."""
    TARGET_PREFIXES.clear()
    TARGET_PREFIXES.update({key: list(value) for key, value in DEFAULT_TARGET_PREFIXES.items()})

    TARGET_ORDER.clear()
    TARGET_ORDER.extend(DEFAULT_TARGET_ORDER)

    TARGET_DISPLAY_NAMES.clear()
    TARGET_DISPLAY_NAMES.update(DEFAULT_TARGET_DISPLAY_NAMES)

    REPORT_KEYS.clear()
    REPORT_KEYS.update(DEFAULT_REPORT_KEYS)


def apply_target_mapping(raw: dict[str, Any]) -> None:
    """
    Apply external target mapping.

    Supported schema:
    {
      "targets": {
        "placements": {
          "display_name": "Placement",
          "prefixes": ["bcg_placements"],
          "kind": "report"   // report | view
        }
      },
      "target_order": ["placements", "..."]
    }
    """
    targets_raw = raw.get("targets")
    if not isinstance(targets_raw, dict) or not targets_raw:
        raise TargetMappingError("`targets` must be a non-empty object.")

    parsed_prefixes: dict[str, list[str]] = {}
    parsed_display_names: dict[str, str] = {}
    parsed_report_keys: set[str] = set()

    for key, value in targets_raw.items():
        target_key = _as_string(key)
        if not target_key:
            continue

        if isinstance(value, dict):
            prefixes = _as_str_list(value.get("prefixes"))
            if not prefixes:
                prefixes = _as_str_list(value.get("prefix"))
            display_name = _as_string(value.get("display_name")) or target_key
            kind = _as_string(value.get("kind") or value.get("type")).lower()
        else:
            prefixes = _as_str_list(value)
            display_name = target_key
            kind = ""

        if not prefixes:
            raise TargetMappingError(f"target `{target_key}` must include at least one prefix.")

        parsed_prefixes[target_key] = prefixes
        parsed_display_names[target_key] = display_name
        if kind == "report":
            parsed_report_keys.add(target_key)

    if not parsed_prefixes:
        raise TargetMappingError("No valid target entries found in `targets`.")

    raw_order = raw.get("target_order")
    parsed_order: list[str]
    if raw_order is None:
        parsed_order = list(parsed_prefixes.keys())
    elif isinstance(raw_order, list):
        parsed_order = []
        for item in raw_order:
            key = _as_string(item)
            if key and key in parsed_prefixes and key not in parsed_order:
                parsed_order.append(key)
        for key in parsed_prefixes:
            if key not in parsed_order:
                parsed_order.append(key)
    else:
        raise TargetMappingError("`target_order` must be an array when provided.")

    # In-place mutation preserves references imported by other modules.
    TARGET_PREFIXES.clear()
    TARGET_PREFIXES.update(parsed_prefixes)

    TARGET_ORDER.clear()
    TARGET_ORDER.extend(parsed_order)

    TARGET_DISPLAY_NAMES.clear()
    TARGET_DISPLAY_NAMES.update(parsed_display_names)

    REPORT_KEYS.clear()
    REPORT_KEYS.update(parsed_report_keys)


def load_target_mapping_file(path: str | Path, logger=None) -> Path | None:
    mapping_path = Path(path).expanduser().resolve()
    if not mapping_path.exists():
        if logger:
            logger.info("target mapping file not found; using built-in defaults path=%s", mapping_path)
        return None

    try:
        raw = json.loads(mapping_path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise TargetMappingError(f"Invalid target mapping JSON: {mapping_path}") from exc
    if not isinstance(raw, dict):
        raise TargetMappingError(f"Target mapping root must be an object: {mapping_path}")

    apply_target_mapping(raw)
    if logger:
        logger.info(
            "target mapping loaded path=%s target_count=%s order=%s",
            mapping_path,
            len(TARGET_ORDER),
            TARGET_ORDER,
        )
    return mapping_path


def normalize_for_match(value: str) -> str:
    return WHITESPACE_REGEX.sub("", (value or "").strip().lower())


def normalize_for_fuzzy_match(value: str) -> str:
    return NON_ALNUM_REGEX.sub("", (value or "").strip().lower())


def flatten_prefixes() -> Iterable[tuple[str, str]]:
    for key, prefixes in TARGET_PREFIXES.items():
        for prefix in prefixes:
            yield key, prefix


def find_matching_keys(normalized_name: str) -> list[str]:
    """
    Return candidate target keys sorted by stronger prefix matches first.

    Matching behavior:
    - case-insensitive
    - space/separator-insensitive fallback (e.g. "_" / "-" / spaces)
    - suffix-tolerant by prefix matching (e.g. `bcg_auto_adformat_v2`)
    """
    hits: list[tuple[str, int, int]] = []
    name_for_match = normalize_for_match(normalized_name)
    name_for_fuzzy = normalize_for_fuzzy_match(normalized_name)
    for key, prefix in flatten_prefixes():
        prefix_for_match = normalize_for_match(prefix)
        prefix_for_fuzzy = normalize_for_fuzzy_match(prefix)
        if name_for_match.startswith(prefix_for_match):
            hits.append((key, 2, len(prefix_for_match)))
        elif name_for_fuzzy.startswith(prefix_for_fuzzy):
            hits.append((key, 1, len(prefix_for_fuzzy)))

    if not hits:
        return []

    hits.sort(key=lambda x: (x[1], x[2]), reverse=True)
    ordered: list[str] = []
    for key, _, _ in hits:
        if key not in ordered:
            ordered.append(key)
    return ordered


def match_target_key(normalized_name: str) -> tuple[str | None, bool]:
    """
    Return (target_key, is_ambiguous).
    If ambiguous, target_key is None so callers can require manual resolution.

    Matching is prefix-based after normalization:
    - strict pass: lowercase + remove whitespace
    - fallback pass: lowercase + remove all non-alnum separators
    This makes matching case/space/separator-insensitive and suffix-tolerant.
    """
    matches: list[tuple[str, int, int]] = []
    name_for_match = normalize_for_match(normalized_name)
    name_for_fuzzy = normalize_for_fuzzy_match(normalized_name)
    for key, prefix in flatten_prefixes():
        prefix_for_match = normalize_for_match(prefix)
        prefix_for_fuzzy = normalize_for_fuzzy_match(prefix)
        if name_for_match.startswith(prefix_for_match):
            # tier=2: strict space-insensitive match (higher priority)
            matches.append((key, 2, len(prefix_for_match)))
            continue
        if name_for_fuzzy.startswith(prefix_for_fuzzy):
            # tier=1: fuzzy separator-insensitive fallback
            matches.append((key, 1, len(prefix_for_fuzzy)))

    if not matches:
        return None, False

    # Prefer highest tier, then most specific (longest) prefix.
    best_tier = max(tier for _, tier, _ in matches)
    tier_filtered = [m for m in matches if m[1] == best_tier]
    best_len = max(length for _, _, length in tier_filtered)
    best_keys: list[str] = []
    for key, _, length in tier_filtered:
        if length != best_len:
            continue
        if key not in best_keys:
            best_keys.append(key)

    if len(best_keys) == 1:
        return best_keys[0], False
    return None, True
