"""ntfy error notifier tests (stdlib only, no network)."""
from __future__ import annotations

import io
import os
import tempfile
import time
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping, cast
from unittest import mock

from fastapi.testclient import TestClient

from yt2abs.app import create_app
from yt2abs.cli import main as cli_main
from yt2abs.config import parse_config
from yt2abs.db import Database
from yt2abs.notify import DiscordNotifier, FanoutNotifier
from yt2abs.ntfy import (
    BATCH_LIMIT,
    MESSAGE_LIMIT,
    TITLE_LIMIT,
    DeliveryError,
    NtfyNotifier,
    _safe_target,
    _verify_ack,
    parse_events,
)
from yt2abs.updater import UpdateResult

BASE = "https://ntfy.example"
TOPIC = "yt2abs-tests"


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
    def __init__(self, *, status: int = 200, exc: BaseException | None = None) -> None:
        self.status = status
        self.exc = exc
        self.calls: list[tuple[str, Mapping[str, Any], float, str | None]] = []

    def __call__(
        self,
        url: str,
        payload: Mapping[str, Any],
        timeout: float,
        token: str | None,
    ) -> int:
        self.calls.append((url, payload, timeout, token))
        if self.exc is not None:
            raise self.exc
        return self.status


class SequenceTransport:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    def __call__(
        self,
        url: str,
        payload: Mapping[str, Any],
        timeout: float,
        token: str | None,
    ) -> int:
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def http_error(
    code: int, body: str = "", headers: Message | None = None
) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        f"{BASE}/{TOPIC}",
        code,
        f"HTTP {code}",
        headers if headers is not None else Message(),
        io.BytesIO(body.encode()),
    )


def retry_after_header(seconds: str) -> Message:
    headers = Message()
    headers["Retry-After"] = seconds
    return headers


def notifier(transport: Any, **kwargs: Any) -> NtfyNotifier:
    kwargs.setdefault("min_interval", 0)
    return NtfyNotifier(BASE, TOPIC, transport=transport, **kwargs)


JOB = {
    "video_id": "dQw4w9WgXcQ",
    "title": "Testowy film",
    "channel_title": "Kanał testowy",
    "file_path": "/library/Kanał/test.m4a",
}


class DisabledTests(unittest.TestCase):
    def test_missing_topic_skips_without_transport(self) -> None:
        transport = RecordingTransport()
        instance = NtfyNotifier(BASE, "", transport=transport)
        result = instance.send("hello")
        self.assertTrue(result.skipped)
        self.assertEqual(result.reason, "not_configured")
        self.assertEqual(transport.calls, [])
        self.assertFalse(instance.configured)
        self.assertFalse(instance.enabled)

    def test_disabled_flag_wins_over_topic(self) -> None:
        transport = RecordingTransport()
        instance = NtfyNotifier(BASE, TOPIC, disabled=True, transport=transport)
        result = instance.send("hello")
        self.assertTrue(result.skipped)
        self.assertEqual(transport.calls, [])

    def test_invalid_url_is_disabled(self) -> None:
        transport = RecordingTransport()
        instance = NtfyNotifier("ftp://ntfy.example", TOPIC, transport=transport)
        result = instance.send("hello")
        self.assertTrue(result.skipped)
        self.assertEqual(transport.calls, [])

    def test_scheme_is_added_and_trailing_slash_removed(self) -> None:
        instance = NtfyNotifier("ntfy.example/", TOPIC)
        self.assertEqual(instance.url, "https://ntfy.example")
        self.assertEqual(instance.endpoint, "https://ntfy.example")
        self.assertTrue(instance.enabled)

    def test_empty_message_skips(self) -> None:
        transport = RecordingTransport()
        result = notifier(transport).send("   ")
        self.assertTrue(result.skipped)
        self.assertEqual(result.reason, "empty_message")
        self.assertEqual(transport.calls, [])


