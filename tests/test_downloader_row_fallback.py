from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from google_ads_exporter.downloader import (
    PENDING_DOWNLOAD_REASON,
    _build_row_miss_reason,
    _log_row_lookup_miss,
    download_item,
    finalize_background_download_results,
)
from google_ads_exporter.models import AdsAccount, DownloadResult, SavedReportItem
from google_ads_exporter.utils import normalize_report_name


def _make_item(name: str, target_key: str = "placements") -> SavedReportItem:
    return SavedReportItem(
        visible_name=name,
        normalized_name=normalize_report_name(name),
        inferred_type="report",
        activity_name="FCAS",
        activity_key="fcas",
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


class DownloaderRowFallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.account = AdsAccount(name="Innisfree Main", cid="123-456-7890", cid_digits="1234567890")
        self.item = _make_item("BCG_auto_Placements_FCAS", target_key="placements")

    @patch("google_ads_exporter.downloader._try_report_download")
    @patch("google_ads_exporter.downloader._log_row_lookup_miss")
    @patch("google_ads_exporter.downloader._collect_row_lookup_diagnostics")
    @patch("google_ads_exporter.downloader._apply_reports_sort_fallback")
    @patch("google_ads_exporter.downloader._find_row_for_item_with_scroll")
    @patch("google_ads_exporter.downloader._set_show_rows_to_500")
    def test_row_miss_triggers_sort_fallback_and_sets_sticky_state(
        self,
        set_rows_mock,
        find_row_mock,
        apply_sort_mock,
        collect_diag_mock,
        log_miss_mock,
        try_report_mock,
    ) -> None:
        del set_rows_mock, log_miss_mock
        fake_row = Mock()
        fake_saved = Path("C:/Temp/fake.csv")
        find_row_mock.side_effect = [None, fake_row]
        apply_sort_mock.return_value = True
        collect_diag_mock.return_value = {
            "dom_rows": 15,
            "aria_rowcount": "16",
            "pagination": "1 - 21 of 21",
            "sample_names": [],
            "near_names": [],
            "url": "https://ads.google.com",
        }
        try_report_mock.return_value = (fake_saved, None)
        lookup_state: dict[str, bool] = {}

        result = download_item(
            page=Mock(),
            account=self.account,
            item=self.item,
            output_dir=Path("C:/Temp"),
            activity_name="FCAS",
            activity_key="fcas",
            lookup_state=lookup_state,
        )

        self.assertTrue(result.success)
        self.assertEqual(result.filename, "fake.csv")
        self.assertTrue(lookup_state.get("prefer_reports_asc"))
        self.assertEqual(find_row_mock.call_count, 2)
        apply_sort_mock.assert_called_once()

    @patch("google_ads_exporter.downloader._try_report_download")
    @patch("google_ads_exporter.downloader._find_row_for_item_with_scroll")
    @patch("google_ads_exporter.downloader._apply_reports_sort_fallback")
    @patch("google_ads_exporter.downloader._set_show_rows_to_500")
    def test_sticky_asc_state_reuses_sort_alignment(
        self,
        set_rows_mock,
        apply_sort_mock,
        find_row_mock,
        try_report_mock,
    ) -> None:
        del set_rows_mock
        fake_row = Mock()
        fake_saved = Path("C:/Temp/fake.csv")
        find_row_mock.return_value = fake_row
        apply_sort_mock.return_value = True
        try_report_mock.return_value = (fake_saved, None)
        lookup_state = {"prefer_reports_asc": True}

        result = download_item(
            page=Mock(),
            account=self.account,
            item=self.item,
            output_dir=Path("C:/Temp"),
            activity_name="FCAS",
            activity_key="fcas",
            lookup_state=lookup_state,
        )

        self.assertTrue(result.success)
        self.assertGreaterEqual(apply_sort_mock.call_count, 1)
        reasons = [call.kwargs.get("reason") for call in apply_sort_mock.call_args_list]
        self.assertIn("sticky_asc", reasons)
        self.assertNotIn("row_not_found", reasons)

    @patch("google_ads_exporter.downloader._try_report_download")
    @patch("google_ads_exporter.downloader._find_row_for_item_with_scroll")
    @patch("google_ads_exporter.downloader._set_show_rows_to_500")
    def test_download_item_preserves_pending_reason_for_background_download(
        self,
        set_rows_mock,
        find_row_mock,
        try_report_mock,
    ) -> None:
        del set_rows_mock
        find_row_mock.return_value = Mock()
        try_report_mock.return_value = (Path("C:/Temp/pending.csv"), PENDING_DOWNLOAD_REASON)

        result = download_item(
            page=Mock(),
            account=self.account,
            item=self.item,
            output_dir=Path("C:/Temp"),
            activity_name="FCAS",
            activity_key="fcas",
            lookup_state={},
        )

        self.assertTrue(result.success)
        self.assertEqual(result.filename, "pending.csv")
        self.assertEqual(result.reason, PENDING_DOWNLOAD_REASON)

    def test_row_miss_reason_includes_dom_diagnostics(self) -> None:
        reason = _build_row_miss_reason(
            {
                "dom_rows": 15,
                "aria_rowcount": "16",
                "pagination": "1 - 21 of 21",
            }
        )
        self.assertIn("dom_rows=15", reason)
        self.assertIn("aria_rowcount=16", reason)
        self.assertIn("pagination=1 - 21 of 21", reason)

    def test_log_row_lookup_miss_includes_diagnostics_fields(self) -> None:
        logger = Mock()
        diagnostics = {
            "dom_rows": 15,
            "aria_rowcount": "16",
            "pagination": "1 - 21 of 21",
            "sample_names": ["BCG_auto_Placements_FCAS"],
            "near_names": ["BCG_auto_Placements_FCAS"],
            "url": "https://ads.google.com",
        }
        page = SimpleNamespace(url="https://ads.google.com")

        _log_row_lookup_miss(
            page=page,
            item=self.item,
            attempt=1,
            logger=logger,
            phase="after_sort_fallback",
            diagnostics=diagnostics,
        )

        self.assertTrue(logger.warning.called)
        message_template = logger.warning.call_args.args[0]
        self.assertIn("dom_rows=%s", message_template)
        self.assertIn("aria_rowcount=%s", message_template)
        self.assertIn("pagination=%s", message_template)
        self.assertIn("sample_names=%s", message_template)

    def test_finalize_background_download_results_resolves_saved_pending_item(self) -> None:
        results = [
            DownloadResult(
                target_key="placements",
                success=True,
                filename="20260415_test_FCAS_placements.csv",
                reason=PENDING_DOWNLOAD_REASON,
            )
        ]
        target_path = Path(__file__).resolve().parent / "_tmp_pending_download_success.csv"
        if target_path.exists():
            target_path.unlink()
        fake_download = Mock()
        fake_download.save_as.side_effect = lambda dest: Path(dest).write_text("a,b\n1,2\n", encoding="utf-8")
        lookup_state = {
            "pending_downloads": {
                "placements": {
                    "download": fake_download,
                    "path": str(target_path),
                }
            }
        }

        finalized = finalize_background_download_results(results=results, lookup_state=lookup_state)
        self.assertEqual(len(finalized), 1)
        self.assertTrue(finalized[0].success)
        self.assertEqual(finalized[0].reason, None)
        self.assertEqual(finalized[0].filename, target_path.name)
        if target_path.exists():
            target_path.unlink()

    def test_finalize_background_download_results_marks_timeout_as_failed(self) -> None:
        results = [
            DownloadResult(
                target_key="demographics",
                success=True,
                filename="20260415_test_FCAS_demographics.csv",
                reason=PENDING_DOWNLOAD_REASON,
            )
        ]
        target_path = Path(__file__).resolve().parent / "_tmp_pending_download_fail.csv"
        if target_path.exists():
            target_path.unlink()
        fake_download = Mock()
        fake_download.save_as.side_effect = RuntimeError("download canceled")
        lookup_state = {
            "pending_downloads": {
                "demographics": {
                    "download": fake_download,
                    "path": str(target_path),
                }
            },
        }

        finalized = finalize_background_download_results(results=results, lookup_state=lookup_state)
        self.assertEqual(len(finalized), 1)
        self.assertFalse(finalized[0].success)
        self.assertIn("background download save failed", finalized[0].reason or "")


if __name__ == "__main__":
    unittest.main()
