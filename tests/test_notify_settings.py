"""WebUI-configurable notification events (SQLite settings + API + page)."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from typing import Any, Mapping

from fastapi.testclient import TestClient

from yt2abs.app import NOTIFY_EVENTS_SETTING, create_app
from yt2abs.config import parse_config
from yt2abs.db import Database
from yt2abs.notify import DiscordNotifier, parse_events

WEBHOOK = "https://discord.com/api/webhooks/123456/token-secret"


class RecordingTransport:
    def __init__(self, *, status: int = 204, exc: BaseException | None = None) -> None:
        self.status = status
        self.exc = exc
        self.calls: list[tuple[str, Mapping[str, Any], float]] = []

    def __call__(self, url: str, payload: Mapping[str, Any], timeout: float) -> int:
        self.calls.append((url, payload, timeout))
        if self.exc is not None:
            raise self.exc
        return self.status


def make_config(root: Path):
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
            "channels": {},
        }
    )


class SettingsStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "settings.sqlite3")
        self.db.initialize()

    def test_roundtrip_and_overwrite(self) -> None:
        self.assertIsNone(self.db.get_setting(NOTIFY_EVENTS_SETTING))
        self.db.set_setting(NOTIFY_EVENTS_SETTING, "imported")
        self.assertEqual(self.db.get_setting(NOTIFY_EVENTS_SETTING), "imported")
        self.db.set_setting(NOTIFY_EVENTS_SETTING, "imported,retry")
        self.assertEqual(self.db.get_setting(NOTIFY_EVENTS_SETTING), "imported,retry")

    def test_empty_value_is_distinct_from_missing(self) -> None:
        self.db.set_setting(NOTIFY_EVENTS_SETTING, "")
        self.assertEqual(self.db.get_setting(NOTIFY_EVENTS_SETTING), "")
        self.assertEqual(parse_events(""), frozenset())

    def test_delete_restores_missing_state(self) -> None:
        self.db.set_setting(NOTIFY_EVENTS_SETTING, "failed")
        self.db.delete_setting(NOTIFY_EVENTS_SETTING)
        self.assertIsNone(self.db.get_setting(NOTIFY_EVENTS_SETTING))


class NotifierRuntimeEventsTests(unittest.TestCase):
    def test_set_events_changes_filter_immediately(self) -> None:
        transport = RecordingTransport()
        notifier = DiscordNotifier(WEBHOOK, events={"imported"}, min_interval=0, transport=transport)
        job = {"video_id": "dQw4w9WgXcQ", "title": "Film"}

        self.assertTrue(notifier.job_failed(job, "boom", 2).skipped)
        notifier.set_events({"failed"})
        self.assertEqual(notifier.events, frozenset({"failed"}))
        self.assertTrue(notifier.job_imported(job, dict(job)).skipped)
        self.assertTrue(notifier.job_failed(job, "boom", 2).delivered)

    def test_set_events_ignores_unknown_names(self) -> None:
        notifier = DiscordNotifier(WEBHOOK, min_interval=0, transport=RecordingTransport())
        updated = notifier.set_events(["imported", "bogus", "IMPORTED"])
        self.assertEqual(updated, frozenset({"imported"}))

    def test_events_property_is_a_snapshot(self) -> None:
        notifier = DiscordNotifier(WEBHOOK, events={"imported"}, min_interval=0,
                                   transport=RecordingTransport())
        snapshot = notifier.events
        notifier.set_events({"failed"})
        self.assertEqual(snapshot, frozenset({"imported"}))
        self.assertEqual(notifier.events, frozenset({"failed"}))


class NotificationApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = make_config(self.root)
        self.db_path = self.root / "data" / "notify.sqlite3"
        saved = os.environ.pop("DISCORD_NOTIFY_EVENTS", None)
        if saved is not None:
            self.addCleanup(lambda: os.environ.__setitem__("DISCORD_NOTIFY_EVENTS", saved))
        self.transport = RecordingTransport()
        self.notifier = DiscordNotifier(
            WEBHOOK, events={"imported"}, min_interval=0, transport=self.transport
        )
        self.app = create_app(
            self.config,
            db=Database(self.db_path),
            worker_enabled=False,
            notifier=self.notifier,
        )
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    # ------------------------------------------------------------------- state
    def test_default_state_never_leaks_webhook(self) -> None:
        response = self.client.get("/api/notifications")
        self.assertEqual(response.status_code, 200)
        state = response.json()
        self.assertTrue(state["configured"])
        self.assertTrue(state["enabled"])
        self.assertFalse(state["disabled_by_env"])
        self.assertEqual(state["source"], "default")
        self.assertEqual(state["available"], ["imported", "retry", "failed"])
        self.assertEqual(
            [entry["name"] for entry in state["events"]],
            ["imported", "retry", "failed"],
        )
        self.assertEqual(
            [entry["enabled"] for entry in state["events"]], [True, False, False]
        )
        self.assertNotIn("token-secret", response.text)
        self.assertNotIn("discord.com", response.text)

    def test_unknown_event_is_rejected_without_side_effects(self) -> None:
        response = self.client.put(
            "/api/notifications", json={"events": ["imported", "nope"]}
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("nope", response.json()["detail"])
        self.assertEqual(self.notifier.events, frozenset({"imported"}))
        self.assertIsNone(self.app.state.db.get_setting(NOTIFY_EVENTS_SETTING))

    def test_save_persists_and_applies_live(self) -> None:
        response = self.client.put(
            "/api/notifications", json={"events": ["failed", "retry", "retry"]}
        )
        self.assertEqual(response.status_code, 200, response.text)
        state = response.json()
        self.assertEqual(state["source"], "webui")
        self.assertEqual(
            [entry["name"] for entry in state["events"] if entry["enabled"]],
            ["retry", "failed"],
        )
        self.assertEqual(self.notifier.events, frozenset({"retry", "failed"}))
        self.assertEqual(
            self.app.state.db.get_setting(NOTIFY_EVENTS_SETTING), "failed,retry"
        )

        job = {"video_id": "dQw4w9WgXcQ", "title": "Film"}
        self.assertTrue(self.notifier.job_retry(job, "timeout", 60.0, 1).delivered)
        self.assertTrue(self.notifier.job_imported(job, dict(job)).skipped)

        health = self.client.get("/api/system").json()
        self.assertEqual(health["notifications"]["events"], ["failed", "retry"])

    def test_save_is_case_insensitive_and_can_disable_all(self) -> None:
        response = self.client.put(
            "/api/notifications", json={"events": ["IMPORTED", "Failed"]}
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.notifier.events, frozenset({"imported", "failed"}))

        empty = self.client.put("/api/notifications", json={"events": []})
        self.assertEqual(empty.status_code, 200, empty.text)
        self.assertEqual(self.notifier.events, frozenset())
        self.assertEqual(
            [entry for entry in empty.json()["events"] if entry["enabled"]], []
        )
        self.assertEqual(self.app.state.db.get_setting(NOTIFY_EVENTS_SETTING), "")
        self.assertEqual(
            self.client.get("/api/system").json()["notifications"]["events"], []
        )

    def test_saved_events_survive_restart(self) -> None:
        self.client.put("/api/notifications", json={"events": ["retry"]})
        self.client.__exit__(None, None, None)

        restarted_notifier = DiscordNotifier(
            WEBHOOK, min_interval=0, transport=RecordingTransport()
        )
        self.assertEqual(restarted_notifier.events, frozenset({"imported", "failed"}))
        app = create_app(
            self.config,
            db=Database(self.db_path),
            worker_enabled=False,
            notifier=restarted_notifier,
        )
        with TestClient(app) as client:
            state = client.get("/api/notifications").json()
        self.assertEqual(restarted_notifier.events, frozenset({"retry"}))
        self.assertEqual(state["source"], "webui")
        self.assertEqual(
            [entry["name"] for entry in state["events"] if entry["enabled"]], ["retry"]
        )

    # -------------------------------------------------------------- test send
    def test_test_endpoint_delivers(self) -> None:
        response = self.client.post("/api/notifications/test")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["delivered"])
        self.assertEqual(response.json()["status"], 204)
        payload = self.transport.calls[0][1]
        self.assertIn("notification test", payload["embeds"][0]["title"])
        self.assertNotIn("token-secret", response.text)

    def test_test_endpoint_reports_failure(self) -> None:
        self.transport.status = 500
        response = self.client.post("/api/notifications/test")
        self.assertEqual(response.status_code, 502)
        self.assertFalse(response.json()["delivered"])
        self.assertIn("500", response.json()["error"])

    def test_test_endpoint_requires_webhook(self) -> None:
        app = create_app(
            self.config,
            db=Database(self.root / "data" / "empty.sqlite3"),
            worker_enabled=False,
            notifier=DiscordNotifier("", transport=RecordingTransport()),
        )
        with TestClient(app) as client:
            response = client.post("/api/notifications/test")
            state = client.get("/api/notifications").json()
        self.assertEqual(response.status_code, 409)
        self.assertIn("DISCORD_WEBHOOK_URL", response.json()["detail"])
        self.assertFalse(state["configured"])
        self.assertFalse(state["enabled"])

    # -------------------------------------------------------------------- page
    def test_settings_page_renders_current_selection(self) -> None:
        response = self.client.get("/settings")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"].split(";")[0], "text/html")
        self.assertEqual(response.text.count('type="checkbox"'), 3)
        self.assertIn('name="events" value="imported" checked', response.text)
        self.assertIn('name="events" value="retry">', response.text)
        self.assertIn("Which operations send a notification", response.text)
        self.assertIn("/api/notifications/test", response.text)
        self.assertNotIn("token-secret", response.text)

    def test_settings_page_reflects_saved_selection(self) -> None:
        self.client.put("/api/notifications", json={"events": ["failed"]})
        page = self.client.get("/settings").text
        self.assertIn('name="events" value="failed" checked', page)
        self.assertIn('name="events" value="imported">', page)
        self.assertIn("panel (WebUI)", page)

    def test_index_links_to_settings(self) -> None:
        index = self.client.get("/")
        self.assertIn('href="/settings"', index.text)


class EnvSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = make_config(self.root)

    def test_env_selection_reports_env_source(self) -> None:
        from unittest import mock

        notifier = DiscordNotifier(
            WEBHOOK, events={"retry"}, min_interval=0, transport=RecordingTransport()
        )
        with mock.patch.dict(os.environ, {"DISCORD_NOTIFY_EVENTS": "retry"}):
            app = create_app(
                self.config,
                db=Database(self.root / "data" / "env.sqlite3"),
                worker_enabled=False,
                notifier=notifier,
            )
        with TestClient(app) as client:
            state = client.get("/api/notifications").json()
            health = client.get("/api/system").json()
        self.assertEqual(state["source"], "env")
        self.assertEqual(
            [entry["name"] for entry in state["events"] if entry["enabled"]], ["retry"]
        )
        self.assertEqual(health["notifications"]["events"], ["retry"])

    def test_env_disable_switch_is_reported(self) -> None:
        notifier = DiscordNotifier(
            WEBHOOK, disabled=True, transport=RecordingTransport()
        )
        app = create_app(
            self.config,
            db=Database(self.root / "data" / "disabled.sqlite3"),
            worker_enabled=False,
            notifier=notifier,
        )
        with TestClient(app) as client:
            state = client.get("/api/notifications").json()
            test = client.post("/api/notifications/test")
        self.assertTrue(state["configured"])
        self.assertFalse(state["enabled"])
        self.assertTrue(state["disabled_by_env"])
        self.assertEqual(test.status_code, 409)


if __name__ == "__main__":
    unittest.main()
