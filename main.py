"""Streamlit script entrypoint used by packaged launcher."""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure sibling package imports work in packaged runtime:
# MyApp/
#   app/main.py
#   google_ads_exporter/
_RUNTIME_ROOT = Path(__file__).resolve().parent.parent
if str(_RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(_RUNTIME_ROOT))

from google_ads_exporter.streamlit_app import run_streamlit_app


if __name__ == "__main__":
    run_streamlit_app()