class UrlValidationTests(unittest.TestCase):
    """A topic pasted into NTFY_URL would publish raw JSON to the wrong topic."""

    def test_path_query_fragment_and_userinfo_are_rejected(self) -> None:
        for url in (
            "https://ntfy.sh/yt2abs-topic",
            "https://ntfy.sh/?topic=x",
            "https://ntfy.sh/#fragment",
            "https://user:pass@ntfy.sh",
        ):
            with self.subTest(url=url):
                transport = RecordingTransport()
                instance = NtfyNotifier(url, TOPIC, transport=transport)
                with self.assertLogs("yt2abs.ntfy", level="WARNING") as logs:
                    self.assertFalse(instance.configured)
                    self.assertFalse(instance.enabled)
                    result = instance.send("hello")
                self.assertTrue(result.skipped)
                self.assertEqual(transport.calls, [])
                text = "\n".join(logs.output)
                self.assertIn("NTFY_URL", text)
                self.assertNotIn(TOPIC, text)

    def test_config_error_explains_the_topic_rule(self) -> None:
        instance = NtfyNotifier("https://ntfy.sh/some-topic", TOPIC)
        problem = instance.config_error or ""
        self.assertIn("path", problem)
        self.assertIn("NTFY_TOPIC", problem)
        self.assertIn("NTFY_URL", problem)

    def test_invalid_topic_names_the_topic_variable(self) -> None:
        instance = NtfyNotifier(BASE, "with space")
        self.assertIn("NTFY_TOPIC", instance.config_error or "")
        self.assertIsNone(NtfyNotifier(BASE, "").config_error)

    def test_safe_target_drops_userinfo_and_path(self) -> None:
        target = _safe_target("https://user:pass@ntfy.example/secret-topic?x=1")
        self.assertEqual(target, "https://ntfy.example/…")
        self.assertNotIn("pass", target)
        self.assertNotIn("secret-topic", target)


