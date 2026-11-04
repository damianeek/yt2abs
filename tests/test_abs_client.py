"""Audiobookshelf client tests (no network: injectable sender)."""
from __future__ import annotations

import json
import unittest
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

from yt2abs.abs_client import AbsClient, AbsError, AbsNotFound


class FakeSender:
    def __init__(self, *responses: tuple[int, Any]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, method: str, url: str, headers: Mapping[str, str], body: bytes | None):
        payload = json.loads(body) if body else None
        self.calls.append({"method": method, "url": url, "headers": dict(headers), "body": payload})
        status, response = self.responses.pop(0)
        if isinstance(response, bytes):
            return status, response
        raw = b"" if response is None else json.dumps(response).encode()
        return status, raw


def make_client(sender: FakeSender) -> AbsClient:
    return AbsClient("http://abs.local:13378", "secret-token", sender=sender)


class AbsClientTests(unittest.TestCase):
    def test_scan_accepts_plain_text_response(self) -> None:
        sender = FakeSender((200, b"OK"))
        client = make_client(sender)
        self.assertEqual(client.scan_library("lib_1"), "OK")

    def test_scan_and_list_items(self) -> None:
        sender = FakeSender(
            (200, {"ok": True}),
            (200, {"results": [{"id": "li_1", "path": "/youtube-podcasts/Chan"}]}),
        )
        client = make_client(sender)
        client.scan_library("lib_1")
        items = client.list_library_items("lib_1")

        self.assertEqual(sender.calls[0]["method"], "POST")
        self.assertTrue(sender.calls[0]["url"].endswith("/api/libraries/lib_1/scan"))
        self.assertEqual(items[0]["id"], "li_1")
        self.assertEqual(sender.calls[1]["headers"]["Authorization"], "Bearer secret-token")
        self.assertNotIn("secret-token", sender.calls[1]["url"])

    def test_find_item_by_path_normalises_paths(self) -> None:
        sender = FakeSender()
        client = make_client(sender)
        items = [
            {"id": "li_1", "path": "/youtube-podcasts/Channel"},
            {"id": "li_2", "path": "/youtube-podcasts/Other/"},
        ]
        first = client.find_item_by_path("lib_1", "/youtube-podcasts/Channel/", items=items)
        second = client.find_item_by_path("lib_1", "/youtube-podcasts/Other", items=items)
        assert first is not None and second is not None
        self.assertEqual(first["id"], "li_1")
        self.assertEqual(second["id"], "li_2")
        self.assertIsNone(
            client.find_item_by_path("lib_1", "/youtube-podcasts/Missing", items=items)
        )

    def test_find_episode_by_filename(self) -> None:
        item = {
            "id": "li_1",
            "media": {
                "episodes": [
                    {"id": "ep_1", "audioFile": {"metadata": {"filename": "a.m4a"}}},
                    {
                        "id": "ep_2",
                        "audioFile": {
                            "metadata": {
                                "filename": "2026-01-01 - T [dQw4w9WgXcQ].m4a",
                                "path": "/youtube-podcasts/Chan/2026-01-01 - T [dQw4w9WgXcQ].m4a",
                            }
                        },
                    },
                ]
            },
        }
        sender = FakeSender()
        client = make_client(sender)
        episode = client.find_episode_by_filename(
            "li_1", "/tmp/2026-01-01 - T [dQw4w9WgXcQ].m4a", item=item
        )
        assert episode is not None
        self.assertEqual(episode["id"], "ep_2")

    def test_get_item_uses_expanded_query(self) -> None:
        sender = FakeSender((200, {"id": "li_1", "media": {"episodes": []}}))
        client = make_client(sender)
        client.get_item("li_1")
        query = parse_qs(urlparse(sender.calls[0]["url"]).query)
        self.assertEqual(query["expanded"], ["1"])

    def test_update_episode_and_podcast(self) -> None:
        sender = FakeSender((200, {"ok": True}), (200, {"updated": True}))
        client = make_client(sender)
        client.update_episode(
            "li_1",
            "ep_1",
            title="Tytuł",
            description="Opis",
            pub_date="2026-01-01T00:00:00.000Z",
        )
        call = sender.calls[0]
        self.assertEqual(call["method"], "PATCH")
        self.assertTrue(call["url"].endswith("/api/podcasts/li_1/episode/ep_1"))
        self.assertEqual(
            call["body"],
            {
                "title": "Tytuł",
                "description": "Opis",
                "pubDate": "2026-01-01T00:00:00.000Z",
            },
        )

        client.edit_podcast("li_1", {"title": "Kanał", "author": "Kanał"})
        self.assertTrue(sender.calls[1]["url"].endswith("/api/items/li_1/media"))
        self.assertEqual(
            sender.calls[1]["body"], {"metadata": {"title": "Kanał", "author": "Kanał"}}
        )

    def test_delete_episode_is_hard_by_default(self) -> None:
        sender = FakeSender((200, {"ok": True}))
        client = make_client(sender)
        client.delete_episode("li_1", "ep_1")
        query = parse_qs(urlparse(sender.calls[0]["url"]).query)
        self.assertEqual(sender.calls[0]["method"], "DELETE")
        self.assertEqual(query["hard"], ["1"])

    def test_http_errors(self) -> None:
        client = make_client(FakeSender((404, {"error": "not found"})))
        with self.assertRaises(AbsNotFound):
            client.get_item("missing")

        client = make_client(FakeSender((500, {"error": "boom"})))
        with self.assertRaises(AbsError) as context:
            client.scan_library("lib_1")
        self.assertEqual(context.exception.status_code, 500)


