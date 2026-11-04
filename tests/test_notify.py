"""Discord notifier tests (stdlib only, no network)."""
from __future__ import annotations

import io
import tempfile
import unittest
import urllib.error
from email.message import Message
from pathlib import Path
from typing import Any, Mapping

from fastapi.testclient import TestClient

from yt2abs.app import create_app
from yt2abs.config import parse_config
from yt2abs.db import Database
from yt2abs.notify import (
    CONTENT_LIMIT,
    EMBED_DESCRIPTION_LIMIT,
    EMBED_TITLE_LIMIT,
    DiscordNotifier,
    _safe_target,
)

WEBHOOK = "https://discord.com/api/webhooks/123456/token-secret"


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


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


class SequenceTransport:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    def __call__(self, url: str, payload: Mapping[str, Any], timeout: float) -> int:
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def http_error(code: int, body: str = "") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        WEBHOOK, code, f"HTTP {code}", Message(), io.BytesIO(body.encode("utf-8"))
    )


def notifier(transport: Any, **kwargs: Any) -> DiscordNotifier:
    kwargs.setdefault("min_interval", 0)
    return DiscordNotifier(WEBHOOK, transport=transport, **kwargs)


class DisabledTests(unittest.TestCase):
    def test_missing_url_skips_without_transport(self) -> None:
        transport = RecordingTransport()
        result = DiscordNotifier("", transport=transport).send("hello")
        self.assertTrue(result.skipped)
        self.assertFalse(result.delivered)
        self.assertEqual(result.reason, "not_configured")
        self.assertEqual(transport.calls, [])

    def test_disabled_flag_wins_over_url(self) -> None:
        transport = RecordingTransport()
        result = DiscordNotifier(
            WEBHOOK, disabled=True, transport=transport
        ).send("hello")
        self.assertTrue(result.skipped)
        self.assertEqual(transport.calls, [])

    def test_invalid_url_is_disabled(self) -> None:
        transport = RecordingTransport()
        result = DiscordNotifier("not-a-webhook-url", transport=transport).send("hi")
        self.assertTrue(result.skipped)
        self.assertEqual(transport.calls, [])

    def test_empty_message_skips(self) -> None:
        transport = RecordingTransport()
        result = notifier(transport).send("   ")
        self.assertTrue(result.skipped)
        self.assertEqual(result.reason, "empty_message")
        self.assertEqual(transport.calls, [])


