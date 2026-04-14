from __future__ import annotations

import unittest
from pathlib import Path

from google_ads_exporter.downloader import _build_output_path
from google_ads_exporter.models import AdsAccount, SavedReportItem
from google_ads_exporter.saved_reports_scanner import (
    extract_activity_name,
    match_targets_by_activity,
    normalize_activity_key,
)
from google_ads_exporter.targets import match_target_key, reset_target_mapping
from google_ads_exporter.utils import normalize_report_name


def _make_item(name: str, target_key: str, activity_name: str) -> SavedReportItem:
    return SavedReportItem(
        visible_name=name,
        normalized_name=normalize_report_name(name),
        inferred_type="report",
        activity_name=activity_name,
        activity_key=normalize_activity_key(activity_name),
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


class GoogleActivityFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_target_mapping()

    def test_extract_activity_name_uses_last_underscore_token(self) -> None:
        key, ambiguous = match_target_key("bcg_auto_demographics_fcas")
        self.assertFalse(ambiguous)
        self.assertEqual(key, "demographics")
        activity = extract_activity_name("BCG_auto_Demographics_FCAS", key)
        self.assertEqual(activity, "FCAS")

    def test_extract_activity_name_without_suffix_returns_none(self) -> None:
        key, ambiguous = match_target_key("bcg_auto_demographics")
        self.assertFalse(ambiguous)
        self.assertEqual(key, "demographics")
        activity = extract_activity_name("BCG_auto_Demographics", key)
        self.assertIsNone(activity)

    def test_match_target_key_accepts_bcg_auto_report_and_view(self) -> None:
        placements_key, placements_ambiguous = match_target_key("bcg_auto_placements_fcas")
        ad_key, ad_ambiguous = match_target_key("bcg_auto_ad_fcas")
        self.assertFalse(placements_ambiguous)
        self.assertFalse(ad_ambiguous)
        self.assertEqual(placements_key, "placements")
        self.assertEqual(ad_key, "ad")

    def test_match_targets_by_activity_groups_and_resolves_ambiguity(self) -> None:
        items = [
            _make_item("BCG_auto_Demographics_FCAS", "demographics", "FCAS"),
            _make_item("BCG_auto_Ad_FCAS", "ad", "FCAS"),
            _make_item("BCG_auto_Demographics_FCAS_v2", "demographics", "FCAS"),
            _make_item("BCG_auto_Demographics_MPF", "demographics", "MPF"),
        ]

        grouped = match_targets_by_activity(items)
        self.assertIn("fcas", grouped)
        self.assertIn("mpf", grouped)
        self.assertNotIn("demographics", grouped["fcas"])
        self.assertIn("ad", grouped["fcas"])
        self.assertIn("demographics", grouped["mpf"])

    def test_output_filename_contains_activity_fragment(self) -> None:
        account = AdsAccount(name="Innisfree Main", cid="123-456-7890", cid_digits="1234567890")
        output_path = _build_output_path(
            output_dir=Path("C:/Temp"),
            account=account,
            target_key="demographics",
            activity_name="FCAS",
        )
        self.assertIn("_FCAS_demographics.csv", output_path.name)


if __name__ == "__main__":
    unittest.main()
