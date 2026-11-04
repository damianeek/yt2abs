"""SQLite queue/state tests."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from yt2abs.db import Database


class DatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "test.sqlite3")
        self.db.initialize()

    def test_create_is_idempotent_and_keeps_added_at(self) -> None:
        row, action = self.db.create_or_requeue("dQw4w9WgXcQ")
        self.assertEqual(action, "created")
        self.assertEqual(row["status"], "queued")
        self.assertEqual(row["attempts"], 0)
        self.assertTrue(row["added_at"])

        again, action = self.db.create_or_requeue("dQw4w9WgXcQ")
        self.assertEqual(action, "existing")
        self.assertEqual(again["added_at"], row["added_at"])
        self.assertEqual(len(self.db.list_videos()), 1)

    def test_claim_marks_downloading_and_counts_attempt(self) -> None:
        self.db.create_or_requeue("dQw4w9WgXcQ")
        job = self.db.claim_next()
        self.assertIsNotNone(job)
        assert job is not None
        self.assertEqual(job["status"], "downloading")
        self.assertEqual(job["stage"], "starting")
        self.assertEqual(job["attempts"], 1)
        self.assertIsNone(self.db.claim_next())

    def test_retry_backoff_hides_job_until_due(self) -> None:
        self.db.create_or_requeue("dQw4w9WgXcQ")
        job = self.db.claim_next(now="2026-01-01T00:00:00Z")
        assert job is not None
        self.db.schedule_retry("dQw4w9WgXcQ", "boom", delay_seconds=120)
        row = self.db.get_video("dQw4w9WgXcQ")
        assert row is not None
        self.assertIsNone(self.db.claim_next(now="2026-01-01T00:01:00Z"))
        self.assertIsNone(self.db.claim_next(now="2026-01-01T00:02:01Z"))
        retried = self.db.claim_next(now=row["next_attempt_at"])
        assert retried is not None
        self.assertEqual(retried["attempts"], 2)

    def test_mark_imported_sets_terminal_fields(self) -> None:
        self.db.create_or_requeue("dQw4w9WgXcQ")
        self.db.claim_next()
        self.db.mark_imported(
            "dQw4w9WgXcQ",
            file_path="/library/Chan/2026-01-01 - T [dQw4w9WgXcQ].m4a",
            abs_item_id="li_1",
            abs_episode_id="ep_1",
        )
        row = self.db.get_video("dQw4w9WgXcQ")
        assert row is not None
        self.assertEqual(row["status"], "imported")
        self.assertIsNone(row["stage"])
        self.assertEqual(row["abs_item_id"], "li_1")
        self.assertEqual(row["abs_episode_id"], "ep_1")
        self.assertIsNone(row["next_attempt_at"])

    def test_mark_imported_does_not_resurrect_a_deleted_job(self) -> None:
        self.db.create_or_requeue("dQw4w9WgXcQ")
        self.db.claim_next()
        self.db.mark_deleted("dQw4w9WgXcQ", reason="deleted in UI")
        row = self.db.mark_imported(
            "dQw4w9WgXcQ",
            file_path="/library/Chan/file.m4a",
            abs_item_id="li_1",
            abs_episode_id="ep_1",
        )
        assert row is not None
        self.assertEqual(row["status"], "deleted")
        self.assertIsNone(row["file_path"])

    def test_failed_and_deleted_jobs_are_requeued(self) -> None:
        self.db.create_or_requeue("dQw4w9WgXcQ")
        self.db.mark_failed("dQw4w9WgXcQ", "provider exploded")
        row, action = self.db.create_or_requeue("dQw4w9WgXcQ")
        self.assertEqual(action, "requeued")
        self.assertEqual(row["status"], "queued")
        self.assertEqual(row["attempts"], 0)
        self.assertIsNone(row["error"])

        self.db.update_video(
            "dQw4w9WgXcQ",
            status="deleted",
            deleted_at="2026-01-01T00:00:00Z",
        )
        row, action = self.db.create_or_requeue("dQw4w9WgXcQ")
        self.assertEqual(action, "requeued")
        self.assertIsNone(row["deleted_at"])

    def test_retention_override_is_stored_and_updatable(self) -> None:
        row, _ = self.db.create_or_requeue("dQw4w9WgXcQ", retention_override=3)
        self.assertEqual(row["retention_override"], 3)
        row, action = self.db.create_or_requeue("dQw4w9WgXcQ", retention_override=7)
        self.assertEqual(action, "existing")
        self.assertEqual(row["retention_override"], 7)

    def test_reset_inflight_requeues_downloading(self) -> None:
        self.db.create_or_requeue("dQw4w9WgXcQ")
        self.db.claim_next()
        self.assertEqual(self.db.reset_inflight(), 1)
        row = self.db.get_video("dQw4w9WgXcQ")
        assert row is not None
        self.assertEqual(row["status"], "queued")
        self.assertEqual(row["attempts"], 1)

    def test_channel_folders_are_unique(self) -> None:
        self.db.save_channel("UC1", "Same name", "Same name")
        self.assertIsNone(self.db.folder_in_use("Other"))
        self.assertEqual(self.db.folder_in_use("Same name"), "UC1")
        self.db.save_channel("UC1", "Renamed", "Same name")
        channel = self.db.get_channel("UC1")
        assert channel is not None
        self.assertEqual(channel["folder_name"], "Same name")

    def test_counts(self) -> None:
        self.db.create_or_requeue("dQw4w9WgXcQ")
        self.db.create_or_requeue("aqz-KE-bpKQ")
        self.db.claim_next()
        counts = self.db.counts()
        self.assertEqual(counts["downloading"], 1)
        self.assertEqual(counts["queued"], 1)
        self.assertEqual(counts["imported"], 0)


    def test_mark_deleted_is_conditional_on_status(self) -> None:
        self.db.create_or_requeue("dQw4w9WgXcQ")
        self.db.update_video("dQw4w9WgXcQ", status="queued", error="worker owns this row")
        row = self.db.mark_deleted("dQw4w9WgXcQ", reason="reconcile", only_status="imported")
        assert row is not None
        self.assertEqual(row["status"], "queued")
        self.assertEqual(row["error"], "worker owns this row")

        self.db.update_video("dQw4w9WgXcQ", status="imported")
        row = self.db.mark_deleted("dQw4w9WgXcQ", reason="reconcile", only_status="imported")
        assert row is not None
        self.assertEqual(row["status"], "deleted")
        self.assertEqual(row["error"], "reconcile")
        self.assertTrue(row["deleted_at"])

    def test_mark_deleted_without_condition_overrides_any_status(self) -> None:
        self.db.create_or_requeue("dQw4w9WgXcQ")
        self.db.update_video("dQw4w9WgXcQ", status="failed")
        row = self.db.mark_deleted("dQw4w9WgXcQ", reason="deleted from panel")
        assert row is not None
        self.assertEqual(row["status"], "deleted")

    def test_channel_ids_counts_rows_per_channel(self) -> None:
        for video_id, channel in (
            ("vid00000001", "UCa"),
            ("vid00000002", "UCa"),
            ("vid00000003", "UCb"),
        ):
            self.db.create_or_requeue(video_id)
            self.db.update_video(video_id, channel_id=channel)
        self.assertEqual(self.db.channel_ids(), {"UCa": 2, "UCb": 1})


if __name__ == "__main__":
    unittest.main()
