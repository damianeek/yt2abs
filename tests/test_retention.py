"""Retention job: policy hierarchy, protect_unfinished and ABS reconcile."""
from __future__ import annotations

import copy
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

from yt2abs.abs_client import AbsError, AbsNotFound, EpisodeProgress
from yt2abs.config import parse_config
from yt2abs.db import Database
from yt2abs.retention import RetentionJob, resolve_retention_days

NOW = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)


def item(item_id: str = "item-1", episodes: list[str] | None = None) -> dict[str, Any]:
    episode_ids = episodes if episodes is not None else ["ep-1"]
    return {
        "id": item_id,
        "path": "/youtube-podcasts/Channel",
        "media": {
            "episodes": [
                {
                    "id": episode_id,
                    "title": f"Episode {episode_id}",
                    "audioFile": {
                        "metadata": {"filename": f"{episode_id}.m4a", "path": f"/lib/{episode_id}.m4a"}
                    },
                }
                for episode_id in episode_ids
            ]
        },
    }


def other_item() -> dict[str, Any]:
    """An unrelated podcast, so the library listing is never empty by accident."""
    entry = item("sentinel", episodes=["s1"])
    entry["path"] = "/youtube-podcasts/Sentinel"
    return entry


class FakeAbs:
    def __init__(self, items: list[dict[str, Any]] | None = None) -> None:
        self.items = {entry["id"]: entry for entry in (items or [item()])}
        self.progress: dict[tuple[str, str], EpisodeProgress | Exception] = {}
        self.progress_by_user: dict[str, dict[tuple[str, str], EpisodeProgress]] = {}
        self.deleted: list[tuple[str, str, bool]] = []
        self.delete_error: Exception | None = None
        self.list_error: Exception | None = None
        self.progress_calls = 0
        self.get_item_calls = 0

    def list_library_items(self, library_id: str) -> list[dict[str, Any]]:
        if self.list_error:
            raise self.list_error
        return [copy.deepcopy(entry) for entry in self.items.values()]

    def get_item(self, item_id: str) -> dict[str, Any]:
        self.get_item_calls += 1
        if item_id not in self.items:
            raise AbsNotFound(f"missing item {item_id}", status_code=404)
        return copy.deepcopy(self.items[item_id])

    @staticmethod
    def find_episode_by_id(entry: dict[str, Any], episode_id: str) -> dict[str, Any] | None:
        for episode in entry.get("media", {}).get("episodes", []):
            if episode.get("id") == episode_id:
                return episode
        return None

    def get_media_progress(self, item_id: str, episode_id: str) -> EpisodeProgress | None:
        self.progress_calls += 1
        value = self.progress.get((item_id, episode_id))
        if isinstance(value, Exception):
            raise value
        return value

    def delete_episode(self, item_id: str, episode_id: str, *, hard: bool = True) -> None:
        if self.delete_error:
            raise self.delete_error
        if item_id not in self.items:
            raise AbsNotFound("missing item", status_code=404)
        episodes = self.items[item_id].get("media", {}).get("episodes", [])
        if not any(episode.get("id") == episode_id for episode in episodes):
            raise AbsNotFound("missing episode", status_code=404)
        self.items[item_id]["media"]["episodes"] = [
            episode for episode in episodes if episode.get("id") != episode_id
        ]
        self.deleted.append((item_id, episode_id, hard))

    # Helpers used by the fallback path (stored ids missing).
    def find_item_by_path(self, library_id: str, path: str, *, items=None):
        for entry in items or self.items.values():
            if entry.get("path") == path:
                return copy.deepcopy(entry)
        return None

    def find_episode_by_filename(self, item_id: str, filename: str, *, item=None):
        matches = self.find_episodes_by_filename(item_id, filename, item=item)
        return matches[0] if matches else None

    def find_episodes_by_filename(self, item_id: str, filename: str, *, item=None):
        entry = item or self.get_item(item_id)
        matches = []
        for episode in entry.get("media", {}).get("episodes", []):
            audio = episode.get("audioFile", {}).get("metadata", {})
            if audio.get("filename") == filename:
                matches.append(episode)
        return matches


def make_config(*, library_path: str = "/youtube-podcasts", progress_user: str = "", **channels: Any):
    return parse_config(
        {
            "defaults": {"retention_days": 14, "protect_unfinished": True},
            "abs": {
                "url": "http://abs.local",
                "api_key": "key",
                "library_id": "lib-1",
                "library_path": "/youtube-podcasts",
                "progress_user": progress_user,
            },
            "paths": {
                "data_dir": "data",
                "tmp_dir": "tmp",
                "library_path": library_path,
            },
            "channels": channels,
        }
    )


class RetentionTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "retention.sqlite3")
        self.db.initialize()
        self.abs = FakeAbs()
        self.library = Path(self.tmp.name) / "library"
        self.library.mkdir()

    def config(self, *, progress_user: str = "", **channels: Any):
        return make_config(
            library_path=str(self.library), progress_user=progress_user, **channels
        )

    def add_video(
        self,
        video_id: str = "dQw4w9WgXcQ",
        *,
        channel_id: str = "UC_short",
        episode_id: str = "ep-1",
        added_at: datetime | None = None,
        override: int | None = None,
        status: str = "imported",
        file_on_disk: bool = True,
    ) -> dict[str, Any]:
        moment = (added_at or (NOW - timedelta(days=30))).isoformat(timespec="seconds").replace(
            "+00:00", "Z"
        )
        path = self.library / "Channel" / f"{video_id}.m4a"
        # The podcast folder always exists in a real library (cover, metadata),
        # even when the audio file itself is gone.
        path.parent.mkdir(parents=True, exist_ok=True)
        if file_on_disk:
            path.write_bytes(b"audio")
        self.db.create_or_requeue(video_id, retention_override=override)
        self.db.update_video(
            video_id,
            channel_id=channel_id,
            status=status,
            added_at=moment,
            file_path=str(path),
            abs_item_id="item-1",
            abs_episode_id=episode_id,
            title="Test",
        )
        row = self.db.get_video(video_id)
        assert row is not None
        return row

    def job(self, config, notifier=None) -> RetentionJob:
        return RetentionJob(
            config,
            self.db,
            cast(Any, self.abs),
            now=lambda: NOW,
            notifier=notifier,
        )


class PolicyTests(RetentionTestCase):
    def test_delete_after_deadline(self) -> None:
        config = self.config(UC_short={"retention_days": 1})
        self.add_video(added_at=NOW - timedelta(days=2))
        report = self.job(config).run_once()
        self.assertEqual(self.abs.deleted, [("item-1", "ep-1", True)])
        row = self.db.get_video("dQw4w9WgXcQ")
        assert row is not None
        self.assertEqual(row["status"], "deleted")
        self.assertTrue(row["deleted_at"])
        self.assertEqual(len(report.deleted), 1)

    def test_zero_days_deletes_on_next_run(self) -> None:
        config = self.config(UC_short={"retention_days": 0})
        self.add_video(added_at=NOW)
        report = self.job(config).run_once()
        self.assertEqual(len(report.deleted), 1)
        self.assertEqual(self.abs.deleted, [("item-1", "ep-1", True)])

    def test_keep_channel_is_never_deleted(self) -> None:
        config = self.config(UC_keep={"retention_days": None})
        self.add_video(channel_id="UC_keep", added_at=NOW - timedelta(days=365))
        report = self.job(config).run_once()
        self.assertEqual(self.abs.deleted, [])
        self.assertEqual(report.kept, 1)
        row = self.db.get_video("dQw4w9WgXcQ")
        assert row is not None
        self.assertEqual(row["status"], "imported")

    def test_override_zero_keeps_forever(self) -> None:
        config = self.config(UC_short={"retention_days": 0})
        self.add_video(added_at=NOW - timedelta(days=100), override=0)
        report = self.job(config).run_once()
        self.assertEqual(self.abs.deleted, [])
        self.assertEqual(report.kept, 1)

    def test_override_beats_channel(self) -> None:
        config = self.config(UC_short={"retention_days": 0})
        self.add_video(added_at=NOW - timedelta(days=2), override=5)
        report = self.job(config).run_once()
        self.assertEqual(self.abs.deleted, [])
        self.assertEqual(report.kept, 1)

    def test_unknown_channel_falls_back_to_defaults(self) -> None:
        config = self.config()
        self.add_video(channel_id="UC_unknown", added_at=NOW - timedelta(days=15))
        report = self.job(config).run_once()
        self.assertEqual(len(report.deleted), 1)

    def test_before_deadline_is_kept(self) -> None:
        config = self.config(UC_short={"retention_days": 7})
        self.add_video(added_at=NOW - timedelta(days=3))
        report = self.job(config).run_once()
        self.assertEqual(self.abs.deleted, [])
        self.assertEqual(report.kept, 1)


