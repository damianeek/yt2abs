"""Channel policy overrides (WebUI) and channel reference parsing."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from yt2abs.channels import (
    INHERIT,
    OVERRIDES_SETTING,
    ChannelInputError,
    ChannelPolicyStore,
    channel_payload,
    parse_channel_reference,
)
from yt2abs.config import parse_config
from yt2abs.db import Database

CONFIG_CHANNEL = "UCconfig0000000000000000"
NEW_CHANNEL = "UCnew000000000000000000"


def make_config():
    return parse_config(
        {
            "defaults": {"retention_days": 14, "protect_unfinished": True},
            "channels": {
                CONFIG_CHANNEL: {
                    "name": "Kanał z pliku",
                    "retention_days": 7,
                    "sponsorblock": "remove:sponsor",
                }
            },
        }
    )


class ChannelStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "channels.sqlite3")
        self.db.initialize()
        self.store = ChannelPolicyStore(self.db)
        self.config = make_config()

    def test_apply_overrides_and_adds_channels(self) -> None:
        self.store.upsert(
            CONFIG_CHANNEL,
            name="Nowa nazwa",
            retention_days=None,
            sponsorblock="mark:sponsor,intro",
            protect_unfinished=False,
        )
        self.store.upsert(NEW_CHANNEL, name="Dodany", retention_days=3)
        effective = self.store.apply(self.config)

        existing = effective.channels[CONFIG_CHANNEL]
        self.assertEqual(existing.name, "Nowa nazwa")
        self.assertIsNone(existing.retention_days)  # keep forever
        self.assertEqual(existing.sponsorblock.mode, "mark")
        self.assertFalse(existing.protect_unfinished)

        added = effective.channels[NEW_CHANNEL]
        self.assertEqual(added.name, "Dodany")
        self.assertEqual(added.retention_days, 3)
        # Untouched fields inherit defaults.
        self.assertEqual(added.sponsorblock.mode, "off")
        self.assertTrue(added.protect_unfinished)

    def test_inherit_removes_a_field_and_clear_restores_config(self) -> None:
        self.store.upsert(CONFIG_CHANNEL, retention_days=1, name="X")
        self.store.upsert(CONFIG_CHANNEL, retention_days=INHERIT, name=INHERIT)
        effective = self.store.apply(self.config)
        channel = effective.channels[CONFIG_CHANNEL]
        self.assertEqual(channel.retention_days, 7)
        self.assertEqual(channel.name, "Kanał z pliku")

        self.assertTrue(self.store.clear(CONFIG_CHANNEL))
        self.assertFalse(self.store.clear(CONFIG_CHANNEL))
        self.assertNotIn(CONFIG_CHANNEL, self.store.load())
        self.assertEqual(
            self.store.apply(self.config).channels[CONFIG_CHANNEL].retention_days, 7
        )

    def test_store_persists_across_instances(self) -> None:
        self.store.upsert(NEW_CHANNEL, name="Trwały", retention_days=0)
        reopened = ChannelPolicyStore(Database(Path(self.tmp.name) / "channels.sqlite3"))
        override = reopened.load()[NEW_CHANNEL]
        self.assertEqual(override.name, "Trwały")
        self.assertEqual(override.retention_days, 0)
        # Channel policy 0 = delete on the next pass; keep forever is None.
        self.assertEqual(reopened.apply(self.config).channels[NEW_CHANNEL].retention_days, 0)

    def test_invalid_values_are_rejected(self) -> None:
        with self.assertRaises(ChannelInputError):
            self.store.upsert(NEW_CHANNEL, retention_days=-1)
        with self.assertRaises(ChannelInputError):
            self.store.upsert(NEW_CHANNEL, retention_days=True)  # type: ignore[arg-type]
        with self.assertRaises(ChannelInputError):
            self.store.upsert(NEW_CHANNEL, sponsorblock="nope:sponsor")
        with self.assertRaises(ChannelInputError):
            self.store.upsert(NEW_CHANNEL, protect_unfinished="yes")  # type: ignore[arg-type]

    def test_malformed_stored_document_is_ignored(self) -> None:
        self.db.set_setting(OVERRIDES_SETTING, "{not json")
        self.assertEqual(self.store.load(), {})
        self.db.set_setting(OVERRIDES_SETTING, json.dumps({"UCx": 5}))
        self.assertEqual(self.store.load(), {})

    def test_channel_payload_sources(self) -> None:
        item = channel_payload(self.config, CONFIG_CHANNEL, in_config=True)
        self.assertEqual(item["retention_source"], "config")
        self.assertEqual(item["retention_days"], 7)
        self.assertTrue(item["in_config"])
        self.assertFalse(item["has_override"])

        self.store.upsert(CONFIG_CHANNEL, retention_days=None)
        override = self.store.load()[CONFIG_CHANNEL]
        item = channel_payload(
            self.store.apply(self.config),
            CONFIG_CHANNEL,
            override=override,
            in_config=True,
        )
        self.assertEqual(item["retention_source"], "ui")
        self.assertIsNone(item["retention_days"])


class ChannelReferenceTests(unittest.TestCase):
    def test_accepts_id_and_channel_link(self) -> None:
        self.assertEqual(parse_channel_reference(CONFIG_CHANNEL), CONFIG_CHANNEL)
        for link in (
            f"https://www.youtube.com/channel/{CONFIG_CHANNEL}",
            f"https://m.youtube.com/channel/{CONFIG_CHANNEL}/videos",
            f"youtube.com/channel/{CONFIG_CHANNEL}",
        ):
            with self.subTest(link=link):
                self.assertEqual(parse_channel_reference(link), CONFIG_CHANNEL)

    def test_rejects_handles_until_resolver_runs(self) -> None:
        for value in ("https://www.youtube.com/@nazwa", "https://vimeo.com/x", ""):
            with self.subTest(value=value):
                with self.assertRaises(ChannelInputError):
                    parse_channel_reference(value)


if __name__ == "__main__":
    unittest.main()
