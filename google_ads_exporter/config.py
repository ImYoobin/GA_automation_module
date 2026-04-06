"""Application-wide configuration values."""

from __future__ import annotations

APP_TITLE = "Google Ads Exporter"

LOGIN_URL = "https://ads.google.com/nav/login"
SELECT_ACCOUNT_URL = "https://ads.google.com/nav/selectaccount"
REPORT_EDITOR_URL = "https://ads.google.com/aw/reporteditor"

ACCOUNT_LIST_LOAD_RETRIES = 2
ACCOUNT_SWITCH_RETRIES = 2
SAVED_REPORT_SCAN_RETRIES = 2
DOWNLOAD_CLICK_RETRIES = 2

SHORT_WAIT_MS = 1200
MEDIUM_WAIT_MS = 2500
LONG_WAIT_MS = 6000

SELECTOR_DETECT_TIMEOUT_MS = 4000
ACCOUNT_VERIFY_TIMEOUT_MS = 3500
TABLE_SCAN_TIMEOUT_MS = 7000
DOWNLOAD_TIMEOUT_MS = 30000

BROWSER_AUTO_ORDER = ("chromium", "msedge", "chrome")

