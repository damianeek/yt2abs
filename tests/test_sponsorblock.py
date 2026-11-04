"""Unit tests for yt2abs.sponsorblock (no network, no ffmpeg)."""
from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from yt2abs.sponsorblock import (
    DEFAULT_CATEGORIES,
    MARK_CATEGORIES,
    MODE_MARK,
    MODE_MUTE,
    MODE_OFF,
    MODE_REMOVE,
    REMOVE_CATEGORIES,
    Segment,
    SponsorBlockConfigError,
    build_mute_command,
    build_mute_filter,
    fetch_segments,
    mute_segments,
    parse_setting,
    video_hash,
    ytdlp_args,
)


class ParseSettingTests(unittest.TestCase):
    def test_off_variants(self):
        for value in (None, False, "", "off", "OFF", "none", "no", "0"):
            with self.subTest(value=value):
                setting = parse_setting(value)
                self.assertEqual(setting.mode, MODE_OFF)
                self.assertFalse(setting.enabled)
                self.assertEqual(ytdlp_args(setting), [])

    def test_true_is_remove_sponsor(self):
        setting = parse_setting(True)
        self.assertEqual(setting.mode, MODE_REMOVE)
        self.assertEqual(setting.categories, DEFAULT_CATEGORIES)

    def test_modes_with_default_categories(self):
        for text, mode in (("remove", MODE_REMOVE), ("mark", MODE_MARK), ("mute", MODE_MUTE)):
            with self.subTest(text=text):
                setting = parse_setting(text)
                self.assertEqual(setting.mode, mode)
                self.assertEqual(setting.categories, ("sponsor",))

    def test_explicit_categories(self):
        setting = parse_setting("remove:sponsor,selfpromo")
        self.assertEqual(setting.mode, MODE_REMOVE)
        self.assertEqual(setting.categories, ("sponsor", "selfpromo"))

    def test_categories_are_ordered_canonically(self):
        setting = parse_setting("mute:intro,sponsor")
        self.assertEqual(setting.categories, ("sponsor", "intro"))

    def test_all_alias_expands_for_mute(self):
        setting = parse_setting("mute:all")
        self.assertEqual(setting.categories, REMOVE_CATEGORIES)

    def test_default_alias_excludes_filler_for_remove(self):
        setting = parse_setting("remove:default")
        self.assertNotIn("filler", setting.categories)
        self.assertIn("sponsor", setting.categories)

    def test_exclusion_token(self):
        setting = parse_setting("remove:all,-filler")
        self.assertNotIn("filler", setting.categories)
        self.assertEqual(set(setting.categories), set(REMOVE_CATEGORIES) - {"filler"})

    def test_mark_accepts_poi_categories(self):
        setting = parse_setting("mark:poi_highlight")
        self.assertEqual(setting.categories, ("poi_highlight",))
        self.assertIn("chapter", MARK_CATEGORIES)

    def test_remove_rejects_non_skippable_category(self):
        with self.assertRaises(SponsorBlockConfigError):
            parse_setting("remove:poi_highlight")

    def test_unknown_category_rejected(self):
        with self.assertRaises(SponsorBlockConfigError):
            parse_setting("remove:sponsor,bogus")

    def test_unknown_mode_like_string_rejected(self):
        with self.assertRaises(SponsorBlockConfigError):
            parse_setting("bogus:sponsor")

    def test_list_form_is_remove(self):
        setting = parse_setting(["selfpromo", "sponsor"])
        self.assertEqual(setting.mode, MODE_REMOVE)
        self.assertEqual(setting.categories, ("sponsor", "selfpromo"))

    def test_mapping_form(self):
        setting = parse_setting({"mode": "mute", "categories": ["sponsor"]})
        self.assertEqual(setting.mode, MODE_MUTE)
        self.assertEqual(setting.categories, ("sponsor",))

    def test_mapping_form_off(self):
        self.assertEqual(parse_setting({"mode": "off"}).mode, MODE_OFF)

    def test_mapping_with_unknown_key_rejected(self):
        with self.assertRaises(SponsorBlockConfigError):
            parse_setting({"mode": "remove", "categoriez": ["sponsor"]})

    def test_wrong_type_rejected(self):
        with self.assertRaises(SponsorBlockConfigError):
            parse_setting(42)


class YtdlpArgsTests(unittest.TestCase):
    def test_off_has_no_args(self):
        self.assertEqual(ytdlp_args(parse_setting("off")), [])

    def test_remove_args(self):
        self.assertEqual(
            ytdlp_args(parse_setting("remove:sponsor,selfpromo")),
            ["--sponsorblock-remove", "sponsor,selfpromo"],
        )

    def test_mark_args(self):
        self.assertEqual(ytdlp_args(parse_setting("mark")), ["--sponsorblock-mark", "sponsor"])

    def test_mute_is_handled_locally(self):
        self.assertEqual(ytdlp_args(parse_setting("mute")), [])

    def test_custom_api_is_passed_through(self):
        args = ytdlp_args(parse_setting("remove"), api="http://sponsor.local:8080/")
        self.assertEqual(args, ["--sponsorblock-remove", "sponsor", "--sponsorblock-api", "http://sponsor.local:8080"])

    def test_default_api_is_not_repeated(self):
        args = ytdlp_args(parse_setting("remove"), api="https://sponsor.ajay.app")
        self.assertEqual(args, ["--sponsorblock-remove", "sponsor"])


