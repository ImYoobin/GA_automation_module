from __future__ import annotations

import shutil
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock, patch

from google_ads_exporter.google_adapter import run_google_export_for_accounts
from google_ads_exporter.models import AdsAccount, SavedReportItem
from google_ads_exporter.utils import normalize_report_name


class _FakeSyncPlaywrightContext:
    def __enter__(self):
        return object()

    def __exit__(self, exc_type, exc, tb):
        return False


def _make_item(name: str, target_key: str, activity_name: str) -> SavedReportItem:
    return SavedReportItem(
        visible_name=name,
        normalized_name=normalize_report_name(name),
        inferred_type="report",
        activity_name=activity_name,
        activity_key=activity_name.lower(),
        row_text=name,
        matched_key=target_key,
        owner_text=None,
        created_by=None,
        creation_date=None,
        last_accessed=None,
        date_range=None,
        has_download_text=False,
        has_download_icon=False,
    )


class ActionLogFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.account = AdsAccount(
            name="Innisfree Main",
            cid="123-456-7890",
            cid_digits="1234567890",
        )
        item = _make_item("BCG_auto_Demographics_FCAS", "demographics", "FCAS")
        self.matched_map_by_activity = {"fcas": {"demographics": item}}

    def _make_logger(self) -> Mock:
        logger = Mock()
        handler = Mock()
        handler.baseFilename = "C:/Temp/google_ads_test.log"
        logger.handlers = [handler]
        return logger

    def _run_flow(
        self,
        *,
        enable_report_download: bool,
        enable_action_log_download: bool,
        report_helper_side_effect=None,
        action_helper_side_effect=None,
    ):
        events: list[dict] = []
        fake_context = Mock()
        fake_page = Mock()

        if report_helper_side_effect is None:
            report_helper_side_effect = (0, 0, False)
        if action_helper_side_effect is None:
            action_helper_side_effect = (0, False)

        tests_dir = Path(__file__).resolve().parent
        tmp_root = tests_dir / "_tmp_action_log_flow"
        tmp_root.mkdir(parents=True, exist_ok=True)
        tmp_path = tmp_root / uuid.uuid4().hex
        tmp_path.mkdir(parents=True, exist_ok=True)
        report_patch_kwargs = (
            {"side_effect": report_helper_side_effect}
            if callable(report_helper_side_effect)
            else {"return_value": report_helper_side_effect}
        )
        action_patch_kwargs = (
            {"side_effect": action_helper_side_effect}
            if callable(action_helper_side_effect)
            else {"return_value": action_helper_side_effect}
        )

        try:
            with (
                patch("playwright.sync_api.sync_playwright", return_value=_FakeSyncPlaywrightContext()),
                patch("google_ads_exporter.google_adapter._ensure_playwright_event_loop_policy"),
                patch("google_ads_exporter.google_adapter.load_target_mapping_file"),
                patch(
                    "google_ads_exporter.google_adapter.launch_ads_context",
                    return_value=(fake_context, fake_page, "msedge"),
                ),
                patch("google_ads_exporter.google_adapter.ensure_logged_in", return_value=fake_page),
                patch("google_ads_exporter.google_adapter.collect_accounts", return_value=[self.account]),
                patch(
                    "google_ads_exporter.google_adapter.scan_account_saved_reports",
                    return_value=([], self.matched_map_by_activity),
                ),
                patch("google_ads_exporter.google_adapter._minimize_browser_page"),
                patch(
                    "google_ads_exporter.google_adapter._run_report_phase_for_account",
                    **report_patch_kwargs,
                ) as report_mock,
                patch(
                    "google_ads_exporter.google_adapter._run_action_log_phase_for_account",
                    **action_patch_kwargs,
                ) as action_mock,
            ):
                _scan_results, scan_rows = run_google_export_for_accounts(
                    selected_accounts=[self.account],
                    scan_results={},
                    browser="msedge",
                    headless=False,
                    target_map_path="",
                    final_output_dir=tmp_path / "output" / "20260416",
                    downloads_dir=tmp_path / "downloads" / "20260416",
                    action_log_dir=tmp_path / "output" / "action_log" / "20260416",
                    enable_report_download=enable_report_download,
                    enable_action_log_download=enable_action_log_download,
                    logger=self._make_logger(),
                    progress_cb=events.append,
                    scan_before_export=True,
                )
        finally:
            shutil.rmtree(tmp_path, ignore_errors=True)

        return events, report_mock, action_mock, scan_rows

    def test_report_only_mode_skips_action_log_phase(self) -> None:
        events, report_mock, action_mock, _scan_rows = self._run_flow(
            enable_report_download=True,
            enable_action_log_download=False,
            report_helper_side_effect=(1, 0, False),
        )

        self.assertEqual(report_mock.call_count, 1)
        action_mock.assert_not_called()
        self.assertFalse(any(event.get("type") == "action_log_update" for event in events))

    def test_action_log_only_mode_uses_partial_match_activity_and_starts_first_row(self) -> None:
        events, report_mock, action_mock, scan_rows = self._run_flow(
            enable_report_download=False,
            enable_action_log_download=True,
            action_helper_side_effect=(1, False),
        )

        report_mock.assert_not_called()
        self.assertEqual(action_mock.call_count, 1)
        self.assertEqual(
            action_mock.call_args.kwargs["activity_entries"],
            [("fcas", "FCAS")],
        )
        exporting_events = [
            event for event in events
            if event.get("type") == "action_log_update" and event.get("status") == "Exporting"
        ]
        self.assertEqual(len(exporting_events), 1)
        self.assertEqual(exporting_events[0]["activity_key"], "fcas")
        self.assertEqual(exporting_events[0]["message"], "액션로그 다운로드중")
        self.assertEqual(len(scan_rows), 7)

    def test_dual_mode_runs_report_before_action_log_for_each_account(self) -> None:
        call_order: list[str] = []

        def _report_side_effect(*args, **kwargs):
            call_order.append("report")
            return (1, 0, False)

        def _action_side_effect(*args, **kwargs):
            call_order.append("action")
            return (1, False)

        _events, report_mock, action_mock, _scan_rows = self._run_flow(
            enable_report_download=True,
            enable_action_log_download=True,
            report_helper_side_effect=_report_side_effect,
            action_helper_side_effect=_action_side_effect,
        )

        self.assertEqual(report_mock.call_count, 1)
        self.assertEqual(action_mock.call_count, 1)
        self.assertEqual(call_order, ["report", "action"])

    def test_dual_mode_seeds_report_rows_and_workbook_waiting_rows(self) -> None:
        events, _report_mock, _action_mock, _scan_rows = self._run_flow(
            enable_report_download=True,
            enable_action_log_download=True,
            report_helper_side_effect=(1, 0, False),
            action_helper_side_effect=(1, False),
        )

        row_events = [event for event in events if event.get("type") == "row_update"]
        self.assertEqual(len(row_events), 7)

        waiting_rows = [event for event in row_events if event.get("status") == "Waiting"]
        not_found_rows = [event for event in row_events if event.get("status") == "Not Found"]
        self.assertEqual(len(waiting_rows), 1)
        self.assertEqual(waiting_rows[0]["target_key"], "demographics")
        self.assertEqual(waiting_rows[0]["message"], "앞선 시트처리 대기중입니다.")
        self.assertEqual(len(not_found_rows), 6)
        self.assertTrue(
            all(event.get("message") == "리포트·뷰를 찾지 못했습니다." for event in not_found_rows)
        )

        workbook_events = [
            event
            for event in events
            if event.get("type") == "account_stage" and event.get("stage") == "통합본"
        ]
        self.assertEqual(len(workbook_events), 1)
        self.assertEqual(workbook_events[0]["status"], "Waiting")
        self.assertEqual(workbook_events[0]["message"], "캠페인 데이터 다운로드 후 통합본을 생성합니다.")
        self.assertEqual(workbook_events[0]["processed_sheet_count"], 0)
        self.assertEqual(workbook_events[0]["total_sheet_count"], 1)

        history_waiting_events = [
            event
            for event in events
            if event.get("type") == "action_log_update" and event.get("status") == "Waiting"
        ]
        self.assertEqual(len(history_waiting_events), 1)
        self.assertEqual(history_waiting_events[0]["message"], "캠페인 데이터 다운로드 진행중입니다.")


if __name__ == "__main__":
    unittest.main()
