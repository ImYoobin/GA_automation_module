Google Ads Auto Download - Quick Start

Run (Module / Streamlit)
1) Set-Location ".\module_source\google_automation_module"
2) python -m streamlit run .\main.py

Recommended verification
1) Set-Location ".\module_source\google_automation_module"
2) python -m unittest tests.test_action_log_downloader tests.test_action_log_flow tests.test_google_excel_builder_header_mapping tests.test_streamlit_runtime_paths tests.test_login_worker_flow tests.test_google_activity_flow tests.test_downloader_row_fallback tests.test_google_excel_builder_split -v

Run (Release / EXE)
1) Double-click Google_Export.exe
2) Browser opens automatically at http://localhost:8501

Current behavior (2026-04-16)
- Login -> account crawl -> account selection -> saved reports activity matching runs once.
- Execution options are split into campaign data download and action log download. Both are enabled by default.
- Activity matching accepts partial matches. An activity is valid when at least one of the 7 targets exists.
- Saved reports naming rule: BCG_auto_<report_or_view_name>_<activity>.
- Campaign data flow:
  - target-wise download
  - unified workbook build
  - workbook progress is shown in the download table
  - missing template columns are highlighted in red in the UI
- Unified workbook template now requires trueview_views. Raw headers accept both Trueview Views and legacy Trueview View aliases.
- Action log flow:
  - opens Google Ads Change history
  - applies Campaign status / Ad group status = All
  - applies Campaign name starts with <activity>_
  - sets Last 30 days
  - confirms All changes
  - downloads .csv
  - rewrites the saved file to these template columns:
    User / Date & Time, Tool, Change, Campaign, Ad group
  - User / Date & Time combines raw User + Date & time with a line break
  - Tool is intentionally left blank

Storage structure
- The UI accepts one parent directory.
- Runtime folders are created under <parent>\GoogleAdsExport:
  - raw\<YYYYMMDD>
  - trace\<YYYYMMDD>
  - output\<YYYYMMDD>
  - output\action_log\<YYYYMMDD>
- When a run finishes, File Explorer opens the output root folder.

Logs
- Launcher log: .\logs\launcher.log
- Run logs: <parent>\GoogleAdsExport\trace\<YYYYMMDD>\run_*.log

Release build
- Build script: .\build\google\build_google_release.ps1
- Example:
  .\build\google\build_google_release.ps1 -BuildRoot "C:\GoogleExportBuild"
