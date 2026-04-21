from __future__ import annotations

import csv
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from google_ads_exporter.action_log_downloader import (
    ALL_CHANGES_CHIP_SELECTOR,
    CSV_REGEX,
    FILTER_CHIP_SELECTOR,
    FILTER_DELETE_BUTTON_SELECTOR,
    _add_campaign_name_filter,
    _click_with_retry,
    _download_action_log_csv,
    _ensure_all_changes_selected,
    _find_csv_menu_item,
    _is_loading_state_blocking,
    _remove_existing_campaign_name_filter,
    _set_last_30_days,
    _set_status_filter_all,
    _transform_action_log_csv,
    _wait_for_ui_idle,
    build_action_log_raw_download_path,
    build_action_log_output_path,
    build_action_log_run_dir,
)
from google_ads_exporter.models import AdsAccount


class ActionLogDownloaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.account = AdsAccount(
            name="Innisfree Main",
            cid="123-456-7890",
            cid_digits="1234567890",
        )
        self._tests_dir = Path(__file__).resolve().parent

    def tearDown(self) -> None:
        for file_name in (
            "_tmp_action_log.csv",
            "_tmp_action_log.raw.csv",
            "_tmp_action_log_transform_raw.csv",
            "_tmp_action_log_transform.csv",
        ):
            target = self._tests_dir / file_name
            try:
                target.unlink()
            except FileNotFoundError:
                pass

    @staticmethod
    def _idle_snapshot() -> dict[str, object]:
        return {
            "progress_container_empty": True,
            "progress_role": "none",
            "shell_only": False,
            "visible_progress": False,
            "chips_busy": "",
            "blocking": False,
        }

    def test_build_action_log_paths_follow_output_base(self) -> None:
        base_a = Path("C:/Exports/A")
        base_b = Path("C:/Exports/B")
        run_dir_a = build_action_log_run_dir(base_a, "20260416")
        run_dir_b = build_action_log_run_dir(base_b, "20260416")

        self.assertEqual(run_dir_a, Path("C:/Exports/A/action_log/20260416"))
        self.assertEqual(run_dir_b, Path("C:/Exports/B/action_log/20260416"))

        output_path = build_action_log_output_path(
            action_log_dir=run_dir_a,
            account=self.account,
            activity_name="FCAS",
            run_date="20260416",
        )
        self.assertEqual(
            output_path.name,
            "20260416_Innisfree Main_FCAS_action_log.csv",
        )
        self.assertEqual(
            build_action_log_raw_download_path(output_path).name,
            "20260416_Innisfree Main_FCAS_action_log.raw.csv",
        )

    def test_remove_existing_campaign_name_filter_uses_third_chip_delete_button(self) -> None:
        page = Mock()
        page.wait_for_function.return_value = None
        chips = Mock()
        page.locator.return_value = chips
        chips.count.return_value = 3

        third_chip = Mock()
        chips.nth.return_value = third_chip
        delete_locator = Mock()
        delete_button = Mock()
        delete_button.count.return_value = 1
        delete_locator.first = delete_button
        third_chip.locator.return_value = delete_locator

        removed = _remove_existing_campaign_name_filter(page)

        self.assertTrue(removed)
        page.locator.assert_called_with(FILTER_CHIP_SELECTOR)
        chips.nth.assert_called_once_with(2)
        third_chip.locator.assert_called_once_with(FILTER_DELETE_BUTTON_SELECTOR)
        delete_button.click.assert_called_once_with(timeout=15_000)

    def test_ensure_all_changes_selected_clicks_first_chip_when_unselected(self) -> None:
        page = Mock()
        page.wait_for_function.return_value = None
        chips = Mock()
        first_chip = Mock()
        page.locator.return_value = chips
        chips.count.return_value = 1
        chips.nth.return_value = first_chip
        first_chip.get_attribute.side_effect = ["false", ""]

        _ensure_all_changes_selected(page)

        page.locator.assert_called_once_with(ALL_CHANGES_CHIP_SELECTOR)
        chips.nth.assert_called_once_with(0)
        first_chip.click.assert_called_once_with(timeout=15_000)

    def test_download_action_log_csv_prefers_exact_csv_menu_item(self) -> None:
        page = Mock()
        page.wait_for_function.return_value = None
        output_path = self._tests_dir / "_tmp_action_log.csv"

        download_button = Mock()
        csv_item = Mock()
        download = Mock()
        expect_ctx = MagicMock()
        expect_ctx.__enter__.return_value = SimpleNamespace(value=download)
        expect_ctx.__exit__.return_value = False
        page.expect_download.return_value = expect_ctx

        with (
            patch("google_ads_exporter.action_log_downloader._find_download_button", return_value=download_button),
            patch("google_ads_exporter.action_log_downloader._first_visible_locator", return_value=csv_item) as first_visible_mock,
            patch("google_ads_exporter.action_log_downloader._find_locator_by_text") as lookup_mock,
            patch("google_ads_exporter.action_log_downloader._transform_action_log_csv") as transform_mock,
        ):
            saved_path = _download_action_log_csv(page, output_path=output_path)

        self.assertEqual(saved_path, output_path)
        download_button.click.assert_called_once_with(timeout=15_000)
        first_visible_mock.assert_called_once()
        lookup_mock.assert_not_called()
        csv_item.click.assert_called_once_with(timeout=15_000)
        raw_download_path = build_action_log_raw_download_path(output_path)
        download.save_as.assert_called_once_with(str(raw_download_path))
        transform_mock.assert_called_once_with(raw_download_path=raw_download_path, output_path=output_path)

    def test_find_csv_menu_item_falls_back_to_second_visible_item_after_excel_csv(self) -> None:
        page = Mock()
        exact_locator = Mock()
        role_locator = Mock()
        menu_locator = Mock()
        first_item = Mock()
        second_item = Mock()

        page.locator.side_effect = lambda selector: {
            "material-select-item[role='menuitem'][aria-label='.csv']": exact_locator,
            "[role='menu'] material-select-item[role='menuitem']": menu_locator,
            "material-select-item[role='menuitem']": menu_locator,
        }[selector]
        page.get_by_role.return_value = role_locator

        exact_locator.count.return_value = 0
        role_locator.count.return_value = 0
        menu_locator.count.return_value = 2
        menu_locator.nth.side_effect = [first_item, second_item, first_item, second_item]

        first_item.is_visible.return_value = True
        first_item.inner_text.return_value = "Excel .csv"
        first_item.get_attribute.side_effect = lambda name: ""

        second_item.is_visible.return_value = True
        second_item.inner_text.side_effect = RuntimeError("text unavailable")
        second_item.get_attribute.side_effect = lambda name: ""

        selected = _find_csv_menu_item(page)

        self.assertIs(selected, second_item)

    def test_transform_action_log_csv_rewrites_download_to_template_columns(self) -> None:
        raw_path = self._tests_dir / "_tmp_action_log_transform_raw.csv"
        output_path = self._tests_dir / "_tmp_action_log_transform.csv"
        with raw_path.open("w", encoding="utf-8-sig", newline="") as fp:
            fp.write(
                'Change history report\n'
                '"March 17, 2026 - April 15, 2026"\n'
                'Date & time,User,Campaign,Ad group,Changes\n'
                '"Apr 15, 2026, 8:38:38 PM",isla.yang22@gmail.com,Campaign A,Ad Group A,"1 video ad changed\n'
                '  Status changed from enabled to paused"\n'
            )

        _transform_action_log_csv(raw_download_path=raw_path, output_path=output_path)

        with output_path.open("r", encoding="utf-8-sig", newline="") as fp:
            reader = csv.DictReader(fp)
            self.assertEqual(
                reader.fieldnames,
                ["User / Date & Time", "Tool", "Change", "Campaign", "Ad group"],
            )
            rows = list(reader)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["User / Date & Time"], "isla.yang22@gmail.com\nApr 15, 2026, 8:38:38 PM")
        self.assertEqual(rows[0]["Tool"], "")
        self.assertEqual(rows[0]["Change"], "1 video ad changed\n  Status changed from enabled to paused")
        self.assertEqual(rows[0]["Campaign"], "Campaign A")
        self.assertEqual(rows[0]["Ad group"], "Ad Group A")

    def test_click_with_retry_retries_when_progress_overlay_intercepts(self) -> None:
        page = Mock()
        page.wait_for_function.return_value = None
        locator = Mock()
        locator.click.side_effect = [
            RuntimeError("ipl-progress-indicator intercepts pointer events"),
            None,
        ]

        _click_with_retry(locator, page=page, description="status filter chip")

        self.assertEqual(locator.click.call_count, 2)
        self.assertGreaterEqual(page.wait_for_timeout.call_count, 1)

    def test_click_with_retry_uses_15000_default_timeout(self) -> None:
        page = Mock()
        page.wait_for_function.return_value = None
        locator = Mock()

        _click_with_retry(locator, page=page, description="status filter chip")

        locator.click.assert_called_once_with(timeout=15_000)

    def test_loading_state_helper_treats_placeholder_shell_as_idle(self) -> None:
        snapshot = {
            "overlay_present": True,
            "overlay_visible": True,
            "progress_container_empty": True,
            "shell_only": True,
            "progress_role": "none",
            "visible_progress": False,
            "chips_busy": "",
        }

        self.assertFalse(_is_loading_state_blocking(snapshot))

    def test_loading_state_helper_treats_visible_progress_as_blocking(self) -> None:
        snapshot = {
            "overlay_present": True,
            "overlay_visible": True,
            "progress_container_empty": False,
            "shell_only": False,
            "progress_role": "progressbar",
            "visible_progress": True,
            "chips_busy": "",
        }

        self.assertTrue(_is_loading_state_blocking(snapshot))

    def test_wait_for_ui_idle_allows_placeholder_only_overlay(self) -> None:
        page = Mock()
        page.wait_for_function.return_value = None

        _wait_for_ui_idle(page, timeout_ms=10)

        page.wait_for_function.assert_called_once()

    def test_wait_for_ui_idle_raises_when_loading_does_not_clear(self) -> None:
        page = Mock()
        page.wait_for_function.side_effect = RuntimeError("timeout")
        page.evaluate.return_value = {
            "overlay_present": True,
            "progress_exists": True,
            "progress_role": "progressbar",
            "progress_container_empty": False,
            "shell_only": False,
            "progress_child_count": 1,
            "progress_html": "<ipl-progress-indicator></ipl-progress-indicator>",
            "visible_progress": True,
            "overlay_visible": True,
            "overlay_pointer_events": "auto",
            "progress_pointer_events": "auto",
            "progress_bounding_box": "0,72,1200,4",
            "blocking": True,
            "chips_busy": "true",
        }
        logger = Mock()

        with self.assertRaisesRegex(RuntimeError, "did not clear"):
            _wait_for_ui_idle(page, timeout_ms=0, logger=logger, reason="status filter chip")

        logger.warning.assert_called_once()

    def test_set_status_filter_all_uses_dialog_scope_and_waits_for_auto_close(self) -> None:
        page = Mock()
        page.wait_for_function.return_value = None
        chips = Mock()
        page.locator.return_value = chips
        chips.count.return_value = 2

        chip_row = Mock()
        chips.nth.return_value = chip_row
        chip_button = Mock()
        chip_row.locator.return_value.first = chip_button

        dialog = Mock()
        listbox = Mock()
        dialog.locator.return_value = listbox
        options = Mock()
        listbox.locator.return_value = options
        options.count.return_value = 1
        option = Mock()
        options.nth.return_value = option

        with (
            patch("google_ads_exporter.action_log_downloader._wait_for_visible_dialog", return_value=dialog),
            patch("google_ads_exporter.action_log_downloader._wait_for_visible_locator", return_value=listbox),
            patch("google_ads_exporter.action_log_downloader._wait_for_locator_gone") as gone_mock,
            patch("google_ads_exporter.action_log_downloader._click_with_retry") as click_mock,
            patch("google_ads_exporter.action_log_downloader._wait_for_ui_idle") as wait_mock,
        ):
            _set_status_filter_all(page, chip_index=0)

        self.assertEqual(click_mock.call_count, 2)
        gone_mock.assert_called_once_with(dialog, timeout_ms=15_000)
        self.assertTrue(any(call.kwargs.get("reason") == "after_status_filter_0" for call in wait_mock.mock_calls))

    def test_set_last_30_days_waits_for_loading_after_click(self) -> None:
        page = Mock()
        button = Mock()

        with (
            patch("google_ads_exporter.action_log_downloader._wait_for_locator", return_value=button),
            patch("google_ads_exporter.action_log_downloader._click_with_retry") as click_mock,
            patch("google_ads_exporter.action_log_downloader._wait_for_ui_idle") as wait_mock,
        ):
            _set_last_30_days(page)

        click_mock.assert_called_once()
        self.assertTrue(any(call.kwargs.get("reason") == "after_last_30_days" for call in wait_mock.mock_calls))

    def test_add_campaign_name_filter_waits_for_loading_after_apply(self) -> None:
        page = Mock()
        menu = Mock()
        menu_items = Mock()
        editor_root = Mock()
        add_filter_input = Mock()
        operator_button = Mock()
        operator_listbox = Mock()
        operator_options = Mock()
        value_input = Mock()
        apply_button = Mock()
        campaign_name_item = Mock()
        starts_with_option = Mock()
        menu.locator.return_value = menu_items
        editor_root.locator.side_effect = [Mock(), Mock(), Mock()]
        operator_listbox.locator.return_value = operator_options

        with (
            patch(
                "google_ads_exporter.action_log_downloader._wait_for_visible_locator",
                side_effect=[add_filter_input, menu, operator_button, operator_listbox, value_input, apply_button],
            ),
            patch("google_ads_exporter.action_log_downloader._wait_for_visible_dialog", return_value=editor_root),
            patch("google_ads_exporter.action_log_downloader._click_with_retry") as click_mock,
            patch("google_ads_exporter.action_log_downloader._wait_for_locator_gone") as gone_mock,
            patch(
                "google_ads_exporter.action_log_downloader._find_locator_by_text",
                side_effect=[campaign_name_item, starts_with_option],
            ) as find_mock,
            patch("google_ads_exporter.action_log_downloader._wait_for_ui_idle") as wait_mock,
        ):
            _add_campaign_name_filter(page, value="FCAS_")

        self.assertEqual(click_mock.call_count, 5)
        self.assertEqual(find_mock.call_args_list[0].kwargs.get("visible_only"), True)
        self.assertEqual(find_mock.call_args_list[1].kwargs.get("visible_only"), True)
        self.assertEqual(gone_mock.call_count, 2)
        self.assertTrue(any(call.kwargs.get("reason") == "after_campaign_name_apply" for call in wait_mock.mock_calls))


if __name__ == "__main__":
    unittest.main()