class ProgressMapTests(unittest.TestCase):
    """Progress of every ABS account (review #7): owner, explicit user, all users."""

    ENTRY = {
        "libraryItemId": "item-1",
        "episodeId": "ep-1",
        "currentTime": 30,
        "duration": 300,
        "progress": 0.1,
        "startedAt": "2026-06-14T00:00:00Z",
    }

    def test_owner_progress_uses_api_me(self) -> None:
        sender = FakeSender((200, {"mediaProgress": [self.ENTRY]}))
        progress = make_client(sender).media_progress_map()
        self.assertTrue(progress[("item-1", "ep-1")].unfinished)
        self.assertTrue(sender.calls[0]["url"].endswith("/api/me"))

    def test_named_user_reads_admin_detail(self) -> None:
        sender = FakeSender(
            (200, {"users": [{"id": "u1", "username": "root"}, {"id": "u2", "username": "kid"}]}),
            (200, {"mediaProgress": [self.ENTRY]}),
        )
        progress = make_client(sender).media_progress_map(user="kid")
        self.assertTrue(progress[("item-1", "ep-1")].unfinished)
        self.assertTrue(sender.calls[1]["url"].endswith("/api/users/u2"))

    def test_user_can_be_selected_by_id_or_username(self) -> None:
        for wanted in ("u1", "ROOT"):
            with self.subTest(wanted=wanted):
                sender = FakeSender(
                    (200, {"users": [{"id": "u1", "username": "root"}]}),
                    (200, {"mediaProgress": []}),
                )
                self.assertEqual(make_client(sender).media_progress_map(user=wanted), {})

    def test_unknown_user_is_an_error(self) -> None:
        sender = FakeSender((200, {"users": [{"id": "u1", "username": "root"}]}))
        with self.assertRaises(AbsError):
            make_client(sender).media_progress_map(user="nobody")

    def test_all_users_merges_and_unfinished_wins(self) -> None:
        sender = FakeSender(
            (200, {"users": [{"id": "u1", "username": "root"}, {"id": "u2", "username": "kid"}]}),
            (
                200,
                {
                    "mediaProgress": [
                        {
                            "libraryItemId": "item-1",
                            "episodeId": "ep-1",
                            "isFinished": True,
                            "progress": 1.0,
                        }
                    ]
                },
            ),
            (200, {"mediaProgress": [self.ENTRY]}),
        )
        merged = make_client(sender).all_media_progress()
        self.assertTrue(merged[("item-1", "ep-1")].unfinished)

    def test_get_media_progress_404_means_not_started(self) -> None:
        client = make_client(FakeSender((404, {"error": "not found"})))
        self.assertIsNone(client.get_media_progress("item-1", "ep-1"))


if __name__ == "__main__":
    unittest.main()
