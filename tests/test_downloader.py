"""Unit tests for yt2abs.downloader (yt-dlp wrapper) with a fake runner."""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from yt2abs.config import parse_config
from yt2abs.downloader import (
    AUDIO_FORMAT,
    DownloadError,
    build_audio_args,
    download,
    download_for_channel,
    probe_metadata,
)
from yt2abs.sponsorblock import Segment, parse_setting


def completed(command, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(command, returncode, stdout, stderr)


class FakeRunner:
    """Records commands and returns a canned result."""

    def __init__(self, *, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.commands: list[list[str]] = []

    def __call__(self, command, **kwargs):
        self.commands.append(list(command))
        return completed(command, self.returncode, self.stdout, self.stderr)


class BuildArgsTests(unittest.TestCase):
    def test_default_has_no_sponsorblock_flags(self):
        args = build_audio_args()
        self.assertIn("--format", args)
        self.assertIn(AUDIO_FORMAT, args)
        self.assertFalse(any(a.startswith("--sponsorblock") for a in args))

    def test_remove_adds_flags(self):
        args = build_audio_args(setting=parse_setting("remove:sponsor"))
        self.assertEqual(args[-2:], ["--sponsorblock-remove", "sponsor"])

    def test_mute_adds_no_ytdlp_flags(self):
        args = build_audio_args(setting=parse_setting("mute:sponsor"))
        self.assertFalse(any(a.startswith("--sponsorblock") for a in args))

    def test_extra_args_are_appended(self):
        args = build_audio_args(extra=["--no-playlist"])
        self.assertEqual(args[-1], "--no-playlist")


class ProbeMetadataTests(unittest.TestCase):
    def test_parses_channel_and_video_id(self):
        runner = FakeRunner(stdout="UCzz4CoEgSgWNs9ZAvRMhW2A|sI_MzL3HIa0\n")
        meta = probe_metadata("https://youtu.be/sI_MzL3HIa0", runner=runner)
        self.assertEqual(meta, {"channel_id": "UCzz4CoEgSgWNs9ZAvRMhW2A", "video_id": "sI_MzL3HIa0"})

    def test_returns_none_on_failure(self):
        runner = FakeRunner(returncode=1, stderr="boom")
        self.assertEqual(probe_metadata("https://youtu.be/x", runner=runner), {"channel_id": None, "video_id": None})


class DownloadTests(unittest.TestCase):
    def test_off_mode_does_not_touch_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "2026-01-01 - Film [sI_MzL3HIa0].m4a"
            target.write_bytes(b"audio")
            runner = FakeRunner(stdout=f"{target}\n")
            result = download("https://youtu.be/sI_MzL3HIa0", tmp, runner=runner, log=lambda *_: None)
            self.assertEqual(result.path, str(target))
            self.assertEqual(result.video_id, "sI_MzL3HIa0")
            self.assertFalse(result.muted)
            self.assertFalse(any(a.startswith("--sponsorblock") for a in runner.commands[0]))
            self.assertEqual(len(runner.commands), 1)

    def test_remove_mode_passes_flag_to_ytdlp(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = FakeRunner(stdout=f"{tmp}/file [sI_MzL3HIa0].m4a\n")
            download(
                "https://youtu.be/sI_MzL3HIa0",
                tmp,
                setting=parse_setting("remove:sponsor,selfpromo"),
                runner=runner,
                log=lambda *_: None,
            )
            command = runner.commands[0]
            self.assertIn("--sponsorblock-remove", command)
            self.assertIn("sponsor,selfpromo", command)

    def test_mute_mode_fetches_segments_and_mutes(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "clip [sI_MzL3HIa0].m4a"
            target.write_bytes(b"audio")
            runner = FakeRunner(stdout=f"{target}\n")
            segments = [Segment(81.5, 100.7, "sponsor")]
            with mock.patch("yt2abs.downloader.fetch_segments", return_value=segments) as fetch, mock.patch(
                "yt2abs.downloader.mute_segments", return_value=True
            ) as mute:
                result = download(
                    "https://youtu.be/sI_MzL3HIa0",
                    tmp,
                    setting=parse_setting("mute"),
                    runner=runner,
                    log=lambda *_: None,
                )
            fetch.assert_called_once()
            self.assertEqual(fetch.call_args.args[0], "sI_MzL3HIa0")
            mute.assert_called_once()
            self.assertTrue(result.muted)
            self.assertEqual(result.segments, 1)
            self.assertFalse(any(a.startswith("--sponsorblock") for a in runner.commands[0]))

    def test_mute_mode_without_segments_keeps_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = FakeRunner(stdout=f"{tmp}/clip [sI_MzL3HIa0].m4a\n")
            with mock.patch("yt2abs.downloader.fetch_segments", return_value=[]), mock.patch(
                "yt2abs.downloader.mute_segments"
            ) as mute:
                result = download(
                    "https://youtu.be/sI_MzL3HIa0",
                    tmp,
                    setting=parse_setting("mute"),
                    runner=runner,
                    log=lambda *_: None,
                )
            mute.assert_not_called()
            self.assertFalse(result.muted)
            self.assertEqual(result.segments, 0)

    def test_ytdlp_failure_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = FakeRunner(returncode=1, stderr="ERROR: blocked\nmore")
            with self.assertRaises(DownloadError) as ctx:
                download("https://youtu.be/x", tmp, runner=runner, log=lambda *_: None)
            self.assertIn("blocked", str(ctx.exception))


class DownloadForChannelTests(unittest.TestCase):
    def test_uses_channel_setting(self):
        config = parse_config(
            {
                "defaults": {"sponsorblock": "off"},
                "channels": {"UC_cut": {"sponsorblock": "remove:sponsor"}},
            }
        )
        with mock.patch("yt2abs.downloader.download") as fake_download:
            fake_download.return_value = "ok"
            download_for_channel("https://youtu.be/x", "/tmp/out", config, channel_id="UC_cut")
        setting = fake_download.call_args.kwargs["setting"]
        self.assertEqual(setting.mode, "remove")
        self.assertEqual(fake_download.call_args.kwargs["channel_id"], "UC_cut")

    def test_unknown_channel_falls_back_to_defaults(self):
        config = parse_config({"defaults": {"sponsorblock": "off"}, "channels": {}})
        with mock.patch("yt2abs.downloader.download") as fake_download:
            fake_download.return_value = "ok"
            download_for_channel("https://youtu.be/x", "/tmp/out", config, channel_id="UC_unknown")
        setting = fake_download.call_args.kwargs["setting"]
        self.assertEqual(setting.mode, "off")

    def test_probes_channel_when_not_given(self):
        config = parse_config({"defaults": {"sponsorblock": "mute"}, "channels": {}})
        with mock.patch(
            "yt2abs.downloader.probe_metadata", return_value={"channel_id": "UC_probe", "video_id": "vid12345678"}
        ), mock.patch("yt2abs.downloader.download") as fake_download:
            fake_download.return_value = "ok"
            download_for_channel("https://youtu.be/vid12345678", "/tmp/out", config)
        self.assertEqual(fake_download.call_args.kwargs["channel_id"], "UC_probe")
        self.assertEqual(fake_download.call_args.kwargs["setting"].mode, "mute")
        self.assertEqual(fake_download.call_args.kwargs["video_id"], "vid12345678")

    def test_explicit_video_id_does_not_collide_with_channel_probe(self):
        config = parse_config({"defaults": {}, "channels": {}})
        with mock.patch("yt2abs.downloader.probe_metadata") as probe, mock.patch(
            "yt2abs.downloader.download"
        ) as fake_download:
            fake_download.return_value = "ok"
            download_for_channel(
                "https://youtu.be/vid12345678", "/tmp/out", config, channel_id="UC_x", video_id="vid12345678"
            )
        probe.assert_not_called()
        self.assertEqual(fake_download.call_args.kwargs["video_id"], "vid12345678")
        self.assertEqual(fake_download.call_args.kwargs["channel_id"], "UC_x")

    def test_defaults_ytdlp_args_are_appended(self):
        config = parse_config({"defaults": {"ytdlp_args": ["--no-playlist"]}, "channels": {}})
        with mock.patch("yt2abs.downloader.download") as fake_download:
            fake_download.return_value = "ok"
            download_for_channel("https://youtu.be/x", "/tmp/out", config, channel_id="UC_x", extra=["--quiet"])
        self.assertEqual(fake_download.call_args.kwargs["extra"], ["--quiet", "--no-playlist"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
