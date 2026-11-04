"""FastAPI endpoint tests (worker disabled, no network)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from yt2abs.app import create_app
from yt2abs.config import parse_config
from yt2abs.db import Database


def make_config(root: Path, channels=None):
    return parse_config(
        {
            "defaults": {"retention_days": 14},
            "abs": {
                "url": "http://abs.local:13378",
                "api_key": "test-key",
                "library_id": "lib_1",
                "library_path": "/youtube-podcasts",
            },
            "paths": {
                "data_dir": str(root / "data"),
                "tmp_dir": str(root / "tmp"),
                "library_path": str(root / "library"),
            },
            "worker": {"max_attempts": 2, "retry_backoff_seconds": [1, 2], "poll_interval": 0.1},
            "channels": channels or {},
        }
    )


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = make_config(self.root)
        self.db = Database(self.root / "data" / "api.sqlite3")
        self.app = create_app(self.config, db=self.db, worker_enabled=False)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def test_create_job_and_idempotency(self) -> None:
        response = self.client.post(
            "/api/jobs", json={"url": "https://youtu.be/dQw4w9WgXcQ?t=5"}
        )
        self.assertEqual(response.status_code, 202, response.text)
        payload = response.json()
        self.assertEqual(payload["video_id"], "dQw4w9WgXcQ")
        self.assertEqual(payload["status"], "queued")
        self.assertTrue(payload["created"])
        self.assertEqual(payload["url"], "https://www.youtube.com/watch?v=dQw4w9WgXcQ")

        repeat = self.client.post(
            "/api/jobs", json={"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PL1"}
        )
        self.assertEqual(repeat.status_code, 200)
        self.assertFalse(repeat.json()["created"])
        self.assertEqual(repeat.json()["status"], "queued")
        with self.db.connect() as connection:
            count = connection.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
        self.assertEqual(count, 1)

    def test_get_job_and_listing(self) -> None:
        self.client.post("/api/jobs", json={"url": "https://youtu.be/dQw4w9WgXcQ"})
        response = self.client.get("/api/jobs/dQw4w9WgXcQ")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["video_id"], "dQw4w9WgXcQ")

        listing = self.client.get("/api/jobs")
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(len(listing.json()["jobs"]), 1)
        self.assertEqual(listing.json()["counts"]["queued"], 1)

        self.assertEqual(self.client.get("/api/jobs/missing").status_code, 404)
        self.assertEqual(self.client.get("/api/jobs?status=nope").status_code, 400)

    def test_retention_override(self) -> None:
        response = self.client.post(
            "/api/jobs",
            json={"url": "https://youtu.be/dQw4w9WgXcQ", "retention_override": 3},
        )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["retention_override"], 3)
        invalid = self.client.post(
            "/api/jobs",
            json={"url": "https://youtu.be/dQw4w9WgXcQ", "retention_override": -1},
        )
        self.assertEqual(invalid.status_code, 422)

    def test_rejects_non_youtube_url(self) -> None:
        for url in ("https://vimeo.com/123", "https://youtube.com/playlist?list=PL1", "nonsense"):
            with self.subTest(url=url):
                response = self.client.post("/api/jobs", json={"url": url})
                self.assertEqual(response.status_code, 400)
                self.assertIn("YouTube", response.json()["detail"])

    def test_add_page(self) -> None:
        response = self.client.get("/add", params={"url": "https://youtu.be/dQw4w9WgXcQ"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("Added to the queue", response.text)
        self.assertIn("dQw4w9WgXcQ", response.text)
        self.assertEqual(response.headers["content-type"].split(";")[0], "text/html")

        bad = self.client.get("/add", params={"url": "https://example.com/x"})
        self.assertEqual(bad.status_code, 400)
        missing = self.client.get("/add")
        self.assertEqual(missing.status_code, 400)

    def test_job_page_is_readonly_and_add_never_requeues(self) -> None:
        self.client.post("/api/jobs", json={"url": "https://youtu.be/dQw4w9WgXcQ"})
        self.db.update_video("dQw4w9WgXcQ", status="failed", attempts=2, error="boom")

        page = self.client.get("/jobs/dQw4w9WgXcQ")
        self.assertEqual(page.status_code, 200, page.text)
        self.assertIn("Read-only view", page.text)
        self.assertIn("Retry import", page.text)
        self.assertIn("id=\"retry-button\"", page.text)
        # Merely opening the page must not restart the job.
        self.assertEqual(self.db.get_video("dQw4w9WgXcQ")["status"], "failed")
        self.assertEqual(self.db.get_video("dQw4w9WgXcQ")["attempts"], 2)

        # /add is the bookmarklet target: opening or refreshing it (F5) must
        # report the status, never requeue a failed/deleted job.
        add = self.client.get("/add", params={"url": "https://youtu.be/dQw4w9WgXcQ"})
        self.assertEqual(add.status_code, 200)
        self.assertIn("separate action", add.text)
        self.assertEqual(self.db.get_video("dQw4w9WgXcQ")["status"], "failed")
        self.assertEqual(self.db.get_video("dQw4w9WgXcQ")["attempts"], 2)

        self.assertEqual(self.client.get("/jobs/unknownXXXX").status_code, 404)

        # Retry stays an explicit POST.
        retried = self.client.post("/api/jobs/dQw4w9WgXcQ/retry")
        self.assertEqual(retried.status_code, 200, retried.text)
        self.assertEqual(retried.json()["status"], "queued")

    def test_index_and_health(self) -> None:
        self.client.post("/api/jobs", json={"url": "https://youtu.be/dQw4w9WgXcQ"})
        index = self.client.get("/")
        self.assertEqual(index.status_code, 200)
        self.assertIn("Queue", index.text)
        self.assertIn("/static/app.js", index.text)
        self.assertEqual(self.client.get("/static/app.js").status_code, 200)

        health = self.client.get("/health")
        self.assertEqual(health.status_code, 200)
        payload = health.json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(
            sorted(payload["alive"]), ["retention", "updater", "worker"]
        )
        # The public probe must not leak anything but status/liveness.
        self.assertEqual(sorted(payload), ["alive", "status"])

        system = self.client.get("/api/system")
        self.assertEqual(system.status_code, 200)
        details = system.json()
        self.assertNotIn("auth", details)
        self.assertEqual(details["api"]["rate_limit_per_minute"], 60)
        self.assertFalse(details["retention"]["enabled"])
        self.assertIn("counts", details)

    def test_retention_endpoints_when_worker_disabled(self) -> None:
        status = self.client.get("/api/retention")
        self.assertEqual(status.status_code, 200)
        self.assertFalse(status.json()["enabled"])
        self.assertEqual(
            self.client.post("/api/retention/run").status_code, 409
        )

    def test_system_endpoint(self) -> None:
        response = self.client.get("/api/system")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["abs"]["library_id"], "lib_1")
        self.assertNotIn("auth", payload)
        self.assertEqual(payload["api"]["public_rate_limit_per_minute"], 10)
        self.assertEqual(payload["defaults"]["retention_days"], 14)
        self.assertIn("ytdlp", payload)

    def test_bookmarklet_page(self) -> None:
        response = self.client.get("/bookmarklet")
        self.assertEqual(response.status_code, 200)
        self.assertIn("/add?url=", response.text)
        self.assertIn("Firefox", response.text)
        self.assertIn("javascript:", response.text)

    def test_policy_endpoint(self) -> None:
        config = make_config(
            self.root,
            channels={
                "UCseven00001": {"name": "Seven days", "retention_days": 7, "protect_unfinished": False},
                "UCkeep000001": {"name": "Keep me", "retention_days": None},
            },
        )
        db = Database(self.root / "data" / "policy.sqlite3")
        app = create_app(config, db=db, worker_enabled=False)
        with TestClient(app) as client:
            by_id = client.get("/api/policy", params={"channel_id": "UCseven00001"})
            self.assertEqual(by_id.status_code, 200, by_id.text)
            payload = by_id.json()
            self.assertEqual(payload["channel_id"], "UCseven00001")
            self.assertEqual(payload["channel_name"], "Seven days")
            self.assertEqual(payload["retention_days"], 7)
            self.assertFalse(payload["protect_unfinished"])
            self.assertEqual(payload["source"], "channel")
            self.assertEqual(payload["matched_by"], "id")

            by_name = client.get("/api/policy", params={"name": "  keep ME "})
            self.assertEqual(by_name.status_code, 200, by_name.text)
            payload = by_name.json()
            self.assertEqual(payload["channel_id"], "UCkeep000001")
            self.assertIsNone(payload["retention_days"])
            self.assertEqual(payload["source"], "channel")
            self.assertEqual(payload["matched_by"], "name")

            fallback = client.get(
                "/api/policy", params={"channel_id": "UCunknown1", "name": "Nobody"}
            )
            payload = fallback.json()
            self.assertEqual(payload["source"], "defaults")
            self.assertIsNone(payload["matched_by"])
            self.assertEqual(payload["retention_days"], 14)
            self.assertEqual(payload["channel_name"], "Nobody")
            self.assertTrue(payload["protect_unfinished"])

            empty = client.get("/api/policy")
            self.assertEqual(empty.status_code, 200)
            self.assertEqual(empty.json()["source"], "defaults")
            self.assertEqual(empty.json()["retention_days"], 14)

    def test_readding_deleted_video_starts_clean(self) -> None:
        self.client.post("/api/jobs", json={"url": "https://youtu.be/dQw4w9WgXcQ"})
        self.db.update_video(
            "dQw4w9WgXcQ",
            status="deleted",
            file_path="/library/old.m4a",
            abs_item_id="item-old",
            abs_episode_id="episode-old",
            deleted_at="2026-01-01T00:00:00Z",
        )
        response = self.client.post(
            "/api/jobs", json={"url": "https://youtu.be/dQw4w9WgXcQ"}
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["requeued"])
        self.assertEqual(payload["status"], "queued")
        self.assertIsNone(payload["file_path"])
        self.assertIsNone(payload["abs_item_id"])
        self.assertIsNone(payload["abs_episode_id"])
        self.assertIsNone(payload["deleted_at"])


if __name__ == "__main__":
    unittest.main()
