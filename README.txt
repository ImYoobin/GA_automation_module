Google Ads Automation - Quick Start

Run (Module / Python manual E2E)
1) Set-Location "c:\Users\im yoobin\Downloads\ads_automation_source\module_source\google_automation_module"
2) python -m unittest tests.test_google_activity_flow tests.test_downloader_row_fallback tests.test_google_excel_builder_header_mapping tests.test_google_excel_builder_split -v
3) python -m streamlit run main.py

Run (Release / EXE)
1) Double-click Google_Export.exe
2) Browser opens automatically at http://localhost:8501

Current behavior (2026-04)
- Flow: account scan -> activity match -> activity-wise 7 exports -> activity-wise unified workbook.
- Saved reports naming: BCG_auto_<report_or_view>_<activity>.
- UI status: each target changes from Exporting to Downloaded after file save is confirmed.
- Browser window policy:
  - Login verification step is visible first.
  - After login confirmation, browser stays minimized until run end.
  - If user manually changes window state, user override is respected.
- Unified workbook policy:
  - Template-based sheet mapping and header normalization.
  - Total/합계 rows are dropped.
  - Day policy:
    - raw_or_top_single_day targets (device, hourofday, adformat): Day fallback order is raw -> top(single-day) -> execution date - 1 day.
    - raw_only targets (campaign_ad_group, ad, placements, demographics): no date-line injection.
  - Large sheet handling:
    - Excel row overflow splits sheet as sheet, sheet(1), sheet(2), ...
  - Placements optimization:
    - Keep top 100 rows per (day, ad_group_id) by cost.
    - If rows tie at cutoff cost, keep all tied rows.

Logs
- Runtime logs: .\logs\launcher.log
- Export run logs: under configured logs directory (default: %USERPROFILE%\GoogleAdsExport\logs\YYYYMMDD)

