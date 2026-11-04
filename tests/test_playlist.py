"""Playlist polling ("add from phone"): parsing, config, dedupe and wiring.

Everything here is offline: yt-dlp is replaced by a recording fake runner and
the poller never starts the real background thread unless the test asks for it.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

from fastapi.testclient import TestClient

from yt2abs.app import create_app
from yt2abs.config import (
    DEFAULT_PLAYLIST_INTERVAL_MINUTES,
    MIN_PLAYLIST_INTERVAL_MINUTES,
    ConfigError,
    parse_config,
)
from yt2abs.db import SCHEMA, Database
from yt2abs.playlist import (
    PlaylistError,
    PlaylistPoller,
    PlaylistPollResult,
    list_playlist_ids,
    parse_playlist_ids,
    redact_playlist_secrets,
)
from yt2abs.urls import InvalidPlaylistURL, extract_playlist_id

PLAYLIST_URL = "https://www.youtube.com/playlist?list=PLabcdefghij0123456789"
VID_A = "aaaaaaaaaaa"
VID_B = "bbbbbbbbbbb"
VID_C = "ccccccccccc"


# --------------------------------------------------------------------- helpers
def make_config(root: Path, *, playlist: dict[str, Any] | None = None) -> Any:
    """Config with an ABS section that never reaches the network."""
    data: dict[str, Any] = {
        "defaults": {"retention_days": 14},
        "abs": {
            "url": "http://abs.local:13378",
            "api_key": "test-key",
            "library_id": "lib_1",
            "library_path": "/youtube-podcasts",
            "scan_timeout": 5,
            "scan_interval": 0.05,
        },
        "paths": {
            "data_dir": str(root / "data"),
            "tmp_dir": str(root / "tmp"),
            "library_path": str(root / "library"),
        },
        "worker": {"max_attempts": 1, "retry_backoff_seconds": [1], "poll_interval": 0.05},
        "channels": {},
    }
    if playlist is not None:
        data["playlist"] = playlist
    return parse_config(data)


class RecordingRunner:
    """Fake ``subprocess.run``: records argv/timeout and returns canned output."""

    def __init__(
        self,
        *,
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
        error: BaseException | None = None,
    ) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.error = error
        self.calls: list[tuple[list[str], float]] = []

    def __call__(self, args: Any, timeout: float) -> Any:
        self.calls.append((list(args), timeout))
        if self.error is not None:
            raise self.error
        return SimpleNamespace(
            returncode=self.returncode, stdout=self.stdout, stderr=self.stderr
        )

    @property
    def args(self) -> list[str]:
        return self.calls[-1][0]

    @property
    def timeout(self) -> float:
        return self.calls[-1][1]


class FakePoller:
    """Stand-in for the background thread in app wiring tests."""

    def __init__(self, result: PlaylistPollResult | None = None) -> None:
        self.last_result = result
        self.last_run_at = "2026-10-20T06:00:00Z" if result is not None else None
        self.calls: list[float | None] = []
        self.started = threading.Event()
        self.config: Any = None

    def run_forever(self, stop_event: threading.Event, interval_seconds: float | None = None) -> None:
        self.calls.append(interval_seconds)
        self.started.set()
        stop_event.wait(10)


class TempDirTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)


# ------------------------------------------------------------------ id parsing
class ParsePlaylistIdsTests(unittest.TestCase):
    def test_keeps_only_valid_ids(self) -> None:
        output = "\n".join(
            [
                VID_A,
                "",
                "NA",
                "not-a-video-id",
                VID_B,
                "  " + VID_C + "  ",
                "UC" + "x" * 22,
            ]
        )
        self.assertEqual(parse_playlist_ids(output), [VID_A, VID_B, VID_C])

    def test_duplicates_keep_first_position(self) -> None:
        output = f"{VID_B}\n{VID_A}\n{VID_B}\n{VID_C}\n{VID_A}\n"
        self.assertEqual(parse_playlist_ids(output), [VID_B, VID_A, VID_C])

    def test_empty_input(self) -> None:
        self.assertEqual(parse_playlist_ids(""), [])
        self.assertEqual(parse_playlist_ids("\n\n  \n"), [])
        self.assertEqual(parse_playlist_ids(None), [])  # type: ignore[arg-type]

    def test_strips_utf8_bom_from_the_first_line(self) -> None:
        self.assertEqual(parse_playlist_ids("\ufeff" + VID_A + "\n"), [VID_A])

    def test_rejects_wrong_length_and_characters(self) -> None:
        self.assertEqual(parse_playlist_ids("short\n" + "a" * 12 + "\nabc!defghij\n"), [])
        self.assertEqual(parse_playlist_ids("abc-def_123\n"), ["abc-def_123"])


# --------------------------------------------------------------- playlist URL
class ExtractPlaylistIdTests(unittest.TestCase):
    def test_accepts_playlist_pages(self) -> None:
        for url in (
            PLAYLIST_URL,
            "https://m.youtube.com/playlist?list=PLabcdefghij0123456789",
            "https://music.youtube.com/playlist?list=OLAK5uy_abcdefghij",
            "youtube.com/playlist?list=PLabcdefghij0123456789",
            "https://www.youtube.com/playlist/?list=PLabcdefghij0123456789&si=x",
        ):
            with self.subTest(url=url):
                self.assertTrue(extract_playlist_id(url))

    def test_rejects_watch_urls_with_a_list_parameter(self) -> None:
        """A watch URL points at one video; polling it would queue the playlist."""
        with self.assertRaises(InvalidPlaylistURL):
            extract_playlist_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PLabcdefghij")

    def test_rejects_foreign_and_malformed_urls(self) -> None:
        for url in (
            "",
            "https://youtu.be/playlist?list=PLabcdefghij0123456789",
            "https://vimeo.com/playlist?list=PLabcdefghij0123456789",
            "ftp://www.youtube.com/playlist?list=PLabcdefghij0123456789",
            "https://www.youtube.com/playlist",
            "https://www.youtube.com/playlist?list=short",
            "https://www.youtube.com/playlist?list=" + "x" * 129,
        ):
            with self.subTest(url=url):
                with self.assertRaises(InvalidPlaylistURL):
                    extract_playlist_id(url)

    def test_accepts_the_example_from_config_example(self) -> None:
        self.assertEqual(
            extract_playlist_id(
                "https://www.youtube.com/playlist?list=PLxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
            ),
            "PLxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        )


# --------------------------------------------------------------------- config
class PlaylistConfigTests(unittest.TestCase):
    def config(self, **playlist: Any) -> Any:
        return parse_config({"playlist": playlist})

    def test_disabled_by_default(self) -> None:
        config = parse_config({})
        self.assertFalse(config.playlist.enabled)
        self.assertEqual(config.playlist.url, "")
        self.assertEqual(
            config.playlist.interval_minutes, DEFAULT_PLAYLIST_INTERVAL_MINUTES
        )
        self.assertGreaterEqual(MIN_PLAYLIST_INTERVAL_MINUTES, 5)
        self.assertEqual(
            config.playlist.interval_seconds,
            config.playlist.interval_minutes * 60,
        )

    def test_parses_url_and_interval(self) -> None:
        config = self.config(enabled=True, url=PLAYLIST_URL, interval_minutes="5")
        self.assertTrue(config.playlist.enabled)
        self.assertEqual(config.playlist.url, PLAYLIST_URL)
        self.assertEqual(config.playlist.interval_minutes, 5)
        self.assertTrue(config.playlist.configured)

    def test_rejects_interval_below_minimum(self) -> None:
        with self.assertRaises(ConfigError) as caught:
            self.config(interval_minutes=MIN_PLAYLIST_INTERVAL_MINUTES - 1)
        self.assertIn("playlist.interval_minutes", str(caught.exception))

    def test_rejects_negative_and_non_integer_intervals(self) -> None:
        for value in (-1, "abc", 1.5, True):
            with self.subTest(value=value):
                with self.assertRaises(ConfigError):
                    self.config(interval_minutes=value)

    def test_rejects_bad_url_even_when_disabled(self) -> None:
        """A typo must show up on ``yt2abs.cli check``, not on the first poll."""
        with self.assertRaises(ConfigError) as caught:
            self.config(enabled=False, url="https://example.com/playlist?list=PL1234567890")
        self.assertIn("playlist.url", str(caught.exception))

    def test_enabled_requires_url(self) -> None:
        with self.assertRaises(ConfigError) as caught:
            self.config(enabled=True)
        self.assertIn("playlist.url", str(caught.exception))

    def test_rejects_bad_timeout(self) -> None:
        for value in (0, -5, "soon"):
            with self.subTest(value=value):
                with self.assertRaises(ConfigError):
                    self.config(timeout=value)

    def test_unknown_keys_are_ignored(self) -> None:
        config = self.config(enabled=False, url=PLAYLIST_URL, future_option=1)
        self.assertFalse(config.playlist.enabled)


# ------------------------------------------------------------------------- db
class QueueNewTests(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db = Database(self.root / "data" / "queue.sqlite3")
        self.db.initialize()

    def count(self) -> int:
        with self.db.connect() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM videos").fetchone()[0])

    def test_inserts_queued_job_with_playlist_source(self) -> None:
        row, created = self.db.queue_new(VID_A)
        self.assertTrue(created)
        assert row is not None
        self.assertEqual(row["video_id"], VID_A)
        self.assertEqual(row["status"], "queued")
        self.assertEqual(row["source"], "playlist")
        self.assertEqual(row["attempts"], 0)
        self.assertEqual(self.count(), 1)

    def test_known_job_is_never_touched(self) -> None:
        self.db.queue_new(VID_A)
        before = self.db.get_video(VID_A)
        row, created = self.db.queue_new(VID_A)
        self.assertFalse(created)
        self.assertEqual(row, before)

    def test_failed_job_is_not_requeued(self) -> None:
        self.db.queue_new(VID_A)
        self.db.update_video(VID_A, status="failed", attempts=3, error="boom")
        _, created = self.db.queue_new(VID_A)
        self.assertFalse(created)
        row = self.db.get_video(VID_A)
        assert row is not None
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["attempts"], 3)
        self.assertEqual(row["error"], "boom")

    def test_deleted_job_is_not_resurrected(self) -> None:
        row, _ = self.db.create_or_requeue(VID_B)
        self.db.update_video(VID_B, status="deleted")
        _, created = self.db.queue_new(VID_B)
        self.assertFalse(created)
        current = self.db.get_video(VID_B)
        assert current is not None
        self.assertEqual(current["status"], "deleted")
        # the row created by the API path keeps its own source
        self.assertEqual(current["source"], "api")

    def test_migrates_old_database_without_source_column(self) -> None:
        legacy = self.root / "data" / "legacy.sqlite3"
        legacy.parent.mkdir(parents=True, exist_ok=True)
        old_schema = SCHEMA.replace(
            "  source             TEXT NOT NULL DEFAULT 'api',\n", ""
        )
        self.assertNotIn("source", old_schema)
        with sqlite3.connect(legacy) as connection:
            connection.executescript(old_schema)
            connection.execute(
                "INSERT INTO videos (video_id, status, added_at, updated_at) "
                "VALUES (?, 'imported', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')",
                (VID_C,),
            )
        database = Database(legacy)
        database.initialize()
        row = database.get_video(VID_C)
        assert row is not None
        self.assertEqual(row["source"], "api")
        # and the playlist path works on the migrated database
        _, created = database.queue_new(VID_A)
        self.assertTrue(created)


# ------------------------------------------------------------------- listing
class ListPlaylistIdsTests(unittest.TestCase):
    def test_builds_the_flat_playlist_command(self) -> None:
        runner = RecordingRunner(stdout=f"{VID_A}\n{VID_B}\n")
        ids = list_playlist_ids(
            PLAYLIST_URL,
            command="/usr/local/bin/yt-dlp",
            extra_args=("--extractor-args", "youtube:player_client=web_embedded"),
            timeout=42,
            runner=runner,
        )
        self.assertEqual(ids, [VID_A, VID_B])
        self.assertEqual(runner.args[0], "/usr/local/bin/yt-dlp")
        for flag in ("--flat-playlist", "--print", "%(id)s", "--no-warnings", "--ignore-errors"):
            self.assertIn(flag, runner.args)
        self.assertEqual(runner.args[-1], PLAYLIST_URL)
        self.assertIn("youtube:player_client=web_embedded", runner.args)
        self.assertEqual(runner.timeout, 42)

    def test_non_zero_exit_raises_with_the_last_stderr_line(self) -> None:
        runner = RecordingRunner(
            returncode=2,
            stderr=(
                "WARNING: something\n"
                f"ERROR: [youtube:tab] {extract_playlist_id(PLAYLIST_URL)}: "
                "The playlist does not exist.\n"
            ),
        )
        with self.assertRaises(PlaylistError) as caught:
            list_playlist_ids(PLAYLIST_URL, runner=runner)
        message = str(caught.exception)
        self.assertIn("code 2", message)
        self.assertIn("playlist does not exist", message)
        # An unlisted playlist id is a capability token: never leak it.
        self.assertNotIn(extract_playlist_id(PLAYLIST_URL), message)
        self.assertNotIn(PLAYLIST_URL, message)
        self.assertIn("<playlist>", message)

    def test_redacts_the_url_and_the_bare_id(self) -> None:
        playlist_id = extract_playlist_id(PLAYLIST_URL)
        text = f"failed for {PLAYLIST_URL} (id {playlist_id}, list={playlist_id})"
        cleaned = redact_playlist_secrets(text, PLAYLIST_URL)
        self.assertNotIn(playlist_id, cleaned)
        self.assertNotIn(PLAYLIST_URL, cleaned)
        self.assertIn("<playlist>", cleaned)

    def test_non_zero_exit_without_stderr(self) -> None:
        runner = RecordingRunner(returncode=1)
        with self.assertRaises(PlaylistError) as caught:
            list_playlist_ids(PLAYLIST_URL, runner=runner)
        self.assertIn("code 1", str(caught.exception))

    def test_timeout_becomes_playlist_error(self) -> None:
        runner = RecordingRunner(
            error=subprocess.TimeoutExpired(cmd=["yt-dlp"], timeout=120)
        )
        with self.assertRaises(PlaylistError) as caught:
            list_playlist_ids(PLAYLIST_URL, timeout=120, runner=runner)
        self.assertIn("120", str(caught.exception))

    def test_missing_binary_becomes_playlist_error(self) -> None:
        runner = RecordingRunner(error=FileNotFoundError("yt-dlp"))
        with self.assertRaises(PlaylistError) as caught:
            list_playlist_ids(PLAYLIST_URL, command="yt-dlp", runner=runner)
        self.assertIn("yt-dlp", str(caught.exception))


# -------------------------------------------------------------------- poller
class PlaylistPollerTests(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db = Database(self.root / "data" / "poll.sqlite3")
        self.db.initialize()
        self.config = make_config(
            self.root,
            playlist={"enabled": True, "url": PLAYLIST_URL, "interval_minutes": 5},
        )

    def poller(self, ids: list[str]) -> PlaylistPoller:
        return PlaylistPoller(self.config, self.db, lister=lambda url, timeout: list(ids))

    def test_queues_new_ids_once(self) -> None:
        poller = self.poller([VID_A, VID_B])
        first = poller.poll_once()
        self.assertEqual(first.seen, 2)
        self.assertEqual(first.queued, [VID_A, VID_B])
        self.assertEqual(first.skipped, 0)
        self.assertIsNone(first.error)
        self.assertFalse(first.disabled)
        self.assertIs(poller.last_result, first)
        self.assertIsNotNone(poller.last_run_at)
        for video_id in (VID_A, VID_B):
            row = self.db.get_video(video_id)
            assert row is not None
            self.assertEqual(row["status"], "queued")
            self.assertEqual(row["source"], "playlist")

        second = poller.poll_once()
        self.assertEqual(second.seen, 2)
        self.assertEqual(second.queued, [])
        self.assertEqual(second.skipped, 2)

    def test_disabled_poller_never_lists(self) -> None:
        disabled = make_config(
            self.root, playlist={"enabled": False, "url": PLAYLIST_URL}
        )

        def explode(url: str, timeout: float) -> list[str]:
            raise AssertionError("yt-dlp must not run when polling is disabled")

        poller = PlaylistPoller(disabled, self.db, lister=explode)
        result = poller.poll_once()
        self.assertTrue(result.disabled)
        self.assertEqual(result.seen, 0)
        self.assertIsNone(result.error)

    def test_listing_error_is_reported_not_raised(self) -> None:
        playlist_id = extract_playlist_id(PLAYLIST_URL)

        def broken(url: str, timeout: float) -> list[str]:
            raise PlaylistError(
                f"ERROR: [youtube:tab] {playlist_id}: The playlist does not exist."
            )

        poller = PlaylistPoller(self.config, self.db, lister=broken)
        with self.assertLogs("yt2abs.playlist", level="WARNING") as captured:
            result = poller.poll_once()
        self.assertIn("will retry next tick", "\n".join(captured.output))
        self.assertNotIn(playlist_id, "\n".join(captured.output))
        self.assertIn("<playlist>", str(result.error))
        self.assertNotIn(playlist_id, str(result.error))
        self.assertEqual(result.queued, [])
        self.assertEqual(self.db.counts()["queued"], 0)

    def test_unexpected_error_does_not_kill_the_loop(self) -> None:
        calls = {"count": 0}

        def broken(url: str, timeout: float) -> list[str]:
            calls["count"] += 1
            raise RuntimeError("boom")

        poller = PlaylistPoller(self.config, self.db, lister=broken)
        with self.assertLogs("yt2abs.playlist", level="ERROR"):
            poller._safe_poll()  # noqa: SLF001 - the loop body is what must survive
            poller._safe_poll()  # noqa: SLF001
        self.assertEqual(calls["count"], 2)
        self.assertIsNone(poller.last_result)

    def test_run_forever_polls_immediately_and_stops(self) -> None:
        poller = self.poller([VID_A])
        stop = threading.Event()
        thread = threading.Thread(
            target=poller.run_forever, args=(stop, 3600), kwargs={"run_immediately": True}
        )
        thread.start()
        try:
            deadline = threading.Event()
            for _ in range(100):
                if self.db.get_video(VID_A) is not None:
                    break
                deadline.wait(0.02)
        finally:
            stop.set()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive(), "poller thread did not stop")
        self.assertIsNotNone(self.db.get_video(VID_A))


# --------------------------------------------------------------- app wiring
class PlaylistAppWiringTests(TempDirTestCase):
    def start(self, config: Any, poller: FakePoller | None) -> tuple[Any, TestClient]:
        db = Database(config.paths.database_path)
        app = create_app(
            config,
            db=db,
            worker_enabled=False,
            playlist_poller=cast(Any, poller),
        )
        client = TestClient(app)
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        return app, client

    def enabled_config(self, *, url: str = PLAYLIST_URL, interval: int = 5) -> Any:
        return make_config(
            self.root,
            playlist={"enabled": True, "url": url, "interval_minutes": interval},
        )

    def test_enabled_playlist_starts_the_poller_and_reports_status(self) -> None:
        poller = FakePoller(
            PlaylistPollResult(
                started_at="2026-10-20T06:00:00Z",
                finished_at="2026-10-20T06:00:02Z",
                seen=2,
                queued=[VID_A, VID_B],
                skipped=0,
            )
        )
        app, client = self.start(self.enabled_config(), poller)
        self.assertTrue(poller.started.wait(2), "poller thread was not started")
        self.assertEqual(poller.calls, [5 * 60])

        # SEC-1b made the public ``/health`` minimal; playlist diagnostics live
        # in the authenticated ``/api/system``.
        system = client.get("/api/system")
        self.assertEqual(system.status_code, 200)
        payload = system.json()
        playlist = payload["playlist"]
        self.assertTrue(playlist["enabled"])
        self.assertTrue(playlist["configured"])
        self.assertEqual(playlist["interval_minutes"], 5)
        self.assertEqual(playlist["last_run"], "2026-10-20T06:00:00Z")
        self.assertEqual(playlist["last_report"]["seen"], 2)
        self.assertEqual(playlist["last_report"]["queued"], 2)
        self.assertEqual(playlist["last_report"]["skipped"], 0)
        self.assertFalse(playlist["last_report"]["error"])
        self.assertNotIn("PLabcdefghij", json.dumps(payload))
        self.assertIsNotNone(app.state.playlist_thread)

    def test_disabled_config_does_not_start_the_poller(self) -> None:
        config = make_config(self.root, playlist={"enabled": False, "url": PLAYLIST_URL})
        poller = FakePoller()
        app, client = self.start(config, poller)
        self.assertFalse(poller.started.wait(0.2))
        self.assertIsNone(app.state.playlist_thread)
        playlist = client.get("/api/system").json()["playlist"]
        self.assertFalse(playlist["enabled"])
        self.assertTrue(playlist["configured"])  # the URL is set, polling is off

    def test_env_switch_disables_a_configured_playlist(self) -> None:
        poller = FakePoller()
        with patch.dict(os.environ, {"YT2ABS_PLAYLIST_ENABLED": "0"}):
            app, client = self.start(self.enabled_config(), poller)
            self.assertFalse(poller.started.wait(0.2))
            self.assertIsNone(app.state.playlist_thread)
            self.assertFalse(client.get("/api/system").json()["playlist"]["enabled"])

    def test_env_switch_does_not_enable_a_disabled_config(self) -> None:
        """The env switch is a kill-switch: =1 must not override the config."""
        config = make_config(self.root, playlist={"enabled": False, "url": PLAYLIST_URL})
        poller = FakePoller()
        with patch.dict(os.environ, {"YT2ABS_PLAYLIST_ENABLED": "1"}):
            app, client = self.start(config, poller)
            self.assertFalse(poller.started.wait(0.2))
            self.assertIsNone(app.state.playlist_thread)
            self.assertFalse(client.get("/api/system").json()["playlist"]["enabled"])

    def test_real_poller_lists_and_queues_on_startup(self) -> None:
        """The real poller (not a fake) must list on its first tick."""
        config = self.enabled_config()
        db = Database(config.paths.database_path)
        poller = PlaylistPoller(
            config, db, lister=lambda url, timeout: [VID_A, VID_B]
        )
        app = create_app(
            config, db=db, worker_enabled=False, playlist_poller=poller
        )
        with TestClient(app):
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and db.get_video(VID_B) is None:
                time.sleep(0.02)
        for video_id in (VID_A, VID_B):
            row = db.get_video(video_id)
            self.assertIsNotNone(row, f"{video_id} was not queued")
            assert row is not None
            self.assertEqual(row["status"], "queued")
            self.assertEqual(row["source"], "playlist")

    def test_public_health_hides_the_playlist_state(self) -> None:
        """``/health`` is public: it says nothing about the playlist at all.

        The redacted listing error and the poller state stay in the
        authenticated ``/api/system``.
        """
        message = "yt-dlp exited with code 1: <playlist>: The playlist does not exist."
        poller = FakePoller(
            PlaylistPollResult(
                started_at="2026-10-20T06:00:00Z",
                finished_at="2026-10-20T06:00:01Z",
                error=message,
            )
        )
        _, client = self.start(self.enabled_config(), poller)
        health = client.get("/health")
        self.assertNotIn("playlist", health.json())
        self.assertNotIn("does not exist", health.text)
        system = client.get("/api/system")
        self.assertEqual(system.json()["playlist"]["last_report"]["error"], message)
        self.assertNotIn(extract_playlist_id(PLAYLIST_URL), system.text)

    def test_enabled_without_url_is_not_started(self) -> None:
        config = make_config(self.root, playlist={"enabled": False})
        poller = FakePoller()
        with patch.dict(os.environ, {"YT2ABS_PLAYLIST_ENABLED": "1"}):
            app, client = self.start(config, poller)
            self.assertFalse(poller.started.wait(0.2))
            self.assertIsNone(app.state.playlist_thread)
            self.assertFalse(client.get("/api/system").json()["playlist"]["enabled"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
