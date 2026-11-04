"""WebUI API: channel policies, job actions, auth and static assets."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any, cast

from fastapi.testclient import TestClient

from yt2abs.abs_client import AbsError, AbsNotFound
from yt2abs.app import create_app
from yt2abs.channels import parse_channel_reference
from yt2abs.config import parse_config
from yt2abs.db import Database

CONFIG_CHANNEL = "UCconfig0000000000000000"
SECOND_CHANNEL = "UCsecond0000000000000000"


class FakeAbs:
    def __init__(self) -> None:
        self.deleted: list[tuple[str, str, bool]] = []
        self.status = {"serverVersion": "2.37.1", "isScanning": False}
        self.delete_error: str | None = None
        self.status_error: bool = False

    def server_status(self) -> dict[str, Any]:
        if self.status_error:
            raise AbsError("ABS connection error: timed out")
        return dict(self.status)

    def delete_episode(self, item_id: str, episode_id: str, *, hard: bool = True) -> None:
        if self.delete_error == "missing":
            raise AbsNotFound("already gone", status_code=404)
        if self.delete_error == "error":
            raise AbsError("ABS connection error: refused")
        self.deleted.append((item_id, episode_id, hard))


def make_config(root: Path) -> Any:
    return parse_config(
        {
            "defaults": {"retention_days": 14, "protect_unfinished": True},
            "abs": {
                "url": "http://abs.local:13378",
                "api_key": "key",
                "library_id": "lib_1",
                "library_path": "/library",
            },
            "paths": {
                "data_dir": str(root / "data"),
                "tmp_dir": str(root / "tmp"),
                "library_path": str(root / "library"),
            },
            "channels": {
                CONFIG_CHANNEL: {"name": "Kanał z config", "retention_days": 7}
            },
            "api": {"public_url": "https://yt2abs.example.com"},
        }
    )


class WebUiApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "data" / "webui.sqlite3"
        self.abs = FakeAbs()
        self.apps: list[tuple[Any, TestClient]] = []

    def make_client(self) -> tuple[Any, TestClient]:
        config = make_config(self.root)
        app = create_app(
            config,
            db=Database(self.db_path),
            worker_enabled=False,
            abs_client=cast(Any, self.abs),
            channel_resolver=parse_channel_reference,
        )
        client = TestClient(app)
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        self.apps.append((app, client))
        return app, client

    def add_job(self, client: TestClient, video_id: str = "dQw4w9WgXcQ") -> dict[str, Any]:
        response = client.post(
            "/api/jobs",
            json={"url": f"https://youtu.be/{video_id}"},
        )
        self.assertIn(response.status_code, (200, 202), response.text)
        return response.json()

    def test_static_shell_and_api_are_open_without_credentials(self) -> None:
        _, client = self.make_client()
        index = client.get("/")
        self.assertEqual(index.status_code, 200)
        self.assertIn("Queue", index.text)
        self.assertIn("/static/app.js", index.text)
        self.assertEqual(client.get("/static/app.js").status_code, 200)
        self.assertEqual(client.get("/static/styles.css").status_code, 200)

        self.assertEqual(client.get("/api/jobs").status_code, 200)

    def test_job_listing_has_deadline_from_policy(self) -> None:
        _, client = self.make_client()
        job = self.add_job(client)
        retention = job["retention"]
        self.assertEqual(retention["days"], 14)
        self.assertEqual(retention["source"], "defaults")
        self.assertIsNotNone(retention["deadline"])

        self.db = Database(self.db_path)
        self.db.update_video(job["video_id"], channel_id=CONFIG_CHANNEL)
        listing = client.get("/api/jobs").json()["jobs"][0]
        self.assertEqual(listing["retention"]["days"], 7)
        self.assertEqual(listing["retention"]["source"], "channel")

    def test_channel_edit_survives_a_new_app(self) -> None:
        _, client = self.make_client()
        channels = client.get("/api/channels").json()["channels"]
        config_item = next(item for item in channels if item["channel_id"] == CONFIG_CHANNEL)
        self.assertEqual(config_item["retention_days"], 7)
        self.assertTrue(config_item["in_config"])

        updated = client.put(
            f"/api/channels/{CONFIG_CHANNEL}",
            json={"retention_days": None, "name": "Panelowa nazwa"},
        )
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertIsNone(updated.json()["retention_days"])
        self.assertEqual(updated.json()["retention_source"], "ui")
        self.assertEqual(updated.json()["inherited_retention_days"], 7)

        # Adding the same channel again (the UI form only sends an id) must
        # not silently drop the policy stored in the panel.
        again = client.post("/api/channels", json={"channel": CONFIG_CHANNEL})
        self.assertEqual(again.status_code, 200, again.text)
        self.assertIsNone(again.json()["channel"]["retention_days"])
        self.assertEqual(again.json()["channel"]["retention_source"], "ui")

        # A fresh app process reads the same SQLite database (container restart).
        _, restarted = self.make_client()
        item = next(
            channel
            for channel in restarted.get("/api/channels").json()["channels"]
            if channel["channel_id"] == CONFIG_CHANNEL
        )
        self.assertEqual(item["name"], "Panelowa nazwa")
        self.assertIsNone(item["retention_days"])
        self.assertEqual(item["retention_source"], "ui")

        reset = restarted.delete(f"/api/channels/{CONFIG_CHANNEL}")
        self.assertEqual(reset.status_code, 200)
        item = next(
            channel
            for channel in restarted.get("/api/channels").json()["channels"]
            if channel["channel_id"] == CONFIG_CHANNEL
        )
        self.assertEqual(item["retention_days"], 7)
        self.assertEqual(item["retention_source"], "config")

    def test_add_channel_by_id_and_reject_foreign_link(self) -> None:
        _, client = self.make_client()
        created = client.post(
            "/api/channels",
            json={"channel": SECOND_CHANNEL, "name": "Nowy", "retention_days": 3},
        )
        self.assertEqual(created.status_code, 201, created.text)
        self.assertTrue(created.json()["created"])
        self.assertEqual(created.json()["channel"]["retention_days"], 3)

        again = client.post("/api/channels", json={"channel": SECOND_CHANNEL})
        self.assertEqual(again.status_code, 200)
        self.assertFalse(again.json()["created"])

        bad = client.post("/api/channels", json={"channel": "https://vimeo.com/123"})
        self.assertEqual(bad.status_code, 400)
        self.assertIn("YouTube", bad.json()["detail"])

    def test_retention_override_bounds_and_reset(self) -> None:
        _, client = self.make_client()
        job = self.add_job(client)
        url = f"/api/jobs/{job['video_id']}/retention"

        too_big = client.post(url, json={"override": 40000})
        self.assertEqual(too_big.status_code, 422, too_big.text)

        missing_field = client.post(url, json={})
        self.assertEqual(missing_field.status_code, 422, missing_field.text)

        set_days = client.post(url, json={"override": 5})
        self.assertEqual(set_days.status_code, 200, set_days.text)
        self.assertEqual(set_days.json()["retention_override"], 5)

        reset = client.post(url, json={"override": None})
        self.assertEqual(reset.status_code, 200, reset.text)
        self.assertIsNone(reset.json()["retention_override"])
        self.assertEqual(reset.json()["retention"]["source"], "defaults")

        keep = client.post(url, json={"override": 0})
        self.assertEqual(keep.status_code, 200)
        self.assertEqual(keep.json()["retention_override"], 0)
        default = client.post(url, json={"override": "default"})
        self.assertEqual(default.status_code, 200, default.text)
        self.assertIsNone(default.json()["retention_override"])

    def test_retry_failed_job(self) -> None:
        _, client = self.make_client()
        job = self.add_job(client)
        self.db = Database(self.db_path)
        self.db.mark_failed(job["video_id"], "boom")
        failed = client.post(f"/api/jobs/{job['video_id']}/retry")
        self.assertEqual(failed.status_code, 200, failed.text)
        payload = failed.json()
        self.assertEqual(payload["status"], "queued")
        self.assertEqual(payload["attempts"], 0)
        self.assertIsNone(payload["error"])

        again = client.post(f"/api/jobs/{job['video_id']}/retry")
        self.assertEqual(again.status_code, 409)

    def test_delete_imported_job_uses_abs_hard_delete(self) -> None:
        _, client = self.make_client()
        job = self.add_job(client)
        self.db = Database(self.db_path)
        self.db.update_video(
            job["video_id"],
            status="imported",
            abs_item_id="item-1",
            abs_episode_id="episode-1",
            file_path=str(self.root / "library" / "missing.m4a"),
        )
        response = client.delete(f"/api/jobs/{job['video_id']}")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "deleted")
        self.assertEqual(self.abs.deleted, [("item-1", "episode-1", True)])
        active = [
            item
            for item in client.get("/api/jobs").json()["jobs"]
            if item["status"] != "deleted"
        ]
        self.assertEqual(active, [])

    def test_delete_keeps_job_when_abs_fails(self) -> None:
        _, client = self.make_client()
        job = self.add_job(client)
        self.db = Database(self.db_path)
        self.db.update_video(
            job["video_id"],
            status="imported",
            abs_item_id="item-1",
            abs_episode_id="episode-1",
        )
        self.abs.delete_error = "error"
        response = client.delete(f"/api/jobs/{job['video_id']}")
        self.assertEqual(response.status_code, 502, response.text)
        self.assertEqual(
            client.get(f"/api/jobs/{job['video_id']}").json()["status"], "imported"
        )

    def test_delete_treats_a_missing_abs_episode_as_deleted_and_removes_file(self) -> None:
        _, client = self.make_client()
        job = self.add_job(client)
        self.db = Database(self.db_path)
        file_path = self.root / "library" / "stale.m4a"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(b"audio")
        self.db.update_video(
            job["video_id"],
            status="imported",
            abs_item_id="item-1",
            abs_episode_id="episode-1",
            file_path=str(file_path),
        )
        self.abs.delete_error = "missing"
        response = client.delete(f"/api/jobs/{job['video_id']}")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "deleted")
        self.assertFalse(file_path.exists())

    def test_delete_queued_job_does_not_call_abs(self) -> None:
        _, client = self.make_client()
        job = self.add_job(client)
        response = client.delete(f"/api/jobs/{job['video_id']}")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "deleted")
        self.assertEqual(self.abs.deleted, [])

    def test_keep_forever_override(self) -> None:
        _, client = self.make_client()
        job = self.add_job(client)
        response = client.post(
            f"/api/jobs/{job['video_id']}/retention", json={"override": 0}
        )
        self.assertEqual(response.status_code, 200, response.text)
        retention = response.json()["retention"]
        self.assertTrue(retention["keep"])
        self.assertIsNone(retention["deadline"])
        self.assertEqual(retention["source"], "keep")

        cleared = client.post(
            f"/api/jobs/{job['video_id']}/retention", json={"override": None}
        )
        self.assertEqual(cleared.status_code, 200)
        self.assertEqual(cleared.json()["retention"]["source"], "defaults")
        self.assertEqual(cleared.json()["retention"]["days"], 14)

    def test_retention_values_are_bounded(self) -> None:
        _, client = self.make_client()
        too_large = client.post(
            "/api/jobs",
            json={"url": "https://youtu.be/dQw4w9WgXcQ", "retention_override": 10_000_000},
        )
        self.assertEqual(too_large.status_code, 422)
        job = self.add_job(client)
        too_large_job = client.post(
            f"/api/jobs/{job['video_id']}/retention",
            json={"override": 10_000_000},
        )
        self.assertEqual(too_large_job.status_code, 422)
        too_large_channel = client.put(
            f"/api/channels/{CONFIG_CHANNEL}", json={"retention_days": 10_000_000}
        )
        self.assertEqual(too_large_channel.status_code, 400)

    def test_abs_status_endpoint(self) -> None:
        _, client = self.make_client()
        status = client.get("/api/abs/status")
        self.assertEqual(status.status_code, 200)
        payload = status.json()
        self.assertTrue(payload["configured"])
        self.assertTrue(payload["reachable"])
        self.assertEqual(payload["version"], "2.37.1")
        self.assertEqual(payload["library_id"], "lib_1")

    def test_abs_status_reports_an_unreachable_server(self) -> None:
        _, client = self.make_client()
        self.abs.status_error = True
        payload = client.get("/api/abs/status").json()
        self.assertTrue(payload["configured"])
        self.assertFalse(payload["reachable"])
        self.assertIn("timed out", payload["error"])


if __name__ == "__main__":
    unittest.main()
