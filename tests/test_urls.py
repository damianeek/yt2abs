"""URL normalisation tests."""
from __future__ import annotations

import unittest

from yt2abs.urls import InvalidYouTubeURL, canonical_url, extract_video_id


class ExtractVideoIdTests(unittest.TestCase):
    def test_supported_forms(self) -> None:
        cases = {
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ": "dQw4w9WgXcQ",
            "http://youtube.com/watch?v=dQw4w9WgXcQ&t=42&list=PL123": "dQw4w9WgXcQ",
            "https://m.youtube.com/watch?v=dQw4w9WgXcQ&feature=share": "dQw4w9WgXcQ",
            "https://music.youtube.com/watch?v=dQw4w9WgXcQ": "dQw4w9WgXcQ",
            "https://youtu.be/dQw4w9WgXcQ?t=10": "dQw4w9WgXcQ",
            "https://www.youtube.com/shorts/dQw4w9WgXcQ": "dQw4w9WgXcQ",
            "https://www.youtube.com/live/dQw4w9WgXcQ?si=abc": "dQw4w9WgXcQ",
            "https://www.youtube.com/embed/dQw4w9WgXcQ": "dQw4w9WgXcQ",
            "https://www.youtube.com/v/dQw4w9WgXcQ": "dQw4w9WgXcQ",
            "https://youtube-nocookie.com/embed/dQw4w9WgXcQ": "dQw4w9WgXcQ",
            "youtube.com/watch?v=dQw4w9WgXcQ": "dQw4w9WgXcQ",
            "  https://www.youtube.com/watch?v=dQw4w9WgXcQ  ": "dQw4w9WgXcQ",
            "https://www.youtube.com/watch?v=aqz-KE-bpKQ&pp=xyz": "aqz-KE-bpKQ",
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(extract_video_id(url), expected)

    def test_rejects_non_youtube_and_bad_ids(self) -> None:
        cases = [
            "",
            "   ",
            "https://vimeo.com/12345",
            "https://evil.com/watch?v=dQw4w9WgXcQ",
            "https://youtube.com.evil.com/watch?v=dQw4w9WgXcQ",
            "https://www.youtube.com/playlist?list=PL123",
            "https://www.youtube.com/watch",
            "https://www.youtube.com/watch?v=too-short",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQextra",
            "https://youtu.be/",
            "ftp://youtube.com/watch?v=dQw4w9WgXcQ",
            "javascript:alert(1)",
        ]
        for url in cases:
            with self.subTest(url=url):
                with self.assertRaises(InvalidYouTubeURL):
                    extract_video_id(url)

    def test_canonical_url_roundtrip(self) -> None:
        self.assertEqual(
            canonical_url("dQw4w9WgXcQ"), "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        )
        with self.assertRaises(InvalidYouTubeURL):
            canonical_url("nope")


if __name__ == "__main__":
    unittest.main()
