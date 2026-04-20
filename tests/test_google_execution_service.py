from __future__ import annotations

import unittest

from google_ads_exporter.execution_service import create_execution_store


class GoogleExecutionServiceTests(unittest.TestCase):
    def test_row_update_without_status_preserves_existing_status(self) -> None:
        store = create_execution_store()
        row_id = "1234567890::fcas::demographics"

        store.push_event(
            {
                "type": "row_update",
                "row_id": row_id,
                "account": "Innisfree Main",
                "cid": "123-456-7890",
                "activity": "FCAS",
                "activity_key": "fcas",
                "target_key": "demographics",
                "target_display": "Demographics",
                "status": "Completed",
                "message": "다운로드 완료:test.csv",
            }
        )
        store.drain_events()

        store.push_event(
            {
                "type": "row_update",
                "row_id": row_id,
                "account": "Innisfree Main",
                "cid": "123-456-7890",
                "activity": "FCAS",
                "activity_key": "fcas",
                "target_key": "demographics",
                "target_display": "Demographics",
                "message": "처리 행수 12/12",
                "missing_columns_text": "trueview_views",
                "has_warning": True,
            }
        )
        store.drain_events()

        snapshot = store.snapshot()
        self.assertEqual(len(snapshot["rows"]), 1)
        row = snapshot["rows"][0]
        self.assertEqual(row.status, "Completed")
        self.assertEqual(row.message, "처리 행수 12/12")
        self.assertEqual(row.missing_columns_text, "trueview_views")
        self.assertTrue(row.has_warning)

    def test_account_stage_snapshot_keeps_sheet_counts(self) -> None:
        store = create_execution_store()

        store.push_event(
            {
                "type": "account_stage",
                "account": "Innisfree Main",
                "cid": "123-456-7890",
                "activity": "FCAS",
                "activity_key": "fcas",
                "stage": "통합본",
                "status": "Exporting",
                "message": "통합본 생성중 (현재 시트: Demographics, 1/3)",
                "processed_sheet_count": 1,
                "total_sheet_count": 3,
            }
        )
        store.drain_events()

        snapshot = store.snapshot()
        self.assertEqual(len(snapshot["account_stage_rows"]), 1)
        row = snapshot["account_stage_rows"][0]
        self.assertEqual(row.status, "Exporting")
        self.assertEqual(row.message, "통합본 생성중 (현재 시트: Demographics, 1/3)")
        self.assertEqual(row.processed_sheet_count, 1)
        self.assertEqual(row.total_sheet_count, 3)


if __name__ == "__main__":
    unittest.main()