class ProtectUnfinishedTests(RetentionTestCase):
    def test_unfinished_is_deferred_until_double_window(self) -> None:
        config = self.config(UC_short={"retention_days": 1, "protect_unfinished": True})
        self.add_video(added_at=NOW - timedelta(days=1, hours=12))
        self.abs.progress[("item-1", "ep-1")] = EpisodeProgress(
            current_time=60, duration=600, progress=0.1, started_at="2026-06-14T00:00:00Z"
        )
        report = self.job(config).run_once()
        self.assertEqual(self.abs.deleted, [])
        self.assertEqual(len(report.deferred), 1)
        self.assertEqual(report.deferred[0]["reason"], "unfinished")

    def test_unfinished_is_deleted_after_double_window(self) -> None:
        config = self.config(UC_short={"retention_days": 1, "protect_unfinished": True})
        self.add_video(added_at=NOW - timedelta(days=2, hours=12))
        self.abs.progress[("item-1", "ep-1")] = EpisodeProgress(
            current_time=60, duration=600, progress=0.1, started_at="2026-06-13T00:00:00Z"
        )
        report = self.job(config).run_once()
        self.assertEqual(len(report.deleted), 1)

    def test_finished_episode_is_deleted_at_deadline(self) -> None:
        config = self.config(UC_short={"retention_days": 1, "protect_unfinished": True})
        self.add_video(added_at=NOW - timedelta(days=1, hours=1))
        self.abs.progress[("item-1", "ep-1")] = EpisodeProgress(
            current_time=600, duration=600, progress=1.0, is_finished=True
        )
        report = self.job(config).run_once()
        self.assertEqual(len(report.deleted), 1)

    def test_protect_false_ignores_progress(self) -> None:
        config = self.config(UC_short={"retention_days": 1, "protect_unfinished": False})
        self.add_video(added_at=NOW - timedelta(days=1, hours=1))
        self.abs.progress[("item-1", "ep-1")] = EpisodeProgress(
            current_time=60, duration=600, progress=0.1
        )
        report = self.job(config).run_once()
        self.assertEqual(len(report.deleted), 1)

    def test_zero_days_ignores_unfinished_protection(self) -> None:
        config = self.config(UC_short={"retention_days": 0, "protect_unfinished": True})
        self.add_video(added_at=NOW)
        self.abs.progress[("item-1", "ep-1")] = EpisodeProgress(
            current_time=30, duration=600, progress=0.05
        )
        report = self.job(config).run_once()
        self.assertEqual(len(report.deleted), 1)
        self.assertEqual(self.abs.deleted, [("item-1", "ep-1", True)])

    def test_progress_error_blocks_deletion(self) -> None:
        config = self.config(UC_short={"retention_days": 1, "protect_unfinished": True})
        self.add_video(added_at=NOW - timedelta(days=5))
        self.abs.progress[("item-1", "ep-1")] = AbsError("ABS down")
        report = self.job(config).run_once()
        self.assertEqual(self.abs.deleted, [])
        self.assertEqual(len(report.errors), 1)
        row = self.db.get_video("dQw4w9WgXcQ")
        assert row is not None
        self.assertEqual(row["status"], "imported")


class ReconcileTests(RetentionTestCase):
    def test_manually_deleted_podcast_is_marked_deleted_before_deadline(self) -> None:
        config = self.config(UC_default={"retention_days": 14})
        self.add_video(channel_id="UC_default", added_at=NOW, file_on_disk=False)
        self.abs.items = {"sentinel": other_item()}
        report = self.job(config).run_once()
        self.assertEqual(report.reconciled[0]["reason"], "item_missing")
        row = self.db.get_video("dQw4w9WgXcQ")
        assert row is not None
        self.assertEqual(row["status"], "deleted")

    def test_manually_deleted_podcast_is_marked_deleted(self) -> None:
        config = self.config(UC_short={"retention_days": 1})
        self.add_video(added_at=NOW - timedelta(days=10), file_on_disk=False)
        self.abs.items = {"sentinel": other_item()}
        report = self.job(config).run_once()
        self.assertEqual(self.abs.deleted, [])
        self.assertEqual(report.reconciled[0]["reason"], "item_missing")
        row = self.db.get_video("dQw4w9WgXcQ")
        assert row is not None
        self.assertEqual(row["status"], "deleted")

    def test_manually_deleted_episode_is_marked_deleted(self) -> None:
        config = self.config(UC_short={"retention_days": 1})
        self.add_video(added_at=NOW - timedelta(days=10), file_on_disk=False)
        self.abs.items["item-1"]["media"]["episodes"] = []
        report = self.job(config).run_once()
        self.assertEqual(self.abs.deleted, [])
        self.assertEqual(report.reconciled[0]["reason"], "episode_missing")
        row = self.db.get_video("dQw4w9WgXcQ")
        assert row is not None
        self.assertEqual(row["status"], "deleted")

    def test_delete_not_found_is_treated_as_deleted(self) -> None:
        config = self.config(UC_short={"retention_days": 1})
        self.add_video(added_at=NOW - timedelta(days=10), file_on_disk=False)

        def missing(*args, **kwargs):
            raise AbsNotFound("gone", status_code=404)

        self.abs.delete_episode = missing  # type: ignore[assignment]
        report = self.job(config).run_once()
        self.assertEqual(len(report.reconciled), 1)
        row = self.db.get_video("dQw4w9WgXcQ")
        assert row is not None
        self.assertEqual(row["status"], "deleted")

    def test_delete_error_keeps_imported_for_next_run(self) -> None:
        config = self.config(UC_short={"retention_days": 1})
        self.add_video(added_at=NOW - timedelta(days=10))
        self.abs.delete_error = AbsError("ABS 500")
        report = self.job(config).run_once()
        self.assertEqual(len(report.errors), 1)
        row = self.db.get_video("dQw4w9WgXcQ")
        assert row is not None
        self.assertEqual(row["status"], "imported")

    def test_missing_ids_are_recovered_from_file_path(self) -> None:
        config = self.config(UC_short={"retention_days": 1})
        self.add_video(added_at=NOW - timedelta(days=10))
        self.db.update_video("dQw4w9WgXcQ", abs_item_id=None, abs_episode_id=None)
        self.abs.items["item-1"]["media"]["episodes"][0]["audioFile"]["metadata"]["filename"] = (
            "dQw4w9WgXcQ.m4a"
        )
        report = self.job(config).run_once()
        self.assertEqual(len(report.deleted), 1)
        row = self.db.get_video("dQw4w9WgXcQ")
        assert row is not None
        self.assertEqual(row["status"], "deleted")