class PayloadTests(unittest.TestCase):
    def test_json_publish_payload(self) -> None:
        transport = RecordingTransport()
        result = notifier(transport).send(
            "Treść",
            title="Tytuł",
            tags=("x", "warning"),
            priority=4,
            click="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        )
        self.assertTrue(result.delivered)
        self.assertEqual(result.status, 200)
        url, payload, timeout, token = transport.calls[0]
        self.assertEqual(url, BASE)
        self.assertEqual(payload["topic"], TOPIC)
        self.assertEqual(payload["title"], "Tytuł")
        self.assertEqual(payload["message"], "Treść")
        self.assertEqual(payload["tags"], ["x", "warning"])
        self.assertEqual(payload["priority"], 4)
        self.assertEqual(
            payload["click"], "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        )
        self.assertEqual(timeout, 10.0)
        self.assertIsNone(token)

    def test_token_is_forwarded(self) -> None:
        transport = RecordingTransport()
        instance = NtfyNotifier(BASE, TOPIC, token="tk_secret", transport=transport)
        instance.send("x")
        self.assertEqual(transport.calls[0][3], "tk_secret")

    def test_invalid_topic_is_disabled(self) -> None:
        transport = RecordingTransport()
        for topic in ("a b", "with/slash", "x" * 65):
            with self.subTest(topic=topic):
                instance = NtfyNotifier(BASE, topic, transport=transport)
                self.assertFalse(instance.configured)
                result = instance.send("x")
                self.assertTrue(result.skipped)
        self.assertEqual(transport.calls, [])

    def test_limits_are_truncated(self) -> None:
        transport = RecordingTransport()
        notifier(transport).send("x" * 9000, title="t" * 900)
        payload = transport.calls[0][1]
        self.assertLessEqual(len(payload["message"].encode("utf-8")), MESSAGE_LIMIT)
        self.assertEqual(len(payload["title"]), TITLE_LIMIT)
        self.assertTrue(payload["message"].endswith("…"))

    def test_message_limit_counts_bytes_not_characters(self) -> None:
        transport = RecordingTransport()
        notifier(transport).send("ą" * 3000)
        payload = transport.calls[0][1]
        self.assertLessEqual(len(payload["message"].encode("utf-8")), MESSAGE_LIMIT)
        self.assertTrue(payload["message"].endswith("…"))

    def test_non_http_click_is_dropped(self) -> None:
        transport = RecordingTransport()
        notifier(transport).send("x", click="javascript:alert(1)")
        self.assertNotIn("click", transport.calls[0][1])

    def test_priority_is_clamped(self) -> None:
        transport = RecordingTransport()
        notifier(transport).send("x", priority=99)
        self.assertEqual(transport.calls[0][1]["priority"], 5)


class AckTests(unittest.TestCase):
    """A 2xx HTML page (e.g. the ntfy web app) must not count as delivered."""

    def test_accepts_ntfy_publish_ack(self) -> None:
        _verify_ack(
            200, b'{"id":"abc","event":"message","topic":"t"}', "t"
        )

    def test_rejects_2xx_html(self) -> None:
        with self.assertRaises(DeliveryError):
            _verify_ack(200, b"<html><body>ntfy</body></html>", "t")

    def test_rejects_ack_for_a_different_topic_without_echoing_it(self) -> None:
        with self.assertRaises(DeliveryError) as raised:
            _verify_ack(
                200, b'{"id":"abc","event":"message","topic":"other"}', "secret"
            )
        message = str(raised.exception)
        self.assertNotIn("secret", message)
        self.assertNotIn("other", message)

    def test_rejects_ack_without_message_id(self) -> None:
        with self.assertRaises(DeliveryError):
            _verify_ack(200, b'{"event":"message","topic":"t"}', "t")

    def test_rejects_json_without_the_message_event(self) -> None:
        # A JSON body with an id is not enough: only event=message is an ack.
        with self.assertRaises(DeliveryError):
            _verify_ack(200, b'{"id":"abc","topic":"t"}', "t")
        with self.assertRaises(DeliveryError):
            _verify_ack(200, b'{"id":"abc","event":"open","topic":"t"}', "t")

    def test_ack_error_is_reported_not_delivered(self) -> None:
        transport = RecordingTransport(
            exc=DeliveryError("HTTP 200 without an ntfy JSON acknowledgement")
        )
        result = notifier(transport).send("hello")
        self.assertFalse(result.delivered)
        self.assertIn("acknowledgement", result.error or "")
        self.assertEqual(len(transport.calls), 1)


class ErrorHandlingTests(unittest.TestCase):
    def test_http_error_is_reported_not_raised(self) -> None:
        transport = RecordingTransport(exc=http_error(400, '{"error":"bad topic"}'))
        result = notifier(transport).send("hello")
        self.assertFalse(result.delivered)
        self.assertEqual(result.status, 400)
        self.assertIn("bad topic", result.error or "")

    def test_read_timeout_is_not_retried(self) -> None:
        """The request was already sent; a retry could publish twice."""
        clock = FakeClock()
        transport = RecordingTransport(exc=TimeoutError("timed out"))
        result = notifier(transport, clock=clock, sleep=clock.sleep).send("hello")
        self.assertFalse(result.delivered)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(clock.sleeps, [])
        self.assertIn("not retried", result.error or "")

    def test_urlerror_is_retried_then_reported(self) -> None:
        clock = FakeClock()
        transport = RecordingTransport(exc=urllib.error.URLError("name resolution"))
        result = notifier(transport, clock=clock, sleep=clock.sleep).send("hello")
        self.assertFalse(result.delivered)
        self.assertEqual(len(transport.calls), 3)
        self.assertEqual(clock.sleeps, [1.0, 3.0])
        self.assertIn("name resolution", result.error or "")

    def test_unexpected_status_is_reported_not_raised(self) -> None:
        clock = FakeClock()
        transport = RecordingTransport(status=500)
        result = notifier(
            transport, retries=0, clock=clock, sleep=clock.sleep
        ).send("hello")
        self.assertFalse(result.delivered)
        self.assertEqual(result.status, 500)
        self.assertEqual(len(transport.calls), 1)

    def test_429_retries_once_honouring_retry_after(self) -> None:
        clock = FakeClock()
        transport = SequenceTransport(
            [http_error(429, '{"retry_after": 2.5}'), 200]
        )
        result = notifier(
            transport, clock=clock, sleep=clock.sleep
        ).send("hello")
        self.assertTrue(result.delivered)
        self.assertEqual(transport.calls, 2)
        self.assertEqual(clock.sleeps, [2.5])

    def test_failed_delivery_releases_dedupe(self) -> None:
        transport = SequenceTransport([500, 200])
        instance = notifier(transport, retries=0)
        first = instance.job_failed(JOB, "boom", 3)
        self.assertFalse(first.delivered)
        second = instance.job_failed(JOB, "boom", 3)
        self.assertTrue(second.delivered)
        self.assertEqual(transport.calls, 2)


class RetryTests(unittest.TestCase):
    def test_5xx_is_retried_with_backoff_then_delivered(self) -> None:
        clock = FakeClock()
        transport = SequenceTransport([500, 503, 200])
        result = notifier(
            transport, clock=clock, sleep=clock.sleep
        ).send("hello")
        self.assertTrue(result.delivered)
        self.assertEqual(transport.calls, 3)
        self.assertEqual(clock.sleeps, [1.0, 3.0])

    def test_5xx_exhausts_the_bounded_retries(self) -> None:
        clock = FakeClock()
        transport = SequenceTransport([500, 500, 500])
        result = notifier(
            transport, clock=clock, sleep=clock.sleep
        ).send("hello")
        self.assertFalse(result.delivered)
        self.assertEqual(transport.calls, 3)
        self.assertEqual(clock.sleeps, [1.0, 3.0])

    def test_429_uses_the_retry_after_header(self) -> None:
        clock = FakeClock()
        transport = SequenceTransport(
            [http_error(429, "", retry_after_header("2")), 200]
        )
        result = notifier(
            transport, clock=clock, sleep=clock.sleep
        ).send("hello")
        self.assertTrue(result.delivered)
        self.assertEqual(clock.sleeps, [2.0])

    def test_429_without_header_waits_the_ntfy_default(self) -> None:
        clock = FakeClock()
        transport = SequenceTransport([http_error(429, "{}"), 200])
        result = notifier(
            transport, clock=clock, sleep=clock.sleep
        ).send("hello")
        self.assertTrue(result.delivered)
        self.assertEqual(clock.sleeps, [5.0])

    def test_429_with_a_long_retry_after_is_not_retried(self) -> None:
        clock = FakeClock()
        transport = RecordingTransport(
            exc=http_error(429, "", retry_after_header("120"))
        )
        result = notifier(
            transport, clock=clock, sleep=clock.sleep
        ).send("hello")
        self.assertFalse(result.delivered)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(clock.sleeps, [])
        self.assertIn("exceeds the limit", result.error or "")

    def test_connect_timeout_through_urlerror_is_retried(self) -> None:
        clock = FakeClock()
        transport = RecordingTransport(
            exc=urllib.error.URLError(TimeoutError("timed out"))
        )
        result = notifier(
            transport, retries=1, clock=clock, sleep=clock.sleep
        ).send("hello")
        self.assertFalse(result.delivered)
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(clock.sleeps, [1.0])

    def test_connection_reset_is_not_retried(self) -> None:
        # ntfy may have accepted the publish before the connection dropped, so a
        # retry could duplicate the message.
        clock = FakeClock()
        transport = RecordingTransport(
            exc=urllib.error.URLError(ConnectionResetError("connection reset by peer"))
        )
        result = notifier(
            transport, retries=2, clock=clock, sleep=clock.sleep
        ).send("hello")
        self.assertFalse(result.delivered)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(clock.sleeps, [])
        self.assertIn("ConnectionResetError", result.error or "")
        self.assertIn("not retried", result.error or "")


class ThrottleAndDedupeTests(unittest.TestCase):
    def test_waits_between_distinct_messages(self) -> None:
        clock = FakeClock()
        transport = RecordingTransport()
        instance = notifier(
            transport, min_interval=1.0, clock=clock, sleep=clock.sleep
        )
        instance.job_failed(JOB, "boom", 3)
        instance.job_failed({**JOB, "video_id": "otherVideo1"}, "boom", 3)
        self.assertEqual(clock.sleeps, [1.0])
        self.assertEqual(len(transport.calls), 2)

    def test_same_event_and_video_is_sent_once(self) -> None:
        transport = RecordingTransport()
        instance = notifier(transport)
        first = instance.job_failed(JOB, "boom", 3)
        second = instance.job_failed(JOB, "boom", 3)
        self.assertTrue(first.delivered)
        self.assertTrue(second.skipped)
        self.assertEqual(second.reason, "duplicate")
        self.assertEqual(len(transport.calls), 1)

    def test_dedupe_expires_after_ttl(self) -> None:
        clock = FakeClock()
        transport = RecordingTransport()
        instance = notifier(
            transport, dedupe_ttl=60, clock=clock, sleep=clock.sleep
        )
        instance.job_failed(JOB, "boom", 3)
        clock.now += 61
        second = instance.job_failed(JOB, "boom", 3)
        self.assertTrue(second.delivered)
        self.assertEqual(len(transport.calls), 2)

    def test_failed_and_deleted_are_separate_keys(self) -> None:
        transport = RecordingTransport()
        instance = notifier(transport)
        instance.job_failed(JOB, "boom", 3)
        instance.job_deleted(JOB, "retention")
        self.assertEqual(len(transport.calls), 2)

    def test_seen_map_is_bounded(self) -> None:
        transport = RecordingTransport()
        instance = notifier(transport, seen_limit=3)
        for index in range(5):
            instance.job_failed(
                {**JOB, "video_id": f"video{index}"}, "boom", 1
            )
        self.assertLessEqual(len(instance._seen), 3)


class FromEnvTests(unittest.TestCase):
    def test_defaults_are_disabled(self) -> None:
        instance = NtfyNotifier.from_env({})
        self.assertFalse(instance.enabled)
        self.assertEqual(instance.url, "https://ntfy.sh")
        self.assertEqual(instance.events, frozenset({"failed", "deleted"}))
        self.assertEqual(instance.timeout, 10.0)

    def test_compose_defaults_with_empty_topic_make_no_request(self) -> None:
        with mock.patch("yt2abs.ntfy._post_json") as transport:
            instance = NtfyNotifier.from_env(
                {"NTFY_URL": "https://ntfy.sh", "NTFY_TOPIC": ""}
            )
            self.assertFalse(instance.configured)
            self.assertFalse(instance.enabled)
            result = instance.send("hello")
            transport.assert_not_called()
        self.assertTrue(result.skipped)

    def test_reads_topic_url_token_timeout_and_events(self) -> None:
        instance = NtfyNotifier.from_env(
            {
                "NTFY_URL": "https://push.example",
                "NTFY_TOPIC": "yt2abs",
                "NTFY_TOKEN": "tk_x",
                "NTFY_NOTIFY_TIMEOUT": "3.5",
                "NTFY_NOTIFY_EVENTS": "failed,unknown",
            }
        )
        self.assertTrue(instance.enabled)
        self.assertEqual(instance.url, "https://push.example")
        self.assertEqual(instance.topic, "yt2abs")
        self.assertEqual(instance.token, "tk_x")
        self.assertEqual(instance.timeout, 3.5)
        self.assertEqual(instance.events, frozenset({"failed"}))

    def test_timeout_is_capped(self) -> None:
        instance = NtfyNotifier.from_env(
            {"NTFY_TOPIC": "t", "NTFY_NOTIFY_TIMEOUT": "9999"}
        )
        self.assertEqual(instance.timeout, 30.0)

    def test_all_none_and_empty(self) -> None:
        self.assertEqual(
            NtfyNotifier.from_env({"NTFY_NOTIFY_EVENTS": "all"}).events,
            frozenset({"failed", "deleted"}),
        )
        self.assertEqual(
            NtfyNotifier.from_env({"NTFY_NOTIFY_EVENTS": "none"}).events,
            frozenset(),
        )
        self.assertEqual(
            NtfyNotifier.from_env({"NTFY_NOTIFY_EVENTS": ""}).events, frozenset()
        )

    def test_disable_flags(self) -> None:
        for env in (
            {"NTFY_TOPIC": "t", "NTFY_NOTIFY_DISABLED": "yes"},
            {"NTFY_TOPIC": "t", "NOTIFY_DISABLED": "1"},
        ):
            with self.subTest(env=env):
                self.assertFalse(NtfyNotifier.from_env(env).enabled)

    def test_parse_events_ignores_success_events(self) -> None:
        self.assertEqual(parse_events("imported,retry"), frozenset())
        self.assertEqual(parse_events("failed,imported"), frozenset({"failed"}))


class JobMessageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.transport = RecordingTransport()
        self.instance = notifier(self.transport)

    def test_failed_message_contains_channel_video_and_reason(self) -> None:
        result = self.instance.job_failed(JOB, "RuntimeError: boom", 3)
        self.assertTrue(result.delivered)
        payload = self.transport.calls[0][1]
        self.assertIn("Kanał testowy", payload["title"])
        self.assertIn("Kanał testowy", payload["message"])
        self.assertIn("dQw4w9WgXcQ", payload["message"])
        self.assertIn("RuntimeError: boom", payload["message"])
        self.assertEqual(payload["priority"], 4)
        self.assertEqual(payload["tags"], ["x"])

    def test_failed_without_metadata_has_no_dangling_dash(self) -> None:
        result = self.instance.job_failed({"video_id": "abc12345678"}, "boom", 1)
        self.assertTrue(result.delivered)
        payload = self.transport.calls[0][1]
        self.assertEqual(payload["title"], "❌ yt2abs: import failed")
        self.assertNotIn(" — ", payload["title"])
        self.assertNotIn("Channel: -", payload["message"])
        self.assertIn(
            "unknown channel \u2014 error before fetching metadata", payload["message"]
        )

    def test_deleted_message_contains_channel_video_and_reason(self) -> None:
        result = self.instance.job_deleted(JOB, "retention: expired after 7 days")
        self.assertTrue(result.delivered)
        payload = self.transport.calls[0][1]
        self.assertIn("Kanał testowy", payload["title"])
        self.assertIn("dQw4w9WgXcQ", payload["message"])
        self.assertIn("retention: expired after 7 days", payload["message"])
        self.assertIn("File: test.m4a", payload["message"])
        self.assertNotIn("/library", payload["message"])
        self.assertEqual(payload["priority"], 3)

    def test_deleted_without_channel_still_has_a_clean_title(self) -> None:
        self.instance.job_deleted({"video_id": "abc12345678"}, "retention")
        payload = self.transport.calls[0][1]
        self.assertEqual(payload["title"], "🗑 yt2abs: episode deleted")
        self.assertIn("Channel: unknown channel", payload["message"])

    def test_event_filter_blocks_deleted(self) -> None:
        instance = notifier(self.transport, events={"failed"})
        result = instance.job_deleted(JOB, "retention")
        self.assertTrue(result.skipped)
        self.assertEqual(result.reason, "event_disabled")
        self.assertEqual(self.transport.calls, [])

    def test_event_filter_blocks_failed(self) -> None:
        instance = notifier(self.transport, events={"deleted"})
        result = instance.job_failed(JOB, "boom", 3)
        self.assertTrue(result.skipped)
        self.assertEqual(result.reason, "event_disabled")
        self.assertEqual(self.transport.calls, [])

    def test_no_success_hooks(self) -> None:
        self.assertFalse(hasattr(self.instance, "job_imported"))
        self.assertFalse(hasattr(self.instance, "job_retry"))


class BatchTests(unittest.TestCase):
    def test_bulk_deletion_is_one_message_with_count_and_limit(self) -> None:
        transport = RecordingTransport()
        items = [
            {
                "video_id": f"video{index:04d}",
                "title": f"Film {index}",
                "channel_title": "Kanał",
            }
            for index in range(BATCH_LIMIT + 2)
        ]
        result = notifier(transport).job_deleted_batch(
            items, "retention: 0-day policy"
        )
        self.assertTrue(result.delivered)
        self.assertEqual(len(transport.calls), 1)
        payload = transport.calls[0][1]
        self.assertIn(f"{BATCH_LIMIT + 2} episodes", payload["title"])
        self.assertIn(f"\u2026 and 2 more", payload["message"])
        self.assertIn(f"Deleted: {BATCH_LIMIT + 2} episodes", payload["message"])
        self.assertEqual(payload["message"].count("•"), BATCH_LIMIT)
        self.assertIn("retention: 0-day policy", payload["message"])
        self.assertEqual(payload["priority"], 3)

    def test_single_item_batch_uses_singular(self) -> None:
        transport = RecordingTransport()
        notifier(transport).job_deleted_batch(
            [{"video_id": "abc", "title": "Film", "channel_title": "Kanał"}]
        )
        payload = transport.calls[0][1]
        self.assertIn("1 episode", payload["title"])
        self.assertNotIn("1 episodes", payload["title"])

    def test_empty_batch_is_skipped(self) -> None:
        transport = RecordingTransport()
        result = notifier(transport).job_deleted_batch([])
        self.assertTrue(result.skipped)
        self.assertEqual(transport.calls, [])

    def test_batch_honours_the_deleted_event_switch(self) -> None:
        transport = RecordingTransport()
        result = notifier(transport, events={"failed"}).job_deleted_batch(
            [{"video_id": "abc"}]
        )
        self.assertTrue(result.skipped)
        self.assertEqual(result.reason, "event_disabled")
        self.assertEqual(transport.calls, [])


class RecordingBackend:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def _record(self, name: str, *args: Any) -> None:
        self.calls.append((name, args))
        if self.fail:
            raise RuntimeError("backend boom")

    def job_failed(self, job, error, attempts):  # type: ignore[no-untyped-def]
        self._record("failed", job, error, attempts)

    def job_deleted(self, job, reason):  # type: ignore[no-untyped-def]
        self._record("deleted", job, reason)

    def retention_alert(self, payload):  # type: ignore[no-untyped-def]
        self._record("retention_alert", payload)

    def job_deleted_batch(self, items, reason=""):  # type: ignore[no-untyped-def]
        self._record("deleted-batch", items, reason)


class FailureOnlyBackend:
    """Stands in for Discord, which has no job_deleted hook."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def job_failed(self, job, error, attempts):  # type: ignore[no-untyped-def]
        self.calls.append(("failed", (job, error, attempts)))


class FanoutTests(unittest.TestCase):
    def test_dispatches_only_supported_hooks(self) -> None:
        discord, ntfy = FailureOnlyBackend(), RecordingBackend()
        fanout = FanoutNotifier([discord, ntfy])
        fanout.job_failed(JOB, "boom", 2)
        self.assertEqual([name for name, _ in discord.calls], ["failed"])
        self.assertEqual([name for name, _ in ntfy.calls], ["failed"])

        fanout.job_deleted(JOB, "retention")
        self.assertEqual([name for name, _ in discord.calls], ["failed"])
        self.assertEqual([name for name, _ in ntfy.calls], ["failed", "deleted"])

    def test_batch_reaches_only_backends_with_the_hook(self) -> None:
        discord, ntfy = FailureOnlyBackend(), RecordingBackend()
        results = FanoutNotifier([discord, ntfy]).job_deleted_batch(
            [{"video_id": "abc"}], "retention"
        )
        self.assertEqual(len(results), 1)
        self.assertEqual([name for name, _ in ntfy.calls], ["deleted-batch"])

    def test_one_backend_failure_does_not_block_another(self) -> None:
        broken, working = RecordingBackend(fail=True), RecordingBackend()
        fanout = FanoutNotifier([broken, working])
        results = fanout.job_failed(JOB, "boom", 1)
        self.assertEqual(len(results), 1)
        self.assertEqual(len(working.calls), 1)

    def test_skips_none_backends(self) -> None:
        fanout = FanoutNotifier([None, RecordingBackend()])
        self.assertEqual(len(fanout.backends), 1)


class RetentionAlertTests(unittest.TestCase):
    def setUp(self) -> None:
        self.transport = RecordingTransport()
        self.instance = notifier(self.transport)

    def test_retention_alert_ignores_event_filter(self) -> None:
        instance = notifier(self.transport, events={"deleted"})
        result = instance.retention_alert(
            {
                "reason": "too_many_missing",
                "message": "12 of 40 episodes disappeared",
                "consequence": "zero writes",
                "would_reconcile": [{"video_id": "a"}] * 12,
            }
        )
        self.assertTrue(result.delivered)
        payload = self.transport.calls[0][1]
        self.assertIn("retention safety fuse", payload["title"])
        self.assertIn("too_many_missing", payload["message"])
        self.assertIn("12", payload["message"])
        self.assertEqual(payload["priority"], 5)

    def test_retention_alert_is_deduped_per_reason(self) -> None:
        payload = {"reason": "library_listing_failed", "message": "ABS 404"}
        self.assertTrue(self.instance.retention_alert(payload).delivered)
        second = self.instance.retention_alert(payload)
        self.assertTrue(second.skipped)
        self.assertEqual(len(self.transport.calls), 1)


class FanoutRetentionAlertTests(unittest.TestCase):
    def test_dispatches_retention_alert_to_supporting_backends(self) -> None:
        backend = RecordingBackend()
        fanout = FanoutNotifier([FailureOnlyBackend(), backend])
        fanout.retention_alert({"reason": "too_many_missing", "message": "boom"})
        self.assertEqual([name for name, _ in backend.calls], ["retention_alert"])


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
        self.ntfy = NtfyNotifier(BASE, TOPIC, transport=RecordingTransport())
        app = create_app(
            config,
            db=Database(root / "data" / "ntfy.sqlite3"),
            worker_enabled=False,
            notifier=DiscordNotifier(""),
            ntfy_notifier=self.ntfy,
        )
        self.client = TestClient(app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def test_health_reports_ntfy_without_topic(self) -> None:
        health = self.client.get("/api/system").json()
        self.assertEqual(
            health["ntfy"],
            {
                "enabled": True,
                "configured": True,
                "events": ["deleted", "failed"],
            },
        )
        self.assertNotIn(TOPIC, str(health))


class StubAbs:
    """Empty ABS library: the retention thread has nothing to do."""

    def list_library_items(self, library_id: str) -> list[dict[str, Any]]:
        return []


class FakeUpdater:
    last_result = UpdateResult(
        checked_at="2026-10-20T00:00:00Z", current="2026.10.20"
    )

    def version_of(self) -> str:
        return "2026.10.20"

    def run_forever(self, stop_event: Any, interval_seconds: float) -> None:
        stop_event.wait(30)


RETRY_ID = "retryVideo1"
IMPORTED_ID = "importedVid1"
FAILED_ID = "failedVideo"


def worker_pipeline(job: Mapping[str, Any], report: Any) -> Any:
    """Fail once then succeed (retry), fail permanently, or import on the first try."""
    video_id = str(job["video_id"])
    report("download")
    if video_id == IMPORTED_ID:
        return {"title": "Zaimportowany", "file_path": "/lib/ok.m4a"}
    if video_id == RETRY_ID and int(job.get("attempts") or 0) >= 1:
        # Second attempt succeeds, so this video only ever produced a `retry`.
        return {"title": "Po ponowieniu", "file_path": "/lib/retry.m4a"}
    raise RuntimeError(f"boom {video_id}")


class AppWorkerWiringTests(unittest.TestCase):
    """The real ``create_app`` path: worker -> fan-out -> ntfy, only ``failed``."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.config = parse_config(
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
                "worker": {
                    "max_attempts": 2,
                    "retry_backoff_seconds": [0],
                    "poll_interval": 0.01,
                },
                # This test is about worker -> fan-out -> ntfy only; a startup
                # retention pass would add its own safety-fuse alert to ntfy.
                "retention": {"run_on_startup": False},
                "channels": {},
            }
        )
        self.db = Database(root / "data" / "worker-ntfy.sqlite3")
        self.transport = RecordingTransport()
        self.ntfy = NtfyNotifier(
            BASE, TOPIC, min_interval=0, retries=0, transport=self.transport
        )
        pipeline = SimpleNamespace(
            process=worker_pipeline,
            abs=StubAbs(),
            discard=lambda fields: None,
        )
        self.app = create_app(
            self.config,
            db=self.db,
            pipeline=cast(Any, pipeline),
            worker_enabled=True,
            notifier=DiscordNotifier(""),
            ntfy_notifier=self.ntfy,
            updater=cast(Any, FakeUpdater()),
        )
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def _wait_for(self, video_id: str, status: str, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            row = self.db.get_video(video_id)
            if row is not None and row["status"] == status:
                return
            time.sleep(0.02)
        row = self.db.get_video(video_id)
        self.fail(f"{video_id} did not reach {status}: {row}")

    def test_only_failed_events_reach_ntfy(self) -> None:
        self.db.create_or_requeue(RETRY_ID)
        self.db.create_or_requeue(IMPORTED_ID)
        self.db.create_or_requeue(FAILED_ID)

        # RETRY_ID fails once and then imports, IMPORTED_ID imports first try,
        # FAILED_ID exhausts its attempts: exactly one permanent failure.
        self._wait_for(RETRY_ID, "imported")
        self._wait_for(FAILED_ID, "failed")
        self._wait_for(IMPORTED_ID, "imported")

        self.assertEqual(len(self.transport.calls), 1)
        _url, payload, _timeout, _token = self.transport.calls[0]
        self.assertEqual(payload.get("topic"), TOPIC)
        self.assertIn("import failed", str(payload.get("title")))
        self.assertIn(FAILED_ID, str(payload.get("message")))
        self.assertNotIn(RETRY_ID, str(payload.get("message")))

    def test_retention_shares_the_app_notifier(self) -> None:
        # RET-1b: retention must reach both Discord and ntfy, so it shares the
        # worker fan-out instead of the raw ntfy backend.
        retention = self.app.state.retention
        self.assertIs(retention.notifier, self.app.state.worker.notifier)
        self.assertIn(self.app.state.ntfy, retention.notifier.backends)
        self.assertIs(self.app.state.ntfy, self.ntfy)


class LogHygieneTests(unittest.TestCase):
    def test_warning_does_not_leak_topic_token_or_password(self) -> None:
        instance = NtfyNotifier(
            "https://user:pass@ntfy.sh/topic-secret?x=1",
            "topic-secret",
            token="tk_secret",
        )
        with self.assertLogs("yt2abs.ntfy", level="WARNING") as logs:
            self.assertFalse(instance.configured)
            result = instance.send("hello")
        self.assertTrue(result.skipped)
        text = "\n".join(logs.output)
        self.assertNotIn("topic-secret", text)
        self.assertNotIn("tk_secret", text)
        self.assertNotIn("user:pass", text)
        self.assertIn("NTFY_URL", text)


class CliTests(unittest.TestCase):
    def _run(self, env: Mapping[str, str]) -> tuple[int, str]:
        stderr = io.StringIO()
        with mock.patch.dict(os.environ, dict(env), clear=True):
            with redirect_stderr(stderr):
                code = cli_main(["ntfy-test"])
        return code, stderr.getvalue()

    def test_disabled_without_topic(self) -> None:
        code, stderr = self._run({})
        self.assertEqual(code, 2)
        self.assertIn("NTFY_TOPIC", stderr)

    def test_url_with_path_is_rejected_without_leaking_secrets(self) -> None:
        code, stderr = self._run(
            {
                "NTFY_TOPIC": "supersecret-topic",
                "NTFY_URL": "https://ntfy.sh/other-topic",
                "NTFY_TOKEN": "tk_leak",
            }
        )
        self.assertEqual(code, 2)
        self.assertIn("NTFY_URL", stderr)
        self.assertIn("path", stderr)
        self.assertNotIn("supersecret-topic", stderr)
        self.assertNotIn("other-topic", stderr)
        self.assertNotIn("tk_leak", stderr)

    def test_delivered_message_exits_zero(self) -> None:
        transport = RecordingTransport()
        instance = NtfyNotifier(
            BASE,
            "supersecret-topic",
            token="tk_secret",
            min_interval=0,
            retries=0,
            transport=transport,
        )
        stdout = io.StringIO()
        with mock.patch(
            "yt2abs.cli.NtfyNotifier.from_env", return_value=instance
        ), redirect_stdout(stdout):
            code = cli_main(["ntfy-test", "--message", "hello"])
        self.assertEqual(code, 0)
        text = stdout.getvalue()
        self.assertIn("OK", text)
        self.assertNotIn("supersecret-topic", text)
        self.assertNotIn("tk_secret", text)

    def test_failed_delivery_exits_one_without_leaking_secrets(self) -> None:
        transport = RecordingTransport(exc=http_error(500, "boom"))
        instance = NtfyNotifier(
            BASE,
            "supersecret-topic",
            token="tk_secret",
            min_interval=0,
            retries=0,
            transport=transport,
        )
        stderr = io.StringIO()
        with mock.patch(
            "yt2abs.cli.NtfyNotifier.from_env", return_value=instance
        ), redirect_stderr(stderr):
            code = cli_main(["ntfy-test", "--message", "hello"])
        self.assertEqual(code, 1)
        text = stderr.getvalue()
        self.assertIn("ERROR", text)
        self.assertNotIn("supersecret-topic", text)
        self.assertNotIn("tk_secret", text)


if __name__ == "__main__":
    unittest.main()