class PayloadTests(unittest.TestCase):
    def test_content_payload(self) -> None:
        transport = RecordingTransport()
        result = notifier(transport).send("Wiadomość testowa")
        self.assertTrue(result.delivered)
        self.assertEqual(result.status, 204)
        url, payload, timeout = transport.calls[0]
        self.assertEqual(url, WEBHOOK)
        self.assertEqual(payload["content"], "Wiadomość testowa")
        self.assertNotIn("embeds", payload)
        self.assertEqual(payload["allowed_mentions"], {"parse": []})
        self.assertEqual(timeout, 10.0)

    def test_title_uses_embed_with_url(self) -> None:
        transport = RecordingTransport()
        notifier(transport).send(
            "Opis", title="Tytuł", url="https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        )
        payload = transport.calls[0][1]
        embed = payload["embeds"][0]
        self.assertEqual(embed["title"], "Tytuł")
        self.assertEqual(embed["description"], "Opis")
        self.assertEqual(embed["url"], "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        self.assertNotIn("content", payload)

    def test_username_is_forwarded(self) -> None:
        transport = RecordingTransport()
        notifier(transport, username="yt2abs").send("x")
        self.assertEqual(transport.calls[0][1]["username"], "yt2abs")
        transport.calls.clear()
        notifier(transport).send("x", username="override")
        self.assertEqual(transport.calls[0][1]["username"], "override")

    def test_limits_are_truncated(self) -> None:
        transport = RecordingTransport()
        notifier(transport).send("x" * 5000)
        content = transport.calls[0][1]["content"]
        self.assertEqual(len(content), CONTENT_LIMIT)
        self.assertTrue(content.endswith("…"))

        transport.calls.clear()
        notifier(transport).send("y" * 9000, title="t" * 500)
        embed = transport.calls[0][1]["embeds"][0]
        self.assertEqual(len(embed["title"]), EMBED_TITLE_LIMIT)
        self.assertEqual(len(embed["description"]), EMBED_DESCRIPTION_LIMIT)


class ErrorHandlingTests(unittest.TestCase):
    def test_http_error_is_reported_not_raised(self) -> None:
        transport = RecordingTransport(
            exc=http_error(400, '{"message": "Invalid Webhook Token", "code": 50027}')
        )
        result = notifier(transport).send("hello")
        self.assertFalse(result.delivered)
        self.assertEqual(result.status, 400)
        self.assertIn("Invalid Webhook Token", result.error or "")

    def test_timeout_is_reported_not_raised(self) -> None:
        transport = RecordingTransport(exc=TimeoutError("timed out"))
        result = notifier(transport).send("hello")
        self.assertFalse(result.delivered)
        self.assertIn("timeout", result.error or "")

    def test_urlerror_is_reported_not_raised(self) -> None:
        transport = RecordingTransport(exc=urllib.error.URLError("name resolution"))
        result = notifier(transport).send("hello")
        self.assertFalse(result.delivered)
        self.assertIn("name resolution", result.error or "")

    def test_unexpected_status_is_reported(self) -> None:
        transport = RecordingTransport(status=500)
        result = notifier(transport).send("hello")
        self.assertFalse(result.delivered)
        self.assertEqual(result.status, 500)
        self.assertIn("500", result.error or "")

    def test_429_retries_once_honouring_retry_after(self) -> None:
        clock = FakeClock()
        transport = SequenceTransport(
            [http_error(429, '{"retry_after": 2.5, "message": "rate limited"}'), 204]
        )
        result = notifier(transport, clock=clock, sleep=clock.sleep).send("hello")
        self.assertTrue(result.delivered)
        self.assertEqual(transport.calls, 2)
        self.assertEqual(clock.sleeps, [2.5])

    def test_429_twice_gives_up(self) -> None:
        clock = FakeClock()
        transport = SequenceTransport(
            [http_error(429, '{"retry_after": 0.5}'), http_error(429, '{"retry_after": 0.5}')]
        )
        result = notifier(transport, clock=clock, sleep=clock.sleep).send("hello")
        self.assertFalse(result.delivered)
        self.assertEqual(result.status, 429)
        self.assertEqual(transport.calls, 2)

    def test_log_target_does_not_leak_token(self) -> None:
        target = _safe_target(WEBHOOK)
        self.assertNotIn("token-secret", target)
        self.assertIn("discord.com", target)


class ThrottleTests(unittest.TestCase):
    def test_waits_between_messages(self) -> None:
        clock = FakeClock()
        transport = RecordingTransport()
        instance = notifier(transport, min_interval=1.0, clock=clock, sleep=clock.sleep)
        instance.send("one")
        instance.send("two")
        self.assertEqual(clock.sleeps, [1.0])

    def test_zero_interval_never_sleeps(self) -> None:
        clock = FakeClock()
        instance = notifier(RecordingTransport(), min_interval=0, clock=clock, sleep=clock.sleep)
        instance.send("one")
        instance.send("two")
        self.assertEqual(clock.sleeps, [])


class FromEnvTests(unittest.TestCase):
    def test_reads_url_username_timeout_and_events(self) -> None:
        instance = DiscordNotifier.from_env(
            {
                "DISCORD_WEBHOOK_URL": WEBHOOK,
                "DISCORD_USERNAME": "yt2abs",
                "DISCORD_NOTIFY_TIMEOUT": "3.5",
                "DISCORD_NOTIFY_EVENTS": "retry,failed,unknown",
            }
        )
        self.assertTrue(instance.enabled)
        self.assertEqual(instance.username, "yt2abs")
        self.assertEqual(instance.timeout, 3.5)
        self.assertEqual(instance.events, frozenset({"retry", "failed"}))

    def test_defaults(self) -> None:
        instance = DiscordNotifier.from_env({})
        self.assertFalse(instance.enabled)
        self.assertEqual(instance.events, frozenset({"imported", "failed"}))
        self.assertEqual(instance.timeout, 10.0)

    def test_all_and_none(self) -> None:
        self.assertEqual(
            DiscordNotifier.from_env({"DISCORD_NOTIFY_EVENTS": "all"}).events,
            frozenset({"imported", "retry", "failed"}),
        )
        self.assertEqual(
            DiscordNotifier.from_env({"DISCORD_NOTIFY_EVENTS": "none"}).events,
            frozenset(),
        )
        self.assertEqual(
            DiscordNotifier.from_env({"DISCORD_NOTIFY_EVENTS": ""}).events, frozenset()
        )

    def test_disable_flag(self) -> None:
        instance = DiscordNotifier.from_env(
            {"DISCORD_WEBHOOK_URL": WEBHOOK, "DISCORD_NOTIFY_DISABLED": "yes"}
        )
        self.assertFalse(instance.enabled)
        generic = DiscordNotifier.from_env(
            {"DISCORD_WEBHOOK_URL": WEBHOOK, "NOTIFY_DISABLED": "1"}
        )
        self.assertFalse(generic.enabled)


class JobMessageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.transport = RecordingTransport()
        self.notifier = notifier(
            self.transport, events={"imported", "retry", "failed"}
        )
        self.job = {
            "video_id": "dQw4w9WgXcQ",
            "title": "Testowy film",
            "channel_title": "Kanał testowy",
            "file_path": "/library/Kanał/2026-01-01 - Test [dQw4w9WgXcQ].m4a",
        }

    def test_imported_message(self) -> None:
        result = self.notifier.job_imported(self.job, dict(self.job))
        self.assertTrue(result.delivered)
        payload = self.transport.calls[0][1]
        embed = payload["embeds"][0]
        self.assertIn("Imported", embed["title"])
        self.assertIn("Testowy film", embed["description"])
        self.assertIn("Kanał testowy", embed["description"])
        self.assertIn("dQw4w9WgXcQ", embed["description"])
        self.assertEqual(
            embed["url"], "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        )

    def test_failed_message(self) -> None:
        result = self.notifier.job_failed(self.job, "RuntimeError: boom", 3)
        self.assertTrue(result.delivered)
        embed = self.transport.calls[0][1]["embeds"][0]
        self.assertIn("Import failed", embed["title"])
        self.assertIn("RuntimeError: boom", embed["description"])
        self.assertIn("Attempts: 3", embed["description"])

    def test_retry_message(self) -> None:
        result = self.notifier.job_retry(self.job, "timeout", 60.0, 1)
        self.assertTrue(result.delivered)
        embed = self.transport.calls[0][1]["embeds"][0]
        self.assertIn("Import retry", embed["title"])
        self.assertIn("Retry in: 60 s", embed["description"])

    def test_event_filter_blocks_disabled_event(self) -> None:
        instance = notifier(self.transport, events={"imported"})
        result = instance.job_failed(self.job, "boom", 3)
        self.assertTrue(result.skipped)
        self.assertEqual(result.reason, "event_disabled")
        self.assertEqual(self.transport.calls, [])

        result = instance.job_imported(self.job, dict(self.job))
        self.assertTrue(result.delivered)


class AppWiringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        config = parse_config(
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
        self.notifier = DiscordNotifier(WEBHOOK, transport=RecordingTransport())
        app = create_app(
            config,
            db=Database(root / "data" / "notify.sqlite3"),
            worker_enabled=False,
            notifier=self.notifier,
        )
        self.client = TestClient(app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def test_health_reports_notifications(self) -> None:
        health = self.client.get("/api/system").json()
        self.assertEqual(
            health["notifications"],
            {"enabled": True, "events": ["failed", "imported"]},
        )


if __name__ == "__main__":
    unittest.main()