class SevenDaysToKeepTests(RetentionTestCase):
    """Acceptance: a row past the 7-day deadline is deleted by ``7`` and kept by ``null``."""

    def test_same_row_past_seven_day_deadline_is_deleted_then_kept(self) -> None:
        video_id = "dQw4w9WgXcQ"
        seven = self.config(UC_seven={"retention_days": 7})
        # 10 days > 7: with the old policy the deadline has already passed.
        self.add_video(video_id=video_id, channel_id="UC_seven", added_at=NOW - timedelta(days=10))
        report = self.job(seven).run_once()
        self.assertEqual(len(report.deleted), 1)
        self.assertEqual(self.abs.deleted, [("item-1", "ep-1", True)])
        row = self.db.get_video(video_id)
        assert row is not None
        self.assertEqual(row["status"], "deleted")

        # Restore the very same row (same added_at) and the ABS episode, then
        # switch the channel policy to keep. The row is already past the 7-day
        # deadline, so only the null policy can protect it.
        self.db.update_video(video_id, status="imported", deleted_at=None)
        self.abs.items = {"item-1": item()}
        keep = self.config(UC_seven={"retention_days": None})
        report = self.job(keep).run_once()
        self.assertEqual(report.kept, 1)
        self.assertEqual(len(report.deleted), 0)
        self.assertEqual(self.abs.deleted, [("item-1", "ep-1", True)])
        row = self.db.get_video(video_id)
        assert row is not None
        self.assertEqual(row["status"], "imported")


class RecordingAlertNotifier:
    def __init__(self) -> None:
        self.alerts: list[dict[str, Any]] = []

    def retention_alert(self, payload: dict[str, Any]) -> None:
        self.alerts.append(payload)


