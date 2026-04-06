"""Account selection helpers.

Legacy Tkinter picker was removed as part of Streamlit migration.
"""

from __future__ import annotations

from typing import Callable

from .models import AdsAccount


def pick_accounts_with_discovery(fetch_accounts: Callable[[], list[AdsAccount]]) -> list[AdsAccount]:
    accounts = fetch_accounts()
    return pick_accounts(accounts)


def pick_accounts(accounts: list[AdsAccount]) -> list[AdsAccount]:
    # Streamlit is now the primary UI. Keep this function deterministic for any
    # non-UI fallback paths by selecting all discovered accounts.
    return list(accounts or [])