class ApiTests(unittest.TestCase):
    VIDEO_ID = "sI_MzL3HIa0"

    def test_video_hash_matches_sponsorblock_scheme(self):
        self.assertEqual(video_hash(self.VIDEO_ID), hashlib.sha256(self.VIDEO_ID.encode("ascii")).hexdigest()[:4])

    def test_fetch_segments_filters_and_sorts(self):
        payload = [  # hashed endpoint shape: one entry per video in the bucket
            {
                "videoID": "VA6tOTomT4Q",  # different video in the same hash bucket
                "segments": [
                    {
                        "category": "sponsor",
                        "actionType": "skip",
                        "segment": [0.0, 16.8],
                        "videoDuration": 224.0,
                    }
                ],
            },
            {
                "videoID": self.VIDEO_ID,
                "segments": [
                    {
                        "category": "selfpromo",
                        "actionType": "skip",
                        "segment": [200.0, 210.0],
                        "videoDuration": 1283.0,
                        "UUID": "b",
                    },
                    {
                        "category": "sponsor",
                        "actionType": "skip",
                        "segment": [81.5, 100.7],
                        "videoDuration": 1283.0,
                        "UUID": "a",
                    },
                    {
                        "category": "poi_highlight",
                        "actionType": "poi",
                        "segment": [50.0, 50.0],
                        "videoDuration": 1283.0,
                    },
                ],
            },
        ]
        captured = {}

        def fake_fetch(url, timeout):
            captured["url"] = url
            return payload

        segments = fetch_segments(self.VIDEO_ID, ("sponsor",), json_fetch=fake_fetch)
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0].start, 81.5)
        self.assertEqual(segments[0].uuid, "a")
        self.assertIn("/api/skipSegments/", captured["url"])
        self.assertIn(video_hash(self.VIDEO_ID), captured["url"])
        self.assertIn("categories", captured["url"])

    def test_fetch_segments_accepts_flat_payload(self):
        payload = [  # ?videoID= endpoint shape
            {
                "videoID": self.VIDEO_ID,
                "category": "sponsor",
                "actionType": "skip",
                "segment": [10.0, 20.0],
                "videoDuration": 1283.0,
            }
        ]
        segments = fetch_segments(self.VIDEO_ID, ("sponsor",), json_fetch=lambda url, timeout: payload)
        self.assertEqual([(s.start, s.end) for s in segments], [(10.0, 20.0)])

    def test_fetch_segments_drops_duration_mismatch(self):
        payload = [
            {
                "videoID": self.VIDEO_ID,
                "segments": [
                    {
                        "category": "sponsor",
                        "actionType": "skip",
                        "segment": [10.0, 20.0],
                        "videoDuration": 999.0,
                    }
                ],
            }
        ]
        segments = fetch_segments(
            self.VIDEO_ID, ("sponsor",), video_duration=1283.0, json_fetch=lambda url, timeout: payload
        )
        self.assertEqual(segments, [])

    def test_fetch_segments_missing_video_returns_empty(self):
        self.assertEqual(fetch_segments(self.VIDEO_ID, json_fetch=lambda url, timeout: None), [])

    def test_build_mute_filter_merges_overlapping_spans(self):
        segments = [
            Segment(30.0, 40.0, "sponsor"),
            Segment(38.0, 45.0, "selfpromo"),
            Segment(100.0, 101.5, "intro"),
        ]
        self.assertEqual(
            build_mute_filter(segments),
            "volume=enable='between(t,30.000,45.000)+between(t,100.000,101.500)':volume=0",
        )

    def test_build_mute_filter_empty(self):
        self.assertIsNone(build_mute_filter([]))

    def test_build_mute_command_maps_audio_and_copies_cover(self):
        """An embedded ``attached_pic`` cover must not be re-encoded.

        Without ``-map 0:a:0`` ffmpeg maps the cover too and the ipod/mp4 muxer
        rejects the resulting H.264 stream ("Could not find tag for codec
        h264"), which broke every ``sponsorblock: mute`` channel with
        ``--embed-thumbnail``.
        """
        command = build_mute_command("in.m4a", "out.m4a", "volume=0")
        self.assertEqual(command[command.index("-map")], "-map")
        self.assertEqual(command[command.index("-map") + 1], "0:a:0")
        self.assertIn("0:v?", command)
        self.assertEqual(command[command.index("-c:v") + 1], "copy")
        # The optional-mapping suffix must stay attached to the video map, not
        # turn into a positional argument.
        video_map = command.index("0:v?")
        self.assertEqual(command[video_map - 1], "-map")
        self.assertEqual(command[command.index("-af") + 1], "volume=0")

    def test_mute_segments_runs_ffmpeg_and_replaces_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "clip.m4a"
            path.write_bytes(b"original")
            commands = []

            def fake_runner(command, **kwargs):
                commands.append(command)
                Path(command[-1]).write_bytes(b"muted")
                return subprocess.CompletedProcess(command, 0, "", "")

            changed = mute_segments(path, [Segment(81.5, 100.7, "sponsor")], runner=fake_runner)
            self.assertTrue(changed)
            self.assertEqual(path.read_bytes(), b"muted")
            self.assertIn("-af", commands[0])
            self.assertIn("volume=enable='between(t,81.500,100.700)':volume=0", commands[0])
            self.assertFalse(list(Path(tmp).glob("*.muted*")))

    def test_mute_segments_without_segments_does_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "clip.m4a"
            path.write_bytes(b"original")
            self.assertFalse(mute_segments(path, [], runner=lambda *a, **k: None))
            self.assertEqual(path.read_bytes(), b"original")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
