"""Import pipeline tests with fake yt-dlp and a simulated Audiobookshelf."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping
from urllib.parse import urlparse

from yt2abs.abs_client import AbsClient, AbsError
from yt2abs.config import parse_config
from yt2abs.db import Database
from yt2abs.downloader import DownloadError
from yt2abs.metadata import VideoMetadata
from yt2abs.pipeline import FALLBACK_ARGS, ImportPipeline, PipelineError, sanitize_folder_name


class AbsSimulator:
    """Just enough ABS API to exercise the scan/find/patch flow."""

    def __init__(self, library_root: Path, folder: str) -> None:
        self.library_root = library_root
        self.folder = folder
        self.scanned = False
        self.scan_calls = 0
        self.item_scans = 0
        self.episode_updates: list[dict[str, Any]] = []
        self.podcast_updates: list[dict[str, Any]] = []
        self.deleted_episodes: list[tuple[str, str, str]] = []
        self.index: dict[str, str] = {}

    @property
    def item_path(self) -> Path:
        return self.library_root / self.folder

    def items(self) -> list[dict[str, Any]]:
        if not self.scanned or not self.item_path.exists():
            return []
        return [{"id": "li_1", "path": f"/youtube-podcasts/{self.folder}"}]

    def episodes(self) -> list[dict[str, Any]]:
        if not self.scanned or not self.item_path.exists():
            return []
        result = []
        for number, path in enumerate(sorted(self.item_path.glob("*.m4a")), start=1):
            episode_id = f"ep_{number}"
            self.index[path.name] = episode_id
            result.append(
                {
                    "id": episode_id,
                    "audioFile": {
                        "metadata": {"filename": path.name, "path": str(path)},
                        "path": str(path),
                    },
                }
            )
        return result

    def __call__(self, method: str, url: str, headers: Mapping[str, str], body: bytes | None):
        parsed = urlparse(url)
        path = parsed.path
        payload = json.loads(body) if body else None
        if method == "POST" and path.endswith("/scan") and "/libraries/" in path:
            self.scanned = True
            self.scan_calls += 1
            return 200, b"{}"
        if method == "POST" and path == "/api/items/li_1/scan":
            self.item_scans += 1
            return 200, b"{}"
        if method == "GET" and path.endswith("/items"):
            return 200, json.dumps({"results": self.items()}).encode()
        if method == "GET" and path.startswith("/api/libraries/"):
            # Cheap preflight before downloading (review finding #3).
            return 200, json.dumps({"id": path.rsplit("/", 1)[-1], "name": "Podcasts"}).encode()
        if method == "GET" and path == "/api/items/li_1":
            return 200, json.dumps({"id": "li_1", "media": {"episodes": self.episodes()}}).encode()
        if method == "PATCH" and "/episode/" in path:
            self.episode_updates.append(payload or {})
            return 200, b"{}"
        if method == "PATCH" and path.endswith("/media"):
            self.podcast_updates.append(payload or {})
            return 200, b"{}"
        if method == "DELETE" and "/episode/" in path:
            item_id = path.split("/api/podcasts/", 1)[1].split("/", 1)[0]
            episode_id = path.rsplit("/", 1)[-1]
            self.deleted_episodes.append((item_id, episode_id, parsed.query))
            return 200, b""
        return 404, b'{"error":"not found"}'


class FakeDownloader:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, url, dest, config, *, channel_id=None, video_id=None, extra=()):
        self.calls.append(
            {"url": url, "channel_id": channel_id, "video_id": video_id, "extra": tuple(extra)}
        )
        destination = Path(dest) / f"2026-01-02 - Test video [{video_id}].m4a"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"fake-audio")
        return SimpleNamespace(path=str(destination))


class Monotonic:
    def __init__(self, step: float = 10.0) -> None:
        self.value = 0.0
        self.step = step

    def __call__(self) -> float:
        self.value += self.step
        return self.value


def metadata_for(video_id: str = "dQw4w9WgXcQ", **overrides: Any) -> VideoMetadata:
    values = {
        "video_id": video_id,
        "channel_id": "UC1",
        "channel_title": "Rick Astley",
        "channel_url": "https://www.youtube.com/channel/UC1",
        "title": "Test video",
        "description": "Opis filmu",
        "published_at": "2013-10-25",
        "thumbnail_url": "https://img.example/video.jpg",
    }
    values.update(overrides)
    return VideoMetadata(**values)


class PipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.library = self.root / "library"
        self.library.mkdir()
        self.config = parse_config(
            {
                "defaults": {"retention_days": 14},
                "abs": {
                    "url": "http://abs.local",
                    "api_key": "key",
                    "library_id": "lib_1",
                    "library_path": "/youtube-podcasts",
                    "scan_timeout": 90,
                    "scan_interval": 1,
                },
                "paths": {
                    "data_dir": str(self.root / "data"),
                    "tmp_dir": str(self.root / "tmp"),
                    "library_path": str(self.library),
                },
                "channels": {},
            }
        )
        self.db = Database(self.root / "data" / "pipeline.sqlite3")
        self.db.initialize()
        self.simulator = AbsSimulator(self.library, "Rick Astley")
        self.abs = AbsClient("http://abs.local", "key", sender=self.simulator)
        self.downloader = FakeDownloader()
        self.cover_calls: list[tuple[str, str]] = []

        def download_cover(url: str, destination) -> bool:
            self.cover_calls.append((url, str(destination)))
            Path(destination).write_bytes(b"fake-cover")
            return True

        self.pipeline = ImportPipeline(
            self.config,
            self.db,
            self.abs,
            metadata_fetcher=lambda url, **kwargs: metadata_for(),
            avatar_fetcher=lambda url, **kwargs: "https://img.example/avatar.jpg",
            cover_downloader=download_cover,
            downloader=self.downloader,
            sleep=lambda seconds: None,
            monotonic=Monotonic(),
        )
        self.db.create_or_requeue("dQw4w9WgXcQ")
        self.job = self.db.claim_next()
        assert self.job is not None

    def test_full_import(self) -> None:
        stages: list[str] = []
        outcome = self.pipeline.process(self.job, report=stages.append)

        expected_file = self.library / "Rick Astley" / "2026-01-02 - Test video [dQw4w9WgXcQ].m4a"
        self.assertTrue(expected_file.exists())
        self.assertEqual(expected_file.read_bytes(), b"fake-audio")
        self.assertTrue((self.library / "Rick Astley" / "cover.jpg").exists())
        self.assertFalse(any((self.root / "tmp").glob("*.m4a")))

        self.assertEqual(outcome.abs_item_id, "li_1")
        self.assertEqual(outcome.abs_episode_id, "ep_1")
        self.assertEqual(outcome.file_path, str(expected_file))
        self.assertEqual(stages, ["metadata", "downloading", "publishing", "importing"])

        row = self.db.get_video("dQw4w9WgXcQ")
        assert row is not None
        self.assertEqual(row["file_path"], str(expected_file))
        self.assertEqual(row["channel_id"], "UC1")
        self.assertEqual(row["title"], "Test video")
        self.assertEqual(row["published_at"], "2013-10-25")

        self.assertEqual(len(self.simulator.episode_updates), 1)
        update = self.simulator.episode_updates[0]
        self.assertEqual(update["title"], "Test video")
        # ABS keeps pubDate (RSS string) and publishedAt (epoch ms) independent.
        self.assertEqual(update["pubDate"], "Fri, 25 Oct 2013 00:00:00 +0000")
        self.assertEqual(update["publishedAt"], 1382659200000)
        self.assertIn("Opis filmu", update["description"])
        self.assertIn(
            "Source: https://www.youtube.com/watch?v=dQw4w9WgXcQ", update["description"]
        )
        self.assertEqual(self.simulator.podcast_updates[0]["metadata"]["title"], "Rick Astley")

        channel = self.db.get_channel("UC1")
        assert channel is not None
        self.assertEqual(channel["folder_name"], "Rick Astley")
        self.assertIsNotNone(channel["cover_written_at"])

    def test_second_video_reuses_channel_folder_and_cover(self) -> None:
        self.pipeline.process(self.job, report=lambda stage: None)
        self.db.create_or_requeue("aqz-KE-bpKQ")
        second = self.db.claim_next()
        assert second is not None
        self.pipeline.metadata_fetcher = lambda url, **kwargs: metadata_for("aqz-KE-bpKQ")
        self.pipeline.process(second, report=lambda stage: None)

        folders = sorted(path.name for path in self.library.iterdir())
        self.assertEqual(folders, ["Rick Astley"])
        self.assertEqual(len(list((self.library / "Rick Astley" / "cover.jpg").parent.glob("*.m4a"))), 2)
        self.assertEqual(len(self.cover_calls), 1)
        with self.db.connect() as connection:
            channels = connection.execute("SELECT COUNT(*) FROM channels").fetchone()[0]
        self.assertEqual(channels, 1)

    def test_live_stream_is_rejected_before_download(self) -> None:
        self.pipeline.metadata_fetcher = lambda url, **kwargs: metadata_for(is_live=True)
        with self.assertRaises(PipelineError):
            self.pipeline.process(self.job, report=lambda stage: None)
        self.assertEqual(self.downloader.calls, [])

    def test_abs_timeout_raises_import_error(self) -> None:
        from dataclasses import replace

        from yt2abs.pipeline import AbsImportError

        self.simulator.scanned = False
        self.pipeline.config = replace(
            self.config,
            abs=replace(self.config.abs, scan_timeout=5, scan_interval=1),
        )

        def empty_abs(method, url, headers, body):
            if method == "GET" and urlparse(url).path.endswith("/items"):
                return 200, b'{"results": []}'
            return 200, b"{}"

        self.pipeline.abs = AbsClient("http://abs.local", "key", sender=empty_abs)
        with self.assertRaises(AbsImportError):
            self.pipeline.process(self.job, report=lambda stage: None)

    def test_published_at_uses_the_exact_ytdlp_timestamp(self) -> None:
        # yt-dlp's ``timestamp`` is the real upload moment; a bare date would
        # sort the episode at midnight UTC.
        self.pipeline.metadata_fetcher = lambda url, **kwargs: metadata_for(
            published_at="2009-10-25", published_timestamp=1256453853
        )
        self.pipeline.process(self.job, report=lambda stage: None)
        update = self.simulator.episode_updates[0]
        self.assertEqual(update["publishedAt"], 1256453853000)
        self.assertEqual(update["pubDate"], "Sun, 25 Oct 2009 06:57:33 +0000")

    def test_retry_after_publish_does_not_trigger_another_scan(self) -> None:
        """One scan per wait, and none at all when the episode already exists.

        ABS 2.37 indexed the same file twice when ``scan_library`` was followed
        immediately by ``scan_item`` (two episodes, one inode). The wait loop
        now asks for exactly one kind of scan, and a retry that finds the
        episode passively does not scan at all.
        """
        self.pipeline.process(self.job, report=lambda stage: None)
        self.assertEqual(self.simulator.scan_calls, 1)
        self.assertEqual(self.simulator.item_scans, 0)

        self.db.update_video("dQw4w9WgXcQ", status="failed", error="ABS down")
        self.db.create_or_requeue("dQw4w9WgXcQ")
        job = self.db.claim_next()
        assert job is not None
        self.pipeline.process(job, report=lambda stage: None)

        self.assertEqual(self.simulator.scan_calls, 1)
        self.assertEqual(self.simulator.item_scans, 0)

    def test_abs_unreachable_fails_before_downloading(self) -> None:
        self.pipeline.abs = AbsClient(
            "http://abs.local", "key", sender=lambda method, url, headers, body: (503, b"{}")
        )
        with self.assertRaises(PipelineError):
            self.pipeline.process(self.job, report=lambda stage: None)
        self.assertEqual(self.downloader.calls, [])

    def test_retry_after_publish_reuses_the_file_and_cleans_tmp(self) -> None:
        """A failed ABS import must not download the audio a second time."""
        from yt2abs.pipeline import AbsImportError

        published = (
            self.library / "Rick Astley" / "2026-01-02 - Test video [dQw4w9WgXcQ].m4a"
        )
        working = self.simulator

        def broken_item_scan(method, url, headers, body):
            if method == "GET" and urlparse(url).path == "/api/items/li_1":
                return 500, b'{"error":"boom"}'
            return working(method, url, headers, body)

        # Attempt 1: the file is published, then ABS never lists the episode.
        self.pipeline.abs = AbsClient("http://abs.local", "key", sender=broken_item_scan)
        self.pipeline.monotonic = Monotonic(step=1000)
        with self.assertRaises(AbsImportError):
            self.pipeline.process(self.job, report=lambda stage: None)

        self.assertEqual(len(self.downloader.calls), 1)
        self.assertTrue(published.exists())
        self.assertEqual(list((self.root / "tmp").glob("*")), [])
        row = self.db.get_video("dQw4w9WgXcQ")
        assert row is not None
        self.assertEqual(row["file_path"], str(published))

        # Attempt 2 (worker retry): requeue keeps file_path, pipeline reuses it.
        self.db.update_video("dQw4w9WgXcQ", status="failed", error="ABS down")
        action = self.db.create_or_requeue("dQw4w9WgXcQ")
        self.assertEqual(action[1], "requeued")
        second = self.db.claim_next()
        assert second is not None
        self.pipeline.abs = self.abs
        self.pipeline.monotonic = Monotonic()
        tmp_dir = self.root / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        leftover = tmp_dir / "leftover [dQw4w9WgXcQ].m4a"
        leftover.write_bytes(b"junk")

        outcome = self.pipeline.process(second, report=lambda stage: None)

        self.assertEqual(len(self.downloader.calls), 1)  # no second download
        self.assertEqual(outcome.abs_episode_id, "ep_1")
        self.assertEqual(len(list(published.parent.glob("*.m4a"))), 1)
        self.assertFalse(leftover.exists())
        row = self.db.get_video("dQw4w9WgXcQ")
        assert row is not None
        self.assertEqual(row["file_path"], str(published))

    def test_retry_removes_a_duplicate_left_by_a_title_change(self) -> None:
        self.pipeline.process(self.job, report=lambda stage: None)
        folder = self.library / "Rick Astley"
        published = folder / "2026-01-02 - Test video [dQw4w9WgXcQ].m4a"
        stale = folder / "2013-10-25 - Old title [dQw4w9WgXcQ].m4a"
        stale.write_bytes(b"old-download")

        self.db.update_video("dQw4w9WgXcQ", status="failed")
        self.db.create_or_requeue("dQw4w9WgXcQ")
        job = self.db.claim_next()
        assert job is not None
        self.pipeline.process(job, report=lambda stage: None)

        self.assertEqual(sorted(path.name for path in folder.glob("*.m4a")), [published.name])
        self.assertEqual(stale.exists(), False)
        self.assertEqual(len(self.downloader.calls), 1)

    def test_missing_preferred_file_falls_back_to_a_glob_match(self) -> None:
        self.pipeline.process(self.job, report=lambda stage: None)
        folder = self.library / "Rick Astley"
        published = folder / "2026-01-02 - Test video [dQw4w9WgXcQ].m4a"
        self.db.update_video(
            "dQw4w9WgXcQ", status="failed", file_path=str(folder / "gone [dQw4w9WgXcQ].m4a")
        )
        self.db.create_or_requeue("dQw4w9WgXcQ")
        job = self.db.claim_next()
        assert job is not None
        outcome = self.pipeline.process(job, report=lambda stage: None)
        self.assertEqual(outcome.file_path, str(published))
        self.assertEqual(len(self.downloader.calls), 1)

    def test_falls_back_to_bestaudio_when_m4a_is_missing(self) -> None:
        class FallbackDownloader(FakeDownloader):
            def __init__(self) -> None:
                super().__init__()
                self.extras: list[tuple[str, ...]] = []

            def __call__(self, url, dest, config, *, channel_id=None, video_id=None, extra=()):
                self.extras.append(tuple(extra))
                if len(self.extras) == 1:
                    raise DownloadError("Requested format is not available")
                return super().__call__(
                    url, dest, config, channel_id=channel_id, video_id=video_id, extra=extra
                )

        downloader = FallbackDownloader()
        self.pipeline.downloader = downloader
        self.pipeline.process(self.job, report=lambda stage: None)
        self.assertEqual(len(downloader.extras), 2)
        self.assertEqual(downloader.extras[1], FALLBACK_ARGS)

    def test_cross_device_move_uses_copy_and_atomic_publish(self) -> None:
        import os
        from unittest.mock import patch

        real_replace = os.replace
        calls = {"count": 0}

        def fake_replace(source, destination):
            calls["count"] += 1
            if calls["count"] == 1:
                raise OSError(18, "Invalid cross-device link")
            return real_replace(source, destination)

        with patch("yt2abs.pipeline.os.replace", side_effect=fake_replace):
            self.pipeline.process(self.job, report=lambda stage: None)

        expected = self.library / "Rick Astley" / "2026-01-02 - Test video [dQw4w9WgXcQ].m4a"
        self.assertTrue(expected.exists())
        self.assertFalse(any(expected.parent.glob(".*.part")))
        self.assertFalse(any((self.root / "tmp").glob("*.m4a")))

    def test_space_metadata_is_not_required(self) -> None:
        # A description is optional; the source link must still be present.
        self.pipeline.metadata_fetcher = lambda url, **kwargs: metadata_for(description="")
        self.pipeline.process(self.job, report=lambda stage: None)
        self.assertEqual(
            self.simulator.episode_updates[0]["description"],
            "Source: https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        )

    def test_discard_removes_the_abs_episode_and_the_file(self) -> None:
        media = self.library / "Rick Astley" / "discarded.m4a"
        media.parent.mkdir(parents=True, exist_ok=True)
        media.write_bytes(b"audio")
        self.pipeline.discard(
            {
                "file_path": str(media),
                "abs_item_id": "li_1",
                "abs_episode_id": "ep_1",
            }
        )
        self.assertEqual(len(self.simulator.deleted_episodes), 1)
        self.assertEqual(self.simulator.deleted_episodes[0][:2], ("li_1", "ep_1"))
        self.assertIn("hard=1", self.simulator.deleted_episodes[0][2])
        self.assertFalse(media.exists())


class SanitizeFolderNameTests(unittest.TestCase):
    def test_replaces_path_separators_and_controls(self) -> None:
        name = sanitize_folder_name("AC/DC: Live?\x07 / Best")
        self.assertNotIn("/", name)
        self.assertNotIn(":", name)
        self.assertNotIn("\x07", name)
        self.assertIn("\u29f8", name)

    def test_truncates_and_falls_back(self) -> None:
        self.assertEqual(sanitize_folder_name("   ", fallback="UC1"), "UC1")
        self.assertLessEqual(len(sanitize_folder_name("x" * 300)), 180)
        multibyte = sanitize_folder_name("ż" * 200)
        self.assertLessEqual(len(multibyte.encode("utf-8")), 180)
        self.assertFalse(multibyte.endswith("\ufffd"))


if __name__ == "__main__":
    unittest.main()
