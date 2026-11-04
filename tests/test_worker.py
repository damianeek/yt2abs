"""Worker queue/retry tests with a fake pipeline."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any, Mapping

from yt2abs.db import Database
from yt2abs.worker import Worker


class Clock:
    def __init__(self, value: str = "2026-01-01T00:00:00Z") -> None:
        self.value = value

    def __call__(self) -> str:
        return self.value


class FakeProcess:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = list(outcomes)
        self.reports: list[str] = []

    def __call__(self, job: Mapping[str, Any], report) -> Any:
        assert job["status"] == "downloading"
        report("downloading")
        self.reports.append("downloading")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class DeletingProcess(FakeProcess):
    """Simulates the WebUI deleting a job while the worker is processing it."""

    def __init__(self, db: Database, outcomes: list[Any]) -> None:
        super().__init__(outcomes)
        self.db = db
        self.discarded: list[Mapping[str, Any]] = []

    def __call__(self, job: Mapping[str, Any], report) -> Any:
        self.db.mark_deleted(str(job["video_id"]), reason="deleted in UI")
        return super().__call__(job, report)

    def discard(self, fields: Mapping[str, Any]) -> None:
        self.discarded.append(dict(fields))


class RecordingNotifier:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[Any, ...]] = []

    def _record(self, name: str, *args: Any) -> None:
        self.calls.append((name, *args))
        if self.fail:
            raise RuntimeError("notification boom")

    def job_imported(self, job: Mapping[str, Any], fields: Mapping[str, Any]) -> None:
        self._record("imported", job, fields)

    def job_retry(
        self, job: Mapping[str, Any], error: str, delay: float, attempts: int
    ) -> None:
        self._record("retry", job, error, delay, attempts)

    def job_failed(self, job: Mapping[str, Any], error: str, attempts: int) -> None:
        self._record("failed", job, error, attempts)


class WorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "worker.sqlite3")
        self.db.initialize()
        self.clock = Clock()

    def make_worker(
        self,
        process: FakeProcess,
        *,
        max_attempts: int = 3,
        notifier: RecordingNotifier | None = None,
    ) -> Worker:
        return Worker(
            self.db,
            process,
            max_attempts=max_attempts,
            retry_backoff=(60, 300, 900),
            poll_interval=0.01,
            clock=self.clock,
            wait=lambda seconds: None,
            notifier=notifier,
        )

    def test_success_marks_imported(self) -> None:
        self.db.create_or_requeue("dQw4w9WgXcQ")
        worker = self.make_worker(
            FakeProcess([{"file_path": "/library/x.m4a", "abs_item_id": "li_1"}])
        )
        self.assertTrue(worker.run_once())
        row = self.db.get_video("dQw4w9WgXcQ")
        assert row is not None
        self.assertEqual(row["status"], "imported")
        self.assertEqual(row["file_path"], "/library/x.m4a")
        self.assertEqual(row["abs_item_id"], "li_1")

    def test_failure_is_retried_with_backoff(self) -> None:
        self.db.create_or_requeue("dQw4w9WgXcQ")
        worker = self.make_worker(FakeProcess([RuntimeError("temporary 403")]))
        self.assertTrue(worker.run_once())

        row = self.db.get_video("dQw4w9WgXcQ")
        assert row is not None
        self.assertEqual(row["status"], "queued")
        self.assertEqual(row["attempts"], 1)
        self.assertIn("temporary 403", row["error"])
        self.assertTrue(row["next_attempt_at"])
        self.assertIsNone(self.db.claim_next(now=self.clock()))

        self.clock.value = row["next_attempt_at"]
        self.assertIsNotNone(self.db.claim_next(now=self.clock()))

    def test_failed_after_max_attempts(self) -> None:
        self.db.create_or_requeue("dQw4w9WgXcQ")
        worker = self.make_worker(
            FakeProcess([RuntimeError("boom-1"), RuntimeError("boom-2"), RuntimeError("boom-3")]),
            max_attempts=3,
        )
        for _ in range(3):
            self.assertTrue(worker.run_once())
            row = self.db.get_video("dQw4w9WgXcQ")
            assert row is not None
            if row["next_attempt_at"]:
                self.clock.value = row["next_attempt_at"]

        row = self.db.get_video("dQw4w9WgXcQ")
        assert row is not None
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["attempts"], 3)
        self.assertIn("boom-3", row["error"])
        self.assertFalse(worker.run_once())

    def test_empty_queue_returns_false(self) -> None:
        worker = self.make_worker(FakeProcess([]))
        self.assertFalse(worker.run_once())

    def test_report_callback_gets_stage(self) -> None:
        self.db.create_or_requeue("dQw4w9WgXcQ")
        process = FakeProcess([{"file_path": None}])
        worker = self.make_worker(process)
        worker.run_once()
        self.assertEqual(process.reports, ["downloading"])

    def test_success_notifies_imported(self) -> None:
        self.db.create_or_requeue("dQw4w9WgXcQ")
        notifier = RecordingNotifier()
        worker = self.make_worker(
            FakeProcess(
                [
                    {
                        "file_path": "/library/x.m4a",
                        "title": "Film",
                        "channel_title": "Kanał",
                    }
                ]
            ),
            notifier=notifier,
        )
        self.assertTrue(worker.run_once())
        self.assertEqual(notifier.calls[0][0], "imported")
        job, fields = notifier.calls[0][1], notifier.calls[0][2]
        self.assertEqual(job["title"], "Film")
        self.assertEqual(fields["file_path"], "/library/x.m4a")

    def test_retry_notifies_with_delay(self) -> None:
        self.db.create_or_requeue("dQw4w9WgXcQ")
        notifier = RecordingNotifier()
        worker = self.make_worker(
            FakeProcess([RuntimeError("temporary 403")]), notifier=notifier
        )
        self.assertTrue(worker.run_once())
        self.assertEqual(notifier.calls[0][0], "retry")
        self.assertEqual(notifier.calls[0][2], "RuntimeError: temporary 403")
        self.assertEqual(notifier.calls[0][3], 60.0)
        self.assertEqual(notifier.calls[0][4], 1)

    def test_final_failure_notifies_failed(self) -> None:
        self.db.create_or_requeue("dQw4w9WgXcQ")
        notifier = RecordingNotifier()
        worker = self.make_worker(
            FakeProcess(
                [RuntimeError("boom-1"), RuntimeError("boom-2"), RuntimeError("boom-3")]
            ),
            notifier=notifier,
        )
        for _ in range(3):
            self.assertTrue(worker.run_once())
            row = self.db.get_video("dQw4w9WgXcQ")
            assert row is not None
            if row["next_attempt_at"]:
                self.clock.value = row["next_attempt_at"]

        self.assertEqual([call[0] for call in notifier.calls], ["retry", "retry", "failed"])
        self.assertEqual(notifier.calls[-1][2], "RuntimeError: boom-3")
        self.assertEqual(notifier.calls[-1][3], 3)

    def test_notifier_failure_does_not_break_the_worker(self) -> None:
        self.db.create_or_requeue("dQw4w9WgXcQ")
        notifier = RecordingNotifier(fail=True)
        worker = self.make_worker(
            FakeProcess([{"file_path": "/library/x.m4a"}]), notifier=notifier
        )
        self.assertTrue(worker.run_once())
        row = self.db.get_video("dQw4w9WgXcQ")
        assert row is not None
        self.assertEqual(row["status"], "imported")
        self.assertEqual(len(notifier.calls), 1)

    def test_deleted_while_processing_discards_the_result(self) -> None:
        self.db.create_or_requeue("dQw4w9WgXcQ")
        process = DeletingProcess(
            self.db,
            [{"file_path": "/library/x.m4a", "abs_item_id": "li_1", "abs_episode_id": "ep_1"}],
        )
        worker = self.make_worker(process)
        self.assertTrue(worker.run_once())
        row = self.db.get_video("dQw4w9WgXcQ")
        assert row is not None
        self.assertEqual(row["status"], "deleted")
        self.assertEqual(len(process.discarded), 1)
        self.assertEqual(process.discarded[0]["abs_episode_id"], "ep_1")

    def test_deleted_while_processing_does_not_schedule_retry(self) -> None:
        self.db.create_or_requeue("dQw4w9WgXcQ")
        process = DeletingProcess(self.db, [RuntimeError("boom")])
        worker = self.make_worker(process)
        self.assertTrue(worker.run_once())
        row = self.db.get_video("dQw4w9WgXcQ")
        assert row is not None
        self.assertEqual(row["status"], "deleted")
        self.assertIsNone(row["next_attempt_at"])
        self.assertEqual(process.discarded, [])

    def test_explicit_discard_callback_survives_a_bound_process_method(self) -> None:
        """The production wiring passes ``pipeline.process`` (bound) + discard."""
        self.db.create_or_requeue("dQw4w9WgXcQ")
        discarded: list[Mapping[str, Any]] = []

        class Pipeline:
            def __init__(self, db: Database) -> None:
                self.db = db

            def process(self, job: Mapping[str, Any], report) -> Mapping[str, Any]:
                self.db.mark_deleted(str(job["video_id"]), reason="deleted in UI")
                return {"file_path": "/library/x.m4a", "abs_item_id": "li_1"}

            def discard(self, fields: Mapping[str, Any]) -> None:
                discarded.append(dict(fields))

        pipeline = Pipeline(self.db)
        worker = Worker(
            self.db,
            pipeline.process,
            discard=pipeline.discard,
            max_attempts=1,
            poll_interval=0.01,
            clock=self.clock,
        )
        self.assertTrue(worker.run_once())
        self.assertEqual(len(discarded), 1)
        self.assertEqual(discarded[0]["abs_item_id"], "li_1")


if __name__ == "__main__":
    unittest.main()
