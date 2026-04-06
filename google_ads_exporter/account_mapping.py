"""Account-group mapping loader for Google Ads exporter."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_NON_DIGIT = re.compile(r"\D+")


class AccountMappingError(ValueError):
    """Raised when account mapping JSON is invalid."""


@dataclass(frozen=True)
class AccountMapping:
    groups: dict[str, tuple[str, ...]]
    default_group: str = ""


def _as_string(value: Any) -> str:
    return str(value or "").strip()


def normalize_cid_digits(value: str) -> str:
    return _NON_DIGIT.sub("", _as_string(value))


def parse_account_mapping(raw: Any) -> AccountMapping:
    if not isinstance(raw, dict):
        raise AccountMappingError("Account mapping root must be an object.")

    groups_raw = raw.get("groups")
    if not isinstance(groups_raw, dict):
        groups_raw = {}

    parsed_groups: dict[str, tuple[str, ...]] = {}
    for group_name, payload in groups_raw.items():
        key = _as_string(group_name)
        if not key:
            continue

        cids_raw: Any
        if isinstance(payload, dict):
            cids_raw = payload.get("cids")
        else:
            cids_raw = payload

        if not isinstance(cids_raw, list):
            raise AccountMappingError(f"group `{key}` must include `cids` as an array.")

        normalized: list[str] = []
        for item in cids_raw:
            digits = normalize_cid_digits(_as_string(item))
            if digits and digits not in normalized:
                normalized.append(digits)

        if normalized:
            parsed_groups[key] = tuple(normalized)

    default_group = _as_string(raw.get("default_group"))
    if default_group and default_group not in parsed_groups:
        raise AccountMappingError(
            f"default_group `{default_group}` does not exist in groups."
        )

    return AccountMapping(groups=parsed_groups, default_group=default_group)


def load_account_mapping(path: str | Path, logger=None) -> AccountMapping:
    mapping_path = Path(path).expanduser().resolve()
    if not mapping_path.exists():
        if logger:
            logger.info("account mapping file not found; manual account picker will be used path=%s", mapping_path)
        return AccountMapping(groups={}, default_group="")

    try:
        raw = json.loads(mapping_path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise AccountMappingError(f"Invalid account mapping JSON: {mapping_path}") from exc

    mapping = parse_account_mapping(raw)
    if logger:
        logger.info(
            "account mapping loaded path=%s group_count=%s default_group=%s",
            mapping_path,
            len(mapping.groups),
            mapping.default_group or "-",
        )
    return mapping
