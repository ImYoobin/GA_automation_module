from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import google_ads_exporter.streamlit_app as streamlit_app


class StreamlitRuntimePathTests(unittest.TestCase):
    def test_normalize_ui_browser_maps_hidden_options_to_msedge(self) -> None:
        self.assertEqual(streamlit_app._normalize_ui_browser("auto"), ("msedge", True))
        self.assertEqual(streamlit_app._normalize_ui_browser("chromium"), ("msedge", True))
        self.assertEqual(streamlit_app._normalize_ui_browser("msedge"), ("msedge", False))
        self.assertEqual(streamlit_app._normalize_ui_browser("chrome"), ("chrome", False))

    def test_build_run_storage_paths_from_parent_dir(self) -> None:
        paths = streamlit_app._build_run_storage_paths(r"C:\Users\tester", "20260416")

        self.assertEqual(paths["base"], Path(r"C:\Users\tester\GoogleAdsExport"))
        self.assertEqual(paths["raw_dir"], Path(r"C:\Users\tester\GoogleAdsExport\raw\20260416"))
        self.assertEqual(paths["trace_dir"], Path(r"C:\Users\tester\GoogleAdsExport\trace\20260416"))
        self.assertEqual(paths["output_dir"], Path(r"C:\Users\tester\GoogleAdsExport\output\20260416"))
        self.assertEqual(paths["action_log_dir"], Path(r"C:\Users\tester\GoogleAdsExport\output\action_log\20260416"))

    def test_sanitize_loaded_runtime_settings_migrates_legacy_output_dir(self) -> None:
        sanitized, has_invalid = streamlit_app._sanitize_loaded_runtime_settings(
            {
                "browser": "msedge",
                "output_dir": r"%USERPROFILE%\GoogleAdsExport\output",
                "downloads_dir": r"%USERPROFILE%\GoogleAdsExport\downloads",
                "logs_dir": r"%USERPROFILE%\GoogleAdsExport\logs",
            }
        )

        self.assertFalse(has_invalid)
        self.assertEqual(sanitized["base_parent_dir"], str(Path.home()))

    def test_sanitize_loaded_runtime_settings_normalizes_hidden_browser_to_msedge(self) -> None:
        sanitized, has_invalid = streamlit_app._sanitize_loaded_runtime_settings(
            {
                "browser": "chromium",
                "base_parent_dir": r"%USERPROFILE%",
            }
        )

        self.assertTrue(has_invalid)
        self.assertEqual(sanitized["browser"], "msedge")

    def test_runtime_settings_payload_serializes_home_as_userprofile(self) -> None:
        fake_st = SimpleNamespace(
            session_state={
                "browser": "msedge",
                "base_parent_dir": str(Path.home()),
            }
        )

        with patch.object(streamlit_app, "st", fake_st):
            payload = streamlit_app._runtime_settings_payload(Path("."))

        self.assertEqual(payload["base_parent_dir"], "%USERPROFILE%")

    def test_runtime_settings_payload_normalizes_hidden_browser_to_msedge(self) -> None:
        fake_st = SimpleNamespace(
            session_state={
                "browser": "auto",
                "base_parent_dir": str(Path.home()),
            }
        )

        with patch.object(streamlit_app, "st", fake_st):
            payload = streamlit_app._runtime_settings_payload(Path("."))

        self.assertEqual(payload["browser"], "msedge")

    def test_apply_runtime_settings_uses_output_and_trace_dirs(self) -> None:
        fake_st = SimpleNamespace(
            session_state={
                "env_file": "",
                "runtime_dir": "",
                "user_data_dir": "",
                "base_parent_dir": r"C:\Users\tester",
            }
        )

        with (
            patch.object(streamlit_app, "st", fake_st),
            patch("google_ads_exporter.streamlit_app._apply_runtime_directory_overrides") as overrides_mock,
        ):
            streamlit_app._apply_runtime_settings(
                output_dir_override=r"C:\Users\tester\GoogleAdsExport\output\20260416",
                trace_dir_override=r"C:\Users\tester\GoogleAdsExport\trace\20260416",
            )

        overrides_mock.assert_called_once_with(
            runtime_dir="",
            output_dir=r"C:\Users\tester\GoogleAdsExport\output\20260416",
            logs_dir=r"C:\Users\tester\GoogleAdsExport\trace\20260416",
            user_data_dir="",
        )

    def test_open_output_folder_for_completed_run_opens_output_root_once(self) -> None:
        fake_st = SimpleNamespace(
            session_state={
                "opened_output_for_run": "",
                "run_output_root_dir": r"C:\Users\tester\GoogleAdsExport\output",
                "run_output_dir": r"C:\Users\tester\GoogleAdsExport\output\20260416",
                "base_parent_dir": r"C:\Users\tester",
            }
        )
        snapshot = {
            "run_status": "Completed",
            "run_id": "run-1",
            "outputs": [{"workbook_path": r"C:\Users\tester\GoogleAdsExport\output\20260416\test.xlsx"}],
            "action_log_outputs": [],
        }

        with (
            patch.object(streamlit_app, "st", fake_st),
            patch("google_ads_exporter.streamlit_app.subprocess.Popen") as popen_mock,
        ):
            streamlit_app._open_output_folder_for_completed_run(snapshot)
            streamlit_app._open_output_folder_for_completed_run(snapshot)

        popen_mock.assert_called_once_with(["explorer", r"C:\Users\tester\GoogleAdsExport\output"])
        self.assertEqual(fake_st.session_state["opened_output_for_run"], "run-1")

    def test_open_output_folder_for_completed_run_opens_for_action_log_only(self) -> None:
        fake_st = SimpleNamespace(
            session_state={
                "opened_output_for_run": "",
                "run_output_root_dir": r"C:\Users\tester\GoogleAdsExport\output",
                "run_output_dir": r"C:\Users\tester\GoogleAdsExport\output\20260416",
                "base_parent_dir": r"C:\Users\tester",
            }
        )
        snapshot = {
            "run_status": "Completed (With Failures)",
            "run_id": "run-2",
            "outputs": [],
            "action_log_outputs": [{"file_path": r"C:\Users\tester\GoogleAdsExport\output\action_log\20260416\a.csv"}],
        }

        with (
            patch.object(streamlit_app, "st", fake_st),
            patch("google_ads_exporter.streamlit_app.subprocess.Popen") as popen_mock,
        ):
            streamlit_app._open_output_folder_for_completed_run(snapshot)

        popen_mock.assert_called_once_with(["explorer", r"C:\Users\tester\GoogleAdsExport\output"])
        self.assertEqual(fake_st.session_state["opened_output_for_run"], "run-2")

    def test_open_output_folder_for_completed_run_skips_when_no_real_outputs(self) -> None:
        fake_st = SimpleNamespace(
            session_state={
                "opened_output_for_run": "",
                "run_output_root_dir": r"C:\Users\tester\GoogleAdsExport\output",
                "run_output_dir": r"C:\Users\tester\GoogleAdsExport\output\20260416",
                "base_parent_dir": r"C:\Users\tester",
            }
        )
        snapshot = {
            "run_status": "Completed",
            "run_id": "run-3",
            "outputs": [],
            "action_log_outputs": [],
        }

        with (
            patch.object(streamlit_app, "st", fake_st),
            patch("google_ads_exporter.streamlit_app.subprocess.Popen") as popen_mock,
        ):
            streamlit_app._open_output_folder_for_completed_run(snapshot)

        popen_mock.assert_not_called()
        self.assertEqual(fake_st.session_state["opened_output_for_run"], "")

    def test_missing_columns_style_text_marks_non_empty_values_red(self) -> None:
        self.assertIn("#b91c1c", streamlit_app._missing_columns_style_text("Day, Campaign ID"))
        self.assertEqual(streamlit_app._missing_columns_style_text(""), "")

    def test_status_helpers_map_not_found_and_downloaded(self) -> None:
        self.assertEqual(streamlit_app._ui_phase_key("Not Found"), "not_found")
        self.assertEqual(streamlit_app._status_label_text("Downloaded"), "Completed")

    def test_login_progress_helper_text_maps_waiting_and_crawling(self) -> None:
        waiting_text = streamlit_app._login_progress_helper_text(
            {
                "is_running": True,
                "run_status": "Preparing",
                "login_status": "Waiting Login",
            }
        )
        crawling_text = streamlit_app._login_progress_helper_text(
            {
                "is_running": True,
                "run_status": "Preparing",
                "login_status": "Crawling Accounts",
            }
        )

        self.assertEqual(waiting_text, "로그인 대기중입니다.")
        self.assertEqual(crawling_text, "계정 크롤링중입니다.")

    def test_apply_ready_login_result_updates_session_state_once(self) -> None:
        fake_st = SimpleNamespace(
            session_state={
                "accounts": [],
                "selected_cids": {"1234567890"},
                "scan_results": {"old": "value"},
                "matching_ready": True,
                "started": False,
                "opened_output_for_run": "old-run",
                "validated_output_count_run": "old-run",
                "expected_output_count": 5,
                "applied_login_result_run_id": "",
            }
        )
        snapshot = {
            "run_id": "login-run-1",
            "login_accounts_payload": [
                {
                    "name": "Innisfree Main",
                    "cid": "123-456-7890",
                    "cid_digits": "1234567890",
                    "is_manager": False,
                    "raw_text": "",
                }
            ],
        }

        with patch.object(streamlit_app, "st", fake_st):
            streamlit_app._apply_ready_login_result(snapshot)
            streamlit_app._apply_ready_login_result(snapshot)

        self.assertTrue(fake_st.session_state["started"])
        self.assertEqual(len(fake_st.session_state["accounts"]), 1)
        self.assertEqual(fake_st.session_state["accounts"][0].cid, "123-456-7890")
        self.assertEqual(fake_st.session_state["selected_cids"], set())
        self.assertEqual(fake_st.session_state["scan_results"], {})
        self.assertEqual(fake_st.session_state["applied_login_result_run_id"], "login-run-1")


if __name__ == "__main__":
    unittest.main()
