"""Discord webhook notifications for yt2abs.

Messages are POSTed as JSON to the Discord webhook configured in
``DISCORD_WEBHOOK_URL``. The module uses only the standard library, never
raises on delivery problems and silently skips when the webhook is not
configured, so it is safe to call from the worker loop.

Environment variables
---------------------
``DISCORD_WEBHOOK_URL``
    Full webhook URL (secret). Empty means notifications are disabled.
``DISCORD_USERNAME``
    Optional sender name override.
``DISCORD_NOTIFY_DISABLED``
    ``1``/``true``/``yes``/``on`` disables delivery even when the URL is set.
``DISCORD_NOTIFY_TIMEOUT``
    Per-request timeout in seconds (default 10).
``DISCORD_NOTIFY_EVENTS``
    Comma-separated job events: ``imported``, ``retry``, ``failed``.
    ``all`` enables every event, ``none`` disables job notifications;
    the default is ``imported,failed``.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Sequence

from .urls import canonical_url

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 10.0
DEFAULT_MIN_INTERVAL = 1.0
DEFAULT_EVENTS = ("imported", "failed")
JOB_EVENTS = ("imported", "retry", "failed")

CONTENT_LIMIT = 2000
EMBED_TITLE_LIMIT = 256
EMBED_DESCRIPTION_LIMIT = 4096
USERNAME_LIMIT = 80

Transport = Callable[[str, Mapping[str, Any], float], int]


@dataclass(frozen=True)
class NotifyResult:
    """Outcome of one delivery attempt (never raised to the caller)."""

    delivered: bool = False
    skipped: bool = False
    status: int | None = None
    error: str | None = None
    reason: str | None = None

    @property
    def ok(self) -> bool:
        """True when the message was delivered or notifications are disabled."""
        return self.delivered or self.skipped


class DiscordNotifier:
    """Minimal Discord webhook client with throttling and error isolation."""

    def __init__(
        self,
        webhook_url: str = "",
        *,
        username: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        min_interval: float = DEFAULT_MIN_INTERVAL,
        events: Iterable[str] | None = None,
        disabled: bool = False,
        transport: Transport | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.webhook_url = (webhook_url or "").strip()
        self.username = (username or "").strip() or None
        self.timeout = _positive_float(timeout, DEFAULT_TIMEOUT)
        self.min_interval = max(0.0, float(min_interval))
        self._events = frozenset(
            DEFAULT_EVENTS if events is None else normalise_events(events)
        )
        self._disabled = bool(disabled)
        self._transport = transport or _post_json
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._events_lock = threading.Lock()
        self._last_sent: float | None = None
        self._missing_warned = False

    # ------------------------------------------------------------------ config
    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "DiscordNotifier":
        source = os.environ if env is None else env
        raw_events = source.get("DISCORD_NOTIFY_EVENTS")
        return cls(
            source.get("DISCORD_WEBHOOK_URL", ""),
            username=source.get("DISCORD_USERNAME"),
            timeout=_positive_float(source.get("DISCORD_NOTIFY_TIMEOUT"), DEFAULT_TIMEOUT),
            disabled=_truthy(source.get("DISCORD_NOTIFY_DISABLED"))
            or _truthy(source.get("NOTIFY_DISABLED")),
            events=parse_events(raw_events),
        )

    @property
    def configured(self) -> bool:
        """True when a usable webhook URL is set (ignores the disable switch)."""
        if not self.webhook_url:
            return False
        scheme = urllib.parse.urlsplit(self.webhook_url).scheme.lower()
        return scheme in ("http", "https")

    @property
    def disabled(self) -> bool:
        """True when delivery was switched off by the environment."""
        return self._disabled

    @property
    def enabled(self) -> bool:
        return self.configured and not self._disabled

    @property
    def events(self) -> frozenset[str]:
        with self._events_lock:
            return self._events

    def set_events(self, events: Iterable[str] | None) -> frozenset[str]:
        """Replace the enabled event set at runtime (WebUI); returns the new set."""
        updated = frozenset(
            DEFAULT_EVENTS if events is None else normalise_events(events)
        )
        with self._events_lock:
            self._events = updated
        logger.info(
            "discord notification events updated: %s", ",".join(sorted(updated)) or "none"
        )
        return updated

    # ------------------------------------------------------------------ public
    def send(
        self,
        content: str | None = None,
        *,
        title: str | None = None,
        url: str | None = None,
        username: str | None = None,
    ) -> NotifyResult:
        """Deliver one message; returns a result instead of raising."""
        if not self.enabled:
            self._warn_disabled_once()
            return NotifyResult(skipped=True, reason="not_configured")

        payload = build_payload(
            content,
            title=title,
            url=url,
            username=username or self.username,
        )
        if payload is None:
            return NotifyResult(skipped=True, reason="empty_message")

        self._throttle()
        return self._deliver(payload)

    def job_imported(
        self, job: Mapping[str, Any], fields: Mapping[str, Any]
    ) -> NotifyResult:
        if "imported" not in self.events:
            return NotifyResult(skipped=True, reason="event_disabled")

        video_id = str(fields.get("video_id") or job.get("video_id") or "")
        title = str(fields.get("title") or job.get("title") or video_id or "video")
        channel = str(fields.get("channel_title") or job.get("channel_title") or "-")
        file_path = str(fields.get("file_path") or job.get("file_path") or "-")
        link = canonical_url(video_id) if video_id else None
        lines = [
            f"**{title}**",
            f"Channel: {channel}",
            f"File: `{file_path}`",
        ]
        if link:
            lines.append(link)
        return self.send(
            "\n".join(lines),
            title="✅ Imported to Audiobookshelf",
            url=link,
        )

    def job_retry(
        self,
        job: Mapping[str, Any],
        error: str,
        delay: float,
        attempts: int,
    ) -> NotifyResult:
        if "retry" not in self.events:
            return NotifyResult(skipped=True, reason="event_disabled")

        video_id = str(job.get("video_id") or "")
        title = str(job.get("title") or video_id or "video")
        link = canonical_url(video_id) if video_id else None
        lines = [
            f"**{title}**",
            f"Attempt: {attempts}",
            f"Retry in: {max(0.0, float(delay)):.0f} s",
            f"Error: {error}",
        ]
        if link:
            lines.append(link)
        return self.send(
            "\n".join(lines),
            title="⚠️ Import retry",
            url=link,
        )

    def job_failed(
        self,
        job: Mapping[str, Any],
        error: str,
        attempts: int,
    ) -> NotifyResult:
        if "failed" not in self.events:
            return NotifyResult(skipped=True, reason="event_disabled")

        video_id = str(job.get("video_id") or "")
        title = str(job.get("title") or video_id or "video")
        link = canonical_url(video_id) if video_id else None
        lines = [
            f"**{title}**",
            f"Attempts: {attempts}",
            f"Error: {error}",
        ]
        if link:
            lines.append(link)
        return self.send(
            "\n".join(lines),
            title="❌ Import failed",
            url=link,
        )

    # ---------------------------------------------------------------- internals
    def _deliver(self, payload: Mapping[str, Any]) -> NotifyResult:
        retried = False
        while True:
            try:
                status = self._transport(self.webhook_url, payload, self.timeout)
            except urllib.error.HTTPError as exc:
                body = _read_body(exc)
                if exc.code == 429 and not retried:
                    retried = True
                    self._sleep(_retry_after(body))
                    continue
                return self._failure(f"HTTP {exc.code}: {body}", status=exc.code)
            except urllib.error.URLError as exc:
                reason = getattr(exc, "reason", exc)
                detail = str(reason)
                prefix = "" if isinstance(reason, str) else f"{type(reason).__name__}: "
                return self._failure(f"{prefix}{detail}")
            except (TimeoutError, socket.timeout):
                return self._failure(f"timeout after {self.timeout:.0f} s")
            except Exception as exc:  # noqa: BLE001 - notifications must never crash callers
                logger.exception("discord notification transport failed")
                return self._failure(f"{type(exc).__name__}: {exc}")

            if 200 <= int(status) < 300:
                logger.debug("discord notification delivered (status=%s)", status)
                return NotifyResult(delivered=True, status=int(status))

            return self._failure(f"unexpected HTTP status {status}", status=int(status))

    def _failure(self, error: str, *, status: int | None = None) -> NotifyResult:
        logger.warning(
            "discord notification failed (%s): %s", _safe_target(self.webhook_url), error
        )
        return NotifyResult(delivered=False, status=status, error=error[:500])

    def _throttle(self) -> None:
        if self.min_interval <= 0:
            return
        with self._lock:
            now = self._clock()
            if self._last_sent is not None:
                wait = self.min_interval - (now - self._last_sent)
                if wait > 0:
                    self._sleep(wait)
                    now = self._clock()
            self._last_sent = now

    def retention_alert(self, payload: Mapping[str, Any]) -> NotifyResult:
        """Safety-fuse alarm: reconcile was blocked and nothing was written."""
        reason = str(payload.get("reason") or "unknown")
        message = str(payload.get("message") or "")
        consequence = str(payload.get("consequence") or "")
        lines = [f"Reason: {reason}"]
        if message:
            lines.append(f"Issue: {message}")
        if consequence:
            lines.append(f"Consequence: {consequence}")
        return self.send("\n".join(lines), title="⚠️ yt2abs: retention safety fuse")

    def _warn_disabled_once(self) -> None:
        if self._missing_warned:
            return
        self._missing_warned = True
        if self._disabled:
            logger.info("discord notifications disabled by DISCORD_NOTIFY_DISABLED")
        elif not self.webhook_url:
            logger.warning(
                "DISCORD_WEBHOOK_URL is not set - discord notifications are disabled"
            )
        else:
            logger.warning(
                "DISCORD_WEBHOOK_URL is not a valid http(s) URL - "
                "discord notifications are disabled"
            )


class FanoutNotifier:
    """Dispatch job hooks to every backend that implements them.

    Discord handles ``imported``/``retry``/``failed`` while ntfy handles
    ``failed``/``deleted``; a missing hook is simply skipped. A backend error
    is logged and never affects the other backend or the job queue.
    """

    def __init__(self, notifiers: Iterable[Any] = ()) -> None:
        self._notifiers = tuple(item for item in notifiers if item is not None)

    @property
    def backends(self) -> tuple[Any, ...]:
        return self._notifiers

    def _dispatch(self, method: str, *args: Any) -> list[Any]:
        results: list[Any] = []
        for backend in self._notifiers:
            callback = getattr(backend, method, None)
            if callback is None:
                continue
            try:
                results.append(callback(*args))
            except Exception:  # noqa: BLE001 - one backend must not break another
                logger.exception(
                    "notification backend %s failed in %s",
                    type(backend).__name__,
                    method,
                )
        return results

    def job_imported(
        self, job: Mapping[str, Any], fields: Mapping[str, Any]
    ) -> list[Any]:
        return self._dispatch("job_imported", job, fields)

    def job_retry(
        self, job: Mapping[str, Any], error: str, delay: float, attempts: int
    ) -> list[Any]:
        return self._dispatch("job_retry", job, error, delay, attempts)

    def job_failed(
        self, job: Mapping[str, Any], error: str, attempts: int
    ) -> list[Any]:
        return self._dispatch("job_failed", job, error, attempts)

    def job_deleted(self, job: Mapping[str, Any], reason: str) -> list[Any]:
        return self._dispatch("job_deleted", job, reason)

    def retention_alert(self, payload: Mapping[str, Any]) -> list[Any]:
        return self._dispatch("retention_alert", payload)

    def job_deleted_batch(
        self, items: Sequence[Mapping[str, Any]], reason: str = ""
    ) -> list[Any]:
        return self._dispatch("job_deleted_batch", items, reason)


# --------------------------------------------------------------------- helpers
def build_payload(
    content: str | None,
    *,
    title: str | None = None,
    url: str | None = None,
    username: str | None = None,
) -> dict[str, Any] | None:
    """Build a webhook payload; embeds are used when a title is present."""
    text = (content or "").strip()
    heading = (title or "").strip()
    payload: dict[str, Any] = {"allowed_mentions": {"parse": []}}
    if username:
        payload["username"] = _truncate(username, USERNAME_LIMIT)

    if heading:
        embed: dict[str, Any] = {"title": _truncate(heading, EMBED_TITLE_LIMIT)}
        if text:
            embed["description"] = _truncate(text, EMBED_DESCRIPTION_LIMIT)
        if url and _is_http_url(url):
            embed["url"] = url
        payload["embeds"] = [embed]
        return payload

    if not text:
        return None
    payload["content"] = _truncate(text, CONTENT_LIMIT)
    return payload


def _post_json(url: str, payload: Mapping[str, Any], timeout: float) -> int:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "yt2abs-discord-notify/0.1",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        response.read(1024)  # drain a possible JSON body; status is what matters
        return int(response.status)


def _read_body(exc: urllib.error.HTTPError) -> str:
    try:
        raw = exc.read() or b""
    except Exception:  # noqa: BLE001 - body is diagnostic only
        raw = b""
    text = raw.decode("utf-8", errors="replace").strip()
    return _truncate(text, 300) if text else str(exc.reason or "no content")


def _retry_after(body: str) -> float:
    try:
        data = json.loads(body)
    except (TypeError, ValueError):
        return 1.0
    try:
        return min(30.0, max(0.0, float(data.get("retry_after", 1.0))))
    except (TypeError, ValueError, AttributeError):
        return 1.0


def parse_events(value: str | None) -> frozenset[str]:
    """Parse ``DISCORD_NOTIFY_EVENTS``-style text into a set of job events."""
    if value is None:
        return frozenset(DEFAULT_EVENTS)
    items = {item.strip().lower() for item in value.split(",") if item.strip()}
    if "all" in items or "*" in items:
        return frozenset(JOB_EVENTS)
    if "none" in items or not items:
        return frozenset()
    return frozenset(items & set(JOB_EVENTS))


def normalise_events(events: Iterable[str]) -> frozenset[str]:
    """Keep only known job events (WebUI/API input may contain anything)."""
    return frozenset(
        str(item).strip().lower() for item in events
    ) & frozenset(JOB_EVENTS)


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def _positive_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _truncate(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _is_http_url(url: str) -> bool:
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    return parts.scheme.lower() in ("http", "https") and bool(parts.netloc)


def _safe_target(url: str) -> str:
    """Describe a webhook URL for logs without leaking its token."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return "<invalid webhook URL>"
    if parts.scheme and parts.netloc:
        return f"{parts.scheme}://{parts.netloc}/api/webhooks/…"
    return "<invalid webhook URL>"