class ReconcileSafetyTests(RetentionTestCase):
    """A wrong ABS_URL / stopped ABS / rebuilt database must not mass-delete."""

    def test_blocked_pass_alerts_once_and_resets_after_recovery(self) -> None:
        config = self.config(UC_short={"retention_days": 1})
        self.add_video(added_at=NOW - timedelta(days=10), file_on_disk=False)
        notifier = RecordingAlertNotifier()
        job = self.job(config, notifier=notifier)

        self.abs.list_error = AbsError("ABS down")
        job.run_once()
        job.run_once()
        self.assertEqual(len(notifier.alerts), 1)
        self.assertEqual(notifier.alerts[0]["reason"], "library_listing_failed")

        # Recovery clears the dedupe slot, so a second outage alerts again.
        self.abs.list_error = None
        job.run_once()
        self.abs.list_error = AbsError("ABS down again")
        job.run_once()
        self.assertEqual(len(notifier.alerts), 2)

    def test_dry_run_never_sends_alerts(self) -> None:
        config = self.config(UC_short={"retention_days": 1})
        self.add_video(added_at=NOW - timedelta(days=10), file_on_disk=False)
        self.abs.list_error = AbsError("ABS down")
        notifier = RecordingAlertNotifier()
        job = self.job(config, notifier=notifier)
        report = job.preview()
        self.assertTrue(report.reconcile_blocked)
        self.assertEqual(notifier.alerts, [])
        self.assertIsNone(job._last_alert)

    def test_failed_library_listing_writes_nothing(self) -> None:
        config = self.config(UC_short={"retention_days": 1})
        self.add_video(added_at=NOW - timedelta(days=10), file_on_disk=False)
        self.abs.list_error = AbsError("ABS 404 / wrong instance")
        report = self.job(config).run_once()
        self.assertEqual(report.reconciled, [])
        self.assertEqual(report.deleted, [])
        self.assertEqual(self.abs.deleted, [])
        self.assertEqual(report.reconcile_blocked["reason"], "library_listing_failed")
        self.assertTrue(report.errors)
        row = self.db.get_video("dQw4w9WgXcQ")
        assert row is not None
        self.assertEqual(row["status"], "imported")

    def test_missing_library_root_writes_nothing(self) -> None:
        config = self.config(UC_short={"retention_days": 1})
        self.add_video(added_at=NOW - timedelta(days=10), file_on_disk=False)
        shutil.rmtree(self.library)
        report = self.job(config).run_once()
        self.assertEqual((report.reconcile_blocked or {})["reason"], "library_unavailable")
        self.assertEqual(self.abs.deleted, [])
        row = self.db.get_video("dQw4w9WgXcQ")
        assert row is not None
        self.assertEqual(row["status"], "imported")

    def test_empty_library_root_writes_nothing(self) -> None:
        """Docker's short bind creates an empty dir when the share is unmounted."""
        config = self.config(UC_short={"retention_days": 1})
        self.add_video(added_at=NOW - timedelta(days=10), file_on_disk=False)
        for entry in self.library.iterdir():
            if entry.is_dir():
                entry.rmdir()
        report = self.job(config).run_once()
        self.assertEqual((report.reconcile_blocked or {})["reason"], "library_unavailable")
        self.assertEqual(self.abs.deleted, [])
        row = self.db.get_video("dQw4w9WgXcQ")
        assert row is not None
        self.assertEqual(row["status"], "imported")

    def test_empty_abs_listing_writes_nothing(self) -> None:
        """A 200 with an empty listing is a wrong library, not a wiped one."""
        config = self.config(UC_short={"retention_days": 1})
        self.add_video(added_at=NOW - timedelta(days=10), file_on_disk=False)
        self.abs.items.clear()
        report = self.job(config).run_once()
        self.assertEqual((report.reconcile_blocked or {})["reason"], "library_mismatch")
        self.assertEqual(self.abs.deleted, [])
        row = self.db.get_video("dQw4w9WgXcQ")
        assert row is not None
        self.assertEqual(row["status"], "imported")

    def test_mass_disappearance_blocks_the_whole_pass(self) -> None:
        config = self.config(UC_short={"retention_days": 1})
        for index in range(10):
            self.add_video(
                video_id=f"vid{index:08d}",
                added_at=NOW - timedelta(days=10),
                file_on_disk=False,
            )
        # A row whose podcast still exists and whose retention deadline passed:
        # the blocked pass must not delete it either (zero writes).
        self.abs.items["item-2"] = item("item-2", episodes=["ep-2"])
        self.add_video(
            video_id="vid-existing",
            added_at=NOW - timedelta(days=10),
            file_on_disk=True,
        )
        self.db.update_video(
            "vid-existing", abs_item_id="item-2", abs_episode_id="ep-2"
        )
        self.abs.items.pop("item-1")  # the other ten "vanished"
        report = self.job(config).run_once()
        blocked = report.reconcile_blocked or {}
        self.assertEqual(blocked.get("reason"), "too_many_missing")
        self.assertEqual(len(blocked.get("would_reconcile") or []), 10)
        self.assertEqual(report.reconciled, [])
        self.assertEqual(report.deleted, [])
        self.assertEqual(self.abs.deleted, [])
        existing = self.db.get_video("vid-existing")
        assert existing is not None
        self.assertEqual(existing["status"], "imported")
        for index in range(10):
            row = self.db.get_video(f"vid{index:08d}")
            assert row is not None
            self.assertEqual(row["status"], "imported")

    def test_manual_delete_with_file_still_on_disk_is_not_marked(self) -> None:
        config = self.config(UC_short={"retention_days": 1})
        self.add_video(added_at=NOW - timedelta(days=10), file_on_disk=True)
        self.abs.items["item-1"]["media"]["episodes"] = []
        report = self.job(config).run_once()
        self.assertEqual(report.reconciled, [])
        row = self.db.get_video("dQw4w9WgXcQ")
        assert row is not None
        self.assertEqual(row["status"], "imported")
        self.assertTrue(any("still on disk" in str(error.get("error")) for error in report.errors))

    def test_changed_episode_id_is_refreshed_not_deleted(self) -> None:
        config = self.config(UC_short={"retention_days": 30})
        self.add_video(added_at=NOW - timedelta(days=1))
        entry = item(episodes=["ep-new"])
        entry["media"]["episodes"][0]["audioFile"]["metadata"]["filename"] = "dQw4w9WgXcQ.m4a"
        self.abs.items = {"item-1": entry}
        report = self.job(config).run_once()
        self.assertEqual(report.reconciled, [])
        self.assertEqual(len(report.resynced), 1)
        row = self.db.get_video("dQw4w9WgXcQ")
        assert row is not None
        self.assertEqual(row["status"], "imported")
        self.assertEqual(row["abs_episode_id"], "ep-new")

    def test_changed_item_id_is_refreshed_not_deleted(self) -> None:
        config = self.config(UC_short={"retention_days": 30})
        self.add_video(added_at=NOW - timedelta(days=1))
        entry = item("item-2", episodes=["ep-2"])
        entry["media"]["episodes"][0]["audioFile"]["metadata"]["filename"] = "dQw4w9WgXcQ.m4a"
        self.abs.items = {"item-2": entry}
        report = self.job(config).run_once()
        self.assertEqual(report.reconciled, [])
        self.assertTrue(report.resynced)
        row = self.db.get_video("dQw4w9WgXcQ")
        assert row is not None
        self.assertEqual(row["status"], "imported")
        self.assertEqual(row["abs_item_id"], "item-2")
        self.assertEqual(row["abs_episode_id"], "ep-2")

    def test_mass_disappearance_below_limit_is_reconciled(self) -> None:
        config = self.config(UC_short={"retention_days": 1})
        self.add_video(added_at=NOW - timedelta(days=10), file_on_disk=False)
        self.abs.items = {"sentinel": other_item()}
        report = self.job(config).run_once()
        self.assertIsNone(report.reconcile_blocked)
        self.assertEqual(len(report.reconciled), 1)

    def test_duplicate_filenames_with_file_on_disk_are_not_guessed(self) -> None:
        config = self.config(UC_short={"retention_days": 1})
        self.add_video(added_at=NOW - timedelta(days=10), file_on_disk=True)
        entry = item(episodes=["ep-a", "ep-b"])
        for episode in entry["media"]["episodes"]:
            episode["audioFile"]["metadata"]["filename"] = "dQw4w9WgXcQ.m4a"
        self.abs.items = {"item-1": entry}
        report = self.job(config).run_once()
        self.assertEqual(report.reconciled, [])
        self.assertEqual(report.resynced, [])
        self.assertEqual(self.abs.deleted, [])
        row = self.db.get_video("dQw4w9WgXcQ")
        assert row is not None
        self.assertEqual(row["status"], "imported")
        self.assertTrue(any("not guessing" in str(err.get("error")) for err in report.errors))

    def test_duplicate_filenames_without_file_are_reconciled(self) -> None:
        config = self.config(UC_short={"retention_days": 1})
        self.add_video(added_at=NOW - timedelta(days=10), file_on_disk=False)
        entry = item(episodes=["ep-a", "ep-b"])
        for episode in entry["media"]["episodes"]:
            episode["audioFile"]["metadata"]["filename"] = "dQw4w9WgXcQ.m4a"
        self.abs.items = {"item-1": entry}
        report = self.job(config).run_once()
        self.assertEqual(len(report.reconciled), 1)
        row = self.db.get_video("dQw4w9WgXcQ")
        assert row is not None
        self.assertEqual(row["status"], "deleted")

    def test_missing_item_404_is_cached_per_pass(self) -> None:
        config = self.config(UC_short={"retention_days": 1})
        for index in range(4):
            self.add_video(
                video_id=f"vid{index:08d}",
                added_at=NOW - timedelta(days=1),
                file_on_disk=False,
            )
        self.abs.items = {"sentinel": other_item()}
        self.job(config).run_once()
        # Four rows share item-1; the 404 must be asked once, not four times.
        self.assertEqual(self.abs.get_item_calls, 1)


