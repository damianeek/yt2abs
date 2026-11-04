"""App wiring: background threads, /health liveness and retention endpoints."""
from __future__ import annotations

import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from fastapi.testclient import TestClient

from yt2abs.app import create_app
from yt2abs.config import parse_config
from yt2abs.db import Database
from yt2abs.retention import RetentionJob, RetentionReport
from yt2abs.updater import UpdateResult


class FakeRetention:
    def __init__(self) -> None:
        self.last_report = RetentionReport(
            started_at="2026-10-19T20:00:00Z",
            finished_at="2026-10-19T20:00:01Z",
            checked=3,
            kept=1,
            deleted=[
                {
                    "video_id": "dQw4w9WgXcQ",
                    "file_path": "/library/secret/path.m4a",
                    "item_id": "item",
                    "episode_id": "episode",
                }
            ],
        )
        self.last_run_at = "2026-10-19T20:00:01Z"
        self.running = False
        self.runs = 0

    def run_once(self, **_: Any) -> RetentionReport:
        self.runs += 1
        return self.last_report

    def try_start(self) -> bool:
        if self.running:
            return False
        self.runs += 1
        return True

    def preview(self) -> RetentionReport:
        return replace(self.last_report, dry_run=True, planned=[])

    def run_forever(self, stop_event: threading.Event, interval_seconds: float, **_: Any) -> None:
        stop_event.wait(30)


class FakeUpdater:
    current_version = "2026.08.19"
    last_result = UpdateResult(
        checked_at="2026-10-19T20:00:00Z",
        previous="2026.08.19",
        latest="2026.8.19",
        current="2026.08.19",
    )

    def version_of(self) -> str:
        return self.current_version

    def update(self) -> UpdateResult:
        return self.last_result

    def run_forever(self, stop_event: threading.Event, interval_seconds: float) -> None:
        stop_event.wait(30)


class AppWiringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = parse_config(
            {
                "defaults": {"retention_days": 14},
                "abs": {
                    "url": "http://abs.local",
                    "api_key": "key",
                    "library_id": "lib",
                    "library_path": "/lib",
                },
                "paths": {
                    "data_dir": str(self.root / "data"),
                    "tmp_dir": str(self.root / "tmp"),
                    "library_path": str(self.root / "library"),
                },
                "channels": {},
            }
        )
        self.db = Database(self.root / "data" / "wiring.sqlite3")
        self.retention = FakeRetention()
        self.updater = FakeUpdater()
        pipeline = SimpleNamespace(
            process=lambda job, report: {},
            abs=object(),
            discard=lambda fields: None,
        )
        self.app = create_app(
            self.config,
            db=self.db,
            pipeline=cast(Any, pipeline),
            worker_enabled=True,
            retention_job=cast(RetentionJob, self.retention),
            updater=cast(Any, self.updater),
        )
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def test_health_uses_compact_retention_summary(self) -> None:
        health = self.client.get("/health")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json()["status"], "ok")
        self.assertNotIn("retention", health.json())

        payload = self.client.get("/api/system").json()
        self.assertTrue(payload["retention"]["enabled"])
        report = payload["retention"]["last_report"]
        self.assertEqual(report["checked"], 3)
        # /api/system returns the full report, where "deleted" is the list of
        # rows (the compact count lives in the removed /health payload).
        self.assertEqual(len(report["deleted"]), 1)
        # File paths are diagnostics: they belong behind the token, never in the
        # public liveness payload.
        self.assertNotIn("/library/secret/path.m4a", self.client.get("/health").text)
        self.assertEqual(payload["ytdlp"]["version"], "2026.08.19")

    def test_worker_gets_the_pipeline_discard_callback(self) -> None:
        """Regression: ``process`` is a bound method and hides ``discard``."""
        worker = self.app.state.worker
        self.assertIsNotNone(worker)
        self.assertIsNotNone(worker.discard)

    def test_retention_and_update_endpoints(self) -> None:
        # Synchronous variant for tests/E2E; the UI uses the 202 background path.
        run = self.client.post("/api/retention/run", params={"wait": "true"})
        self.assertEqual(run.status_code, 200)
        self.assertEqual(run.json()["checked"], 3)
        status = self.client.get("/api/retention")
        self.assertEqual(len(status.json()["last_report"]["deleted"]), 1)
        update = self.client.post("/api/ytdlp/update")
        self.assertEqual(update.status_code, 200)
        self.assertEqual(update.json()["current"], "2026.08.19")

    def test_retention_run_is_background_by_default(self) -> None:
        run = self.client.post("/api/retention/run")
        self.assertEqual(run.status_code, 202, run.text)
        self.assertEqual(run.json()["status"], "started")
        self.assertEqual(run.json()["poll"], "/api/retention")
        self.assertEqual(self.retention.runs, 1)

    def test_retention_run_rejects_a_second_pass(self) -> None:
        self.retention.running = True
        second = self.client.post("/api/retention/run")
        self.assertEqual(second.status_code, 409, second.text)
        preview = self.client.get("/api/retention/preview")
        self.assertEqual(preview.status_code, 409)
        self.retention.running = False

    def test_retention_preview_endpoint(self) -> None:
        preview = self.client.get("/api/retention/preview")
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertTrue(preview.json()["dry_run"])


if __name__ == "__main__":
    unittest.main()