class DryRunTests(RetentionTestCase):
    def test_dry_run_reports_without_writing(self) -> None:
        config = self.config(UC_short={"retention_days": 1})
        self.add_video(added_at=NOW - timedelta(days=2))
        job = self.job(config)
        report = job.run_once(dry_run=True)
        self.assertTrue(report.dry_run)
        self.assertEqual(report.deleted, [])
        self.assertEqual(len(report.planned), 1)
        self.assertEqual(report.planned[0]["action"], "delete")
        self.assertEqual(self.abs.deleted, [])
        row = self.db.get_video("dQw4w9WgXcQ")
        assert row is not None
        self.assertEqual(row["status"], "imported")
        self.assertIsNone(job.last_report)

    def test_preview_is_always_a_dry_run(self) -> None:
        config = self.config(UC_short={"retention_days": 1})
        self.add_video(added_at=NOW - timedelta(days=2))
        report = self.job(config).preview()
        self.assertTrue(report.dry_run)
        self.assertEqual(self.abs.deleted, [])
        self.assertEqual(len(report.planned), 1)

    def test_config_dry_run_flag_is_used_by_scheduled_runs(self) -> None:
        config = parse_config(
            {
                "defaults": {"retention_days": 14},
                "abs": {
                    "url": "http://abs.local",
                    "api_key": "key",
                    "library_id": "lib-1",
                    "library_path": "/youtube-podcasts",
                },
                "paths": {
                    "data_dir": "data",
                    "tmp_dir": "tmp",
                    "library_path": str(self.library),
                },
                "retention": {"dry_run": True},
                "channels": {"UC_short": {"retention_days": 1}},
            }
        )
        self.add_video(added_at=NOW - timedelta(days=2))
        report = self.job(config).run_once()
        self.assertTrue(report.dry_run)
        self.assertEqual(self.abs.deleted, [])
        row = self.db.get_video("dQw4w9WgXcQ")
        assert row is not None
        self.assertEqual(row["status"], "imported")


class AlbumProgressFakeAbs(FakeAbs):
    """Fake that exposes the admin progress maps used by the real client."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.all_users_error: Exception | None = None
        self.progress_user_error: Exception | None = None

    def media_progress_map(self, user: str | None = None):
        if user:
            if self.progress_user_error:
                raise self.progress_user_error
            return dict(self.progress_by_user.get(user, {}))
        return {
            key: value
            for key, value in self.progress.items()
            if not isinstance(value, Exception)
        }

    def all_media_progress(self):
        if self.all_users_error:
            raise self.all_users_error
        merged = dict(self.media_progress_map())
        for entries in self.progress_by_user.values():
            for key, entry in entries.items():
                current = merged.get(key)
                if current is None or (entry.unfinished and not current.unfinished):
                    merged[key] = entry
        return merged


class MultiUserProgressTests(RetentionTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.abs = AlbumProgressFakeAbs()

    def _unfinished(self) -> EpisodeProgress:
        return EpisodeProgress(
            current_time=60,
            duration=600,
            progress=0.1,
            started_at="2026-06-14T00:00:00Z",
        )

    def test_progress_of_another_listener_defers_deletion(self) -> None:
        config = self.config(UC_one={"retention_days": 1})
        self.add_video(channel_id="UC_one", added_at=NOW - timedelta(days=1, hours=12))
        self.abs.progress_by_user["user-b"] = {("item-1", "ep-1"): self._unfinished()}
        report = self.job(config).run_once()
        self.assertEqual(len(report.deferred), 1)
        self.assertEqual(self.abs.deleted, [])

    def test_progress_user_narrows_the_scope_to_one_account(self) -> None:
        config = self.config(
            progress_user="user-a", UC_one={"retention_days": 1}
        )
        self.add_video(channel_id="UC_one", added_at=NOW - timedelta(days=1, hours=12))
        self.abs.progress_by_user["user-b"] = {("item-1", "ep-1"): self._unfinished()}
        report = self.job(config).run_once()
        self.assertEqual(report.deferred, [])
        self.assertEqual(len(report.deleted), 1)

    def test_admin_listing_failure_falls_back_to_key_owner_with_warning(self) -> None:
        config = self.config(UC_one={"retention_days": 1})
        self.add_video(channel_id="UC_one", added_at=NOW - timedelta(days=1, hours=12))
        self.abs.all_users_error = AbsError("ABS 403: admin required", status_code=403)
        self.abs.progress[("item-1", "ep-1")] = self._unfinished()
        report = self.job(config).run_once()
        self.assertEqual(len(report.deferred), 1)
        self.assertTrue(any("all ABS accounts" in warning for warning in report.warnings))

    def test_admin_listing_500_does_not_delete(self) -> None:
        config = self.config(UC_one={"retention_days": 1})
        self.add_video(channel_id="UC_one", added_at=NOW - timedelta(days=1, hours=12))
        self.abs.all_users_error = AbsError("ABS 500: boom", status_code=500)
        self.abs.progress[("item-1", "ep-1")] = EpisodeProgress(
            current_time=0, duration=600, progress=0.0
        )
        report = self.job(config).run_once()
        self.assertEqual(report.deferred, [])
        self.assertEqual(self.abs.deleted, [])
        row = self.db.get_video("dQw4w9WgXcQ")
        assert row is not None
        self.assertEqual(row["status"], "imported")
        self.assertTrue(report.warnings)

    def test_unknown_progress_user_does_not_delete(self) -> None:
        config = self.config(progress_user="ghost", UC_one={"retention_days": 1})
        self.add_video(channel_id="UC_one", added_at=NOW - timedelta(days=1, hours=12))
        self.abs.progress_user_error = AbsError("ABS user 'ghost' not found")
        report = self.job(config).run_once()
        self.assertEqual(self.abs.deleted, [])
        self.assertTrue(any("ABS progress" in warning for warning in report.warnings))
        row = self.db.get_video("dQw4w9WgXcQ")
        assert row is not None
        self.assertEqual(row["status"], "imported")

    def test_malformed_me_response_does_not_delete(self) -> None:
        config = self.config(UC_one={"retention_days": 1})
        self.add_video(channel_id="UC_one", added_at=NOW - timedelta(days=1, hours=12))
        self.abs.all_users_error = AbsError("ABS /api/users returned no users")
        report = self.job(config).run_once()
        self.assertEqual(self.abs.deleted, [])
        row = self.db.get_video("dQw4w9WgXcQ")
        assert row is not None
        self.assertEqual(row["status"], "imported")


class OverrideBoundsTests(RetentionTestCase):
    def test_huge_override_does_not_break_the_pass(self) -> None:
        config = self.config(UC_short={"retention_days": 1})
        self.add_video(video_id="dQw4w9WgXcQ", added_at=NOW, override=10**9)
        # Another due row must still be processed after the broken one.
        self.add_video(video_id="vid00000002", added_at=NOW - timedelta(days=2))
        report = self.job(config).run_once()
        self.assertTrue(report.errors)
        row = self.db.get_video("dQw4w9WgXcQ")
        assert row is not None
        self.assertEqual(row["status"], "imported")
        other = self.db.get_video("vid00000002")
        assert other is not None
        self.assertEqual(other["status"], "deleted")

    def test_zero_override_keeps_even_when_channel_deletes(self) -> None:
        config = self.config(UC_short={"retention_days": 0})
        self.add_video(added_at=NOW - timedelta(days=100), override=0)
        report = self.job(config).run_once()
        self.assertEqual(report.kept, 1)
        self.assertEqual(self.abs.deleted, [])


class DeletedNotificationTests(RetentionTestCase):
    """ntfy hook is called only for real retention deletions, once per pass."""

    def test_successful_delete_notifies_once(self) -> None:
        config = self.config(UC_short={"retention_days": 1})
        self.add_video(added_at=NOW - timedelta(days=2))
        self.db.update_video("dQw4w9WgXcQ", channel_title="Kanał testowy")
        notifier = RecordingDeletedNotifier()
        report = self.job(config, notifier=notifier).run_once()
        self.assertEqual(len(report.deleted), 1)
        self.assertEqual(len(notifier.batches), 1)
        items, reason = notifier.batches[0]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["video_id"], "dQw4w9WgXcQ")
        self.assertEqual(items[0]["channel_title"], "Kanał testowy")
        self.assertIn("retention", reason)
        self.assertIn("1 day", reason)
        self.assertEqual(notifier.calls, [])

    def test_bulk_delete_sends_one_summary_instead_of_one_push_per_row(self) -> None:
        config = self.config(UC_short={"retention_days": 1})
        episodes = self.abs.items["item-1"]["media"]["episodes"]
        episodes.append({**episodes[0], "id": "ep-2"})
        episodes.append({**episodes[0], "id": "ep-3"})
        for index, episode_id in enumerate(("ep-1", "ep-2", "ep-3"), start=1):
            self.add_video(
                f"bulk{index}",
                episode_id=episode_id,
                added_at=NOW - timedelta(days=2),
            )
        notifier = RecordingDeletedNotifier()
        report = self.job(config, notifier=notifier).run_once()
        self.assertEqual(len(report.deleted), 3)
        self.assertEqual(len(notifier.batches), 1)
        items, reason = notifier.batches[0]
        self.assertEqual(len(items), 3)
        self.assertEqual({item["video_id"] for item in items}, {"bulk1", "bulk2", "bulk3"})
        self.assertIn("1 day", reason)
        self.assertEqual(notifier.calls, [])

    def test_reconcile_does_not_notify(self) -> None:
        config = self.config(UC_short={"retention_days": 1})
        self.add_video(added_at=NOW - timedelta(days=2), file_on_disk=False)
        self.abs.items["item-1"]["media"]["episodes"] = []
        notifier = RecordingDeletedNotifier()
        report = self.job(config, notifier=notifier).run_once()
        self.assertEqual(len(report.reconciled), 1)
        self.assertEqual(notifier.batches, [])
        self.assertEqual(notifier.calls, [])

    def test_delete_error_does_not_notify(self) -> None:
        config = self.config(UC_short={"retention_days": 1})
        self.add_video(added_at=NOW - timedelta(days=2))
        self.abs.delete_error = AbsError("ABS 500")
        notifier = RecordingDeletedNotifier()
        report = self.job(config, notifier=notifier).run_once()
        self.assertEqual(len(report.errors), 1)
        self.assertEqual(notifier.batches, [])
        self.assertEqual(notifier.calls, [])

    def test_notifier_exception_does_not_break_retention(self) -> None:
        config = self.config(UC_short={"retention_days": 1})
        self.add_video(added_at=NOW - timedelta(days=2))
        notifier = RecordingDeletedNotifier(fail=True)
        report = self.job(config, notifier=notifier).run_once()
        self.assertEqual(len(report.deleted), 1)
        row = self.db.get_video("dQw4w9WgXcQ")
        assert row is not None
        self.assertEqual(row["status"], "deleted")


class RecordingDeletedNotifier:
    """Batch-first fake: the per-row hook must not be used by retention."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[Any, str]] = []
        self.batches: list[tuple[list[dict[str, Any]], str]] = []

    def job_deleted(self, job: Any, reason: str) -> None:
        self.calls.append((job, reason))
        if self.fail:
            raise RuntimeError("notification boom")

    def job_deleted_batch(self, items: list[dict[str, Any]], reason: str = "") -> None:
        self.batches.append((items, reason))
        if self.fail:
            raise RuntimeError("notification boom")


if __name__ == "__main__":
    unittest.main()
