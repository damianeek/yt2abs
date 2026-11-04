"""Error-only ntfy push notifications for yt2abs.

The channel is deliberately narrow: a message is sent only when a job becomes
permanently ``failed`` (downloader exhausted its retries) or when retention
removes an episode (``deleted``). Successful imports and retries are never
published here.

Configuration is environment-only, mirroring the Discord notifier, and disabled
by default:

``NTFY_TOPIC``
    Target topic. Empty means notifications are disabled (no HTTP requests).
``NTFY_URL``
    Server base URL (default ``https://ntfy.sh``); a scheme is added when the
    value has none, a trailing slash is ignored. It must be the server root:
    a path, query, fragment or userinfo is rejected, because the topic belongs
    in ``NTFY_TOPIC`` and JSON publishing only works on ``/``.
``NTFY_TOKEN``
    Optional bearer token for protected topics.
``NTFY_NOTIFY_DISABLED``
    ``1``/``true``/``yes``/``on`` disables delivery even with a topic; the
    generic ``NOTIFY_DISABLED`` switch is honoured too.
``NTFY_NOTIFY_TIMEOUT``
    Per-request timeout in seconds (default 10, capped at 30).
``NTFY_NOTIFY_EVENTS``
    Comma-separated ``failed``/``deleted``. ``all`` enables both, ``none``
    disables them; the default is ``failed,deleted``.

Delivery: JSON is POSTed to the server root and a message counts as delivered
only when ntfy answers with a 2xx publish acknowledgement (``event``
``message``, a message id and the same topic). Redirects are refused so the
bearer token can never be forwarded to another host. Transient failures
(5xx, 429 and connection errors) are retried a bounded number of times, with
``Retry-After`` honoured for 429. Neither a read timeout nor a connection reset
while the acknowledgement is read is retried: the request has already been sent,
so a retry could publish the same message twice.

Anti-spam: one message per ``(event, video_id)`` within ``dedupe_ttl`` seconds
(one hour by default) and a minimum interval between distinct messages. A
failed delivery releases the dedupe reservation, so a later explicit call can
retry.
"""
from __future__ import annotations

import json
import logging
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .notify import NotifyResult
from .urls import canonical_url

logger = logging.getLogger(__name__)

DEFAULT_URL = "https://ntfy.sh"
DEFAULT_TIMEOUT = 10.0
MAX_TIMEOUT = 30.0
DEFAULT_MIN_INTERVAL = 1.0
DEFAULT_DEDUPE_TTL = 3600.0
DEFAULT_SEEN_LIMIT = 1024
DEFAULT_RETRIES = 2
DEFAULT_RETRY_DELAYS = (1.0, 3.0)
# ntfy refills anonymous rate limits at roughly one request per five seconds.
DEFAULT_RETRY_AFTER = 5.0
MAX_RETRY_WAIT = 15.0
BATCH_LIMIT = 10
ACK_READ_LIMIT = 64 * 1024

NTFY_EVENTS = ("failed", "deleted")
DEFAULT_EVENTS = NTFY_EVENTS

TITLE_LIMIT = 250
# ntfy counts message bytes (4096), not characters: non-ASCII text and emoji
# are multi-byte, so the payload is truncated by encoded length.
MESSAGE_LIMIT = 4096
TOPIC_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

Transport = Callable[[str, Mapping[str, Any], float, str | None], int]


class DeliveryError(RuntimeError):
    """A 2xx response that is not an ntfy publish acknowledgement."""


class NtfyNotifier:
    """Minimal ntfy publisher with throttling, dedupe and error isolation."""

    def __init__(
        self,
        url: str = DEFAULT_URL,
        topic: str = "",
        *,
        token: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        min_interval: float = DEFAULT_MIN_INTERVAL,
        dedupe_ttl: float = DEFAULT_DEDUPE_TTL,
        seen_limit: int = DEFAULT_SEEN_LIMIT,
        events: Iterable[str] | None = None,
        disabled: bool = False,
        retries: int = DEFAULT_RETRIES,
        retry_delays: Iterable[float] = DEFAULT_RETRY_DELAYS,
        max_retry_wait: float = MAX_RETRY_WAIT,
        transport: Transport | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.url = _normalise_url(url)
        self.topic = (topic or "").strip().strip("/")
        self.token = (token or "").strip() or None
        self.timeout = min(MAX_TIMEOUT, _positive_float(timeout, DEFAULT_TIMEOUT))
        self.min_interval = max(0.0, float(min_interval))
        self.dedupe_ttl = max(0.0, float(dedupe_ttl))
        self.seen_limit = max(1, int(seen_limit))
        self.retries = max(0, int(retries))
        delays = tuple(max(0.0, float(delay)) for delay in retry_delays)
        self.retry_delays = delays or DEFAULT_RETRY_DELAYS
        self.max_retry_wait = max(0.0, float(max_retry_wait))
        self._events = frozenset(
            DEFAULT_EVENTS if events is None else parse_events(",".join(events))
        )
        self._disabled = bool(disabled)
        self._transport = transport or _post_json
        self._clock = clock
        self._sleep = sleep
        self._throttle_lock = threading.Lock()
        self._seen_lock = threading.Lock()
        self._last_sent: float | None = None
        self._seen: OrderedDict[str, float] = OrderedDict()
        self._missing_warned = False
        self._config_warned = False
        self._plain_http_warned = False

    # ------------------------------------------------------------------ config
    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "NtfyNotifier":
        source = os.environ if env is None else env
        return cls(
            source.get("NTFY_URL") or DEFAULT_URL,
            source.get("NTFY_TOPIC", ""),
            token=source.get("NTFY_TOKEN"),
            timeout=_positive_float(source.get("NTFY_NOTIFY_TIMEOUT"), DEFAULT_TIMEOUT),
            disabled=_truthy(source.get("NTFY_NOTIFY_DISABLED"))
            or _truthy(source.get("NOTIFY_DISABLED")),
            events=parse_events(source.get("NTFY_NOTIFY_EVENTS")),
        )

    @property
    def config_error(self) -> str | None:
        """Why a topic that is set cannot be used, or None when it can."""
        if not self.topic:
            return None
        if TOPIC_RE.fullmatch(self.topic) is None:
            return (
                "NTFY_TOPIC must match [A-Za-z0-9_-]{1,64} "
                "(no spaces, slashes or non-ASCII characters)"
            )
        return _url_problem(self.url)

    @property
    def configured(self) -> bool:
        """True when both a valid topic and a bare http(s) server URL are set."""
        if not self.topic:
            return False
        problem = self.config_error
        if problem is None:
            return True
        self._warn_config_once(problem)
        return False

    @property
    def disabled(self) -> bool:
        return self._disabled

    @property
    def enabled(self) -> bool:
        return self.configured and not self._disabled

    @property
    def events(self) -> frozenset[str]:
        return self._events

    @property
    def endpoint(self) -> str:
        """JSON publishing always POSTs to the server root; the topic is in the body."""
        return self.url

    # ------------------------------------------------------------------ public
    def send(
        self,
        message: str,
        *,
        title: str = "",
        tags: Iterable[str] = (),
        priority: int | None = None,
        click: str | None = None,
        dedupe_key: str | None = None,
    ) -> NotifyResult:
        """Publish one message; never raises on delivery problems."""
        if not self.enabled:
            self._warn_disabled_once()
            return NotifyResult(skipped=True, reason="not_configured")
        self._warn_plain_http_once()

        body = (message or "").strip()
        if not body:
            return NotifyResult(skipped=True, reason="empty_message")

        payload = build_payload(
            body,
            topic=self.topic,
            title=title,
            tags=tags,
            priority=priority,
            click=click,
        )
        if dedupe_key and not self._reserve(dedupe_key):
            logger.debug("ntfy notification suppressed as duplicate: %s", dedupe_key)
            return NotifyResult(skipped=True, reason="duplicate")

        self._throttle()
        result = self._deliver(payload)
        if not result.delivered and dedupe_key:
            self._release(dedupe_key)
        return result

    def job_failed(
        self, job: Mapping[str, Any], error: str, attempts: int
    ) -> NotifyResult:
        """Permanently failed download: channel title, video id and reason."""
        if "failed" not in self.events:
            return NotifyResult(skipped=True, reason="event_disabled")

        video_id = str(job.get("video_id") or "")
        title = str(job.get("title") or video_id or "video")
        channel = str(job.get("channel_title") or "").strip()
        # Most permanent failures happen before metadata is fetched (private or
        # removed video, region block, broken extractor), so the channel is
        # best effort and the title must not end with a dangling dash.
        label = channel or "unknown channel \u2014 error before fetching metadata"
        heading = "❌ yt2abs: import failed"
        if channel:
            heading = f"{heading} — {channel}"
        link = _video_link(video_id)
        lines = [
            f"Video: {title}",
            f"Channel: {label}",
            f"ID: {video_id or '-'}",
            f"Reason: {error}",
            f"Attempts: {attempts}",
        ]
        if link:
            lines.append(link)
        return self.send(
            "\n".join(lines),
            title=_truncate(heading, TITLE_LIMIT),
            tags=("x",),
            priority=4,
            click=link,
            dedupe_key=f"failed:{video_id}",
        )

    def job_deleted(self, job: Mapping[str, Any], reason: str) -> NotifyResult:
        """Episode removed from Audiobookshelf (retention): id and reason."""
        if "deleted" not in self.events:
            return NotifyResult(skipped=True, reason="event_disabled")

        video_id = str(job.get("video_id") or "")
        title = str(job.get("title") or video_id or "video")
        channel = str(job.get("channel_title") or "").strip()
        label = channel or "unknown channel"
        heading = "🗑 yt2abs: episode deleted"
        if channel:
            heading = f"{heading} — {channel}"
        link = _video_link(video_id)
        lines = [
            f"Video: {title}",
            f"Channel: {label}",
            f"ID: {video_id or '-'}",
            f"Reason: {reason}",
        ]
        # Only the file name: full paths are internal and ntfy.sh is public.
        file_name = Path(str(job.get("file_path") or "")).name
        if file_name:
            lines.append(f"File: {file_name}")
        if link:
            lines.append(link)
        return self.send(
            "\n".join(lines),
            title=_truncate(heading, TITLE_LIMIT),
            tags=("wastebasket",),
            priority=3,
            click=link,
            dedupe_key=f"deleted:{video_id}",
        )

    def retention_alert(self, payload: Mapping[str, Any]) -> NotifyResult:
        """Safety-fuse alarm: reconcile was blocked, nothing was written.

        This is an operational alarm, not a job event, so it ignores
        ``NTFY_NOTIFY_EVENTS``; the one-hour dedupe keeps an hourly failing pass
        from becoming a flood.
        """
        reason = str(payload.get("reason") or "unknown")
        message = str(payload.get("message") or "")
        consequence = str(payload.get("consequence") or "")
        lines = [f"Reason: {reason}"]
        if message:
            lines.append(f"Issue: {message}")
        if consequence:
            lines.append(f"Consequence: {consequence}")
        would = payload.get("would_reconcile") or []
        if isinstance(would, list) and would:
            lines.append(f"Rows to mark: {len(would)}")
        return self.send(
            "\n".join(lines),
            title="⚠️ yt2abs: retention safety fuse",
            tags=("warning",),
            priority=5,
            dedupe_key=f"retention-blocked:{reason}",
        )

    def job_deleted_batch(
        self, items: Sequence[Mapping[str, Any]], reason: str = ""
    ) -> NotifyResult:
        """One summary message for a retention pass that deleted several rows.

        Retention calls this once per pass, after the pass and outside its
        lock, so a bulk cleanup cannot produce one push per episode and burn
        the ntfy rate limit that ``failed`` alerts also need.
        """
        if "deleted" not in self.events:
            return NotifyResult(skipped=True, reason="event_disabled")
        entries = [item for item in items if item]
        if not entries:
            return NotifyResult(skipped=True, reason="empty_message")

        count = len(entries)
        plural = _plural_en(count, "episode", "episodes")
        lines: list[str] = []
        for item in entries[:BATCH_LIMIT]:
            title = str(item.get("title") or item.get("video_id") or "video")
            channel = str(item.get("channel_title") or "").strip()
            suffix = f" — {channel}" if channel else ""
            video_id = str(item.get("video_id") or "")
            identifier = f" ({video_id})" if video_id else ""
            lines.append(f"• {title}{suffix}{identifier}")
        if count > BATCH_LIMIT:
            lines.append(f"\u2026 and {count - BATCH_LIMIT} more")
        lines.append(f"Deleted: {count} {plural}")
        if reason:
            lines.append(f"Reason: {reason}")
        return self.send(
            "\n".join(lines),
            title=_truncate(f"🗑 yt2abs: retention deleted {count} {plural}", TITLE_LIMIT),
            tags=("wastebasket",),
            priority=3,
        )

    # ---------------------------------------------------------------- internals
    def _deliver(self, payload: Mapping[str, Any]) -> NotifyResult:
        """POST once, then retry only failures that cannot duplicate a message."""
        attempt = 0
        while True:
            attempt += 1
            try:
                status = self._transport(
                    self.endpoint, payload, self.timeout, self.token
                )
            except urllib.error.HTTPError as exc:
                code = int(exc.code)
                if 300 <= code < 400:
                    # urllib would turn a POST into a GET and could forward the
                    # Authorization header to another host; the opener refuses
                    # that, this is only a defence for injected transports.
                    return self._failure(
                        f"HTTP {code}: redirects are not followed - "
                        "set NTFY_URL to the final server root",
                        status=code,
                    )
                body = _read_body(exc)
                if code == 429 and attempt <= self.retries:
                    wait = _retry_after(exc.headers, body)
                    if wait <= self.max_retry_wait:
                        logger.info("ntfy rate limited; retrying in %.1fs", wait)
                        self._sleep(wait)
                        continue
                    return self._failure(
                        f"HTTP 429: retry after {wait:.0f}s exceeds the limit",
                        status=code,
                    )
                if 500 <= code < 600 and attempt <= self.retries:
                    logger.info("ntfy server error %d; retrying", code)
                    self._sleep(self._retry_delay(attempt))
                    continue
                return self._failure(f"HTTP {code}: {body}", status=code)
            except urllib.error.URLError as exc:
                reason = getattr(exc, "reason", exc)
                if isinstance(
                    reason, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)
                ):
                    # The connection died while ntfy was answering: the publish
                    # may already have happened, so retrying could duplicate it.
                    return self._failure(
                        f"{type(reason).__name__}: {reason} "
                        "(not retried: the message may have arrived)"
                    )
                # Connect-level failures (DNS, refused, connect timeout) mean the
                # request never reached ntfy, so a retry is safe.
                if attempt <= self.retries:
                    logger.info("ntfy connection error; retrying: %s", exc)
                    self._sleep(self._retry_delay(attempt))
                    continue
                detail = str(reason)
                prefix = "" if isinstance(reason, str) else f"{type(reason).__name__}: "
                return self._failure(f"{prefix}{detail}")
            except (TimeoutError, socket.timeout):
                # A read timeout may happen after ntfy accepted the message:
                # retrying could publish it twice, so report the uncertainty.
                return self._failure(
                    f"timeout after {self.timeout:.0f} s "
                    "(not retried: the message may have arrived)"
                )
            except DeliveryError as exc:
                return self._failure(str(exc))
            except Exception as exc:  # noqa: BLE001 - notifications must never crash callers
                logger.exception("ntfy notification transport failed")
                return self._failure(f"{type(exc).__name__}: {exc}")

            code = int(status)
            if 200 <= code < 300:
                logger.debug("ntfy notification delivered (status=%s)", status)
                return NotifyResult(delivered=True, status=code)
            if code == 429 and attempt <= self.retries:
                wait = DEFAULT_RETRY_AFTER
                if wait <= self.max_retry_wait:
                    logger.info("ntfy rate limited; retrying in %.1fs", wait)
                    self._sleep(wait)
                    continue
            if 500 <= code < 600 and attempt <= self.retries:
                logger.info("ntfy server error %d; retrying", code)
                self._sleep(self._retry_delay(attempt))
                continue
            return self._failure(f"unexpected HTTP status {status}", status=code)

    def _retry_delay(self, attempt: int) -> float:
        """Backoff for the given retry number (1-based), bounded by the cap."""
        index = min(max(0, attempt - 1), len(self.retry_delays) - 1)
        return min(self.max_retry_wait, self.retry_delays[index])

    def _failure(self, error: str, *, status: int | None = None) -> NotifyResult:
        logger.warning(
            "ntfy notification failed (%s): %s", _safe_target(self.url), error
        )
        return NotifyResult(delivered=False, status=status, error=error[:500])

    def _throttle(self) -> None:
        if self.min_interval <= 0:
            return
        with self._throttle_lock:
            now = self._clock()
            if self._last_sent is not None:
                wait = self.min_interval - (now - self._last_sent)
                if wait > 0:
                    self._sleep(wait)
                    now = self._clock()
            self._last_sent = now

    def _reserve(self, key: str) -> bool:
        """Claim a dedupe slot; False when the same event is still fresh."""
        if self.dedupe_ttl <= 0:
            return True
        now = self._clock()
        with self._seen_lock:
            expires_at = self._seen.get(key)
            if expires_at is not None and expires_at > now:
                return False
            self._seen[key] = now + self.dedupe_ttl
            self._seen.move_to_end(key)
            while len(self._seen) > self.seen_limit:
                self._seen.popitem(last=False)
            return True

    def _release(self, key: str) -> None:
        with self._seen_lock:
            self._seen.pop(key, None)

    def _warn_config_once(self, problem: str) -> None:
        if self._config_warned:
            return
        self._config_warned = True
        logger.warning(
            "ntfy notifications disabled: %s (%s)", problem, _safe_target(self.url)
        )

    def _warn_plain_http_once(self) -> None:
        if self._plain_http_warned or not self.token:
            return
        self._plain_http_warned = True
        try:
            scheme = urllib.parse.urlsplit(self.url).scheme.lower()
        except ValueError:
            return
        if scheme == "http":
            logger.warning(
                "ntfy token is sent over plain HTTP to %s - use https "
                "or a trusted network",
                _safe_target(self.url),
            )

    def _warn_disabled_once(self) -> None:
        if self._missing_warned:
            return
        self._missing_warned = True
        if self._disabled:
            logger.info("ntfy notifications disabled by NTFY_NOTIFY_DISABLED")
        elif not self.topic:
            logger.info("NTFY_TOPIC is not set - ntfy notifications are disabled")
        else:
            problem = self.config_error or "ntfy notifications are not configured"
            self._warn_config_once(problem)


# --------------------------------------------------------------------- helpers
def build_payload(
    message: str,
    *,
    topic: str = "",
    title: str = "",
    tags: Iterable[str] = (),
    priority: int | None = None,
    click: str | None = None,
) -> dict[str, Any]:
    """Build an ntfy JSON publish payload (the root endpoint requires ``topic``)."""
    payload: dict[str, Any] = {"message": _truncate_bytes(message, MESSAGE_LIMIT)}
    if topic:
        payload["topic"] = topic
    heading = (title or "").strip()
    if heading:
        payload["title"] = _truncate(heading, TITLE_LIMIT)
    clean_tags = [str(tag).strip() for tag in tags if str(tag).strip()]
    if clean_tags:
        payload["tags"] = clean_tags
    if priority is not None:
        try:
            payload["priority"] = max(1, min(5, int(priority)))
        except (TypeError, ValueError):
            pass
    if click and _is_http_url(click):
        payload["click"] = click
    return payload


def parse_events(value: str | None) -> frozenset[str]:
    """Parse ``NTFY_NOTIFY_EVENTS`` into a set of supported error events."""
    if value is None:
        return frozenset(DEFAULT_EVENTS)
    items = {item.strip().lower() for item in value.split(",") if item.strip()}
    if "all" in items or "*" in items:
        return frozenset(NTFY_EVENTS)
    if "none" in items or not items:
        return frozenset()
    return frozenset(items & set(NTFY_EVENTS))


def _video_link(video_id: str) -> str | None:
    """Canonical YouTube link, or None for a malformed/missing id."""
    if not video_id:
        return None
    try:
        return canonical_url(video_id)
    except Exception:  # noqa: BLE001 - a bad row must not stop notifications
        return None


def _post_json(
    url: str, payload: Mapping[str, Any], timeout: float, token: str | None
) -> int:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "yt2abs-ntfy/0.1",
    }
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    if token:
        # Never attach the bearer token to a redirected request, even if the
        # redirect handler below is replaced or bypassed.
        request.add_unredirected_header("Authorization", f"Bearer {token}")
    opener = urllib.request.build_opener(_NoRedirect)
    with opener.open(request, timeout=timeout) as response:
        status = int(response.status)
        raw = response.read(ACK_READ_LIMIT)
    _verify_ack(status, raw, payload.get("topic"))
    return status


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect instead of turning a POST into an unauthenticated GET."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        raise urllib.error.HTTPError(
            req.full_url,
            code,
            f"redirect to {_safe_target(newurl)} refused",
            headers,
            fp,
        )


def _verify_ack(status: int, raw: bytes, topic: Any) -> None:
    """Require an ntfy publish acknowledgement, not just any 2xx response."""
    if not 200 <= status < 300:
        raise DeliveryError(f"unexpected HTTP status {status}")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise DeliveryError(
            f"HTTP {status} without an ntfy JSON acknowledgement - "
            "is NTFY_URL the server root (no topic path)?"
        ) from None
    if not isinstance(data, dict):
        raise DeliveryError(f"HTTP {status} returned a non-object JSON body")
    event = data.get("event")
    if event != "message":
        # ntfy always answers a publish with event=message; any other JSON
        # object (e.g. a proxy status payload that happens to carry an id)
        # must not be reported as delivered.
        raise DeliveryError(
            f"HTTP {status} returned no ntfy publish acknowledgement "
            "(expected event=message) - is NTFY_URL the server root?"
        )
    if not str(data.get("id") or "").strip():
        raise DeliveryError(f"HTTP {status} acknowledgement has no message id")
    ack_topic = data.get("topic")
    if topic and ack_topic is not None and ack_topic != topic:
        # Never echo the topic: it is the secret of an ntfy.sh channel.
        raise DeliveryError(f"HTTP {status} acknowledgement is for a different topic")


def _read_body(exc: urllib.error.HTTPError) -> str:
    try:
        raw = exc.read() or b""
    except Exception:  # noqa: BLE001 - body is diagnostic only
        raw = b""
    text = raw.decode("utf-8", errors="replace").strip()
    return _truncate(text, 300) if text else str(exc.reason or "no content")


def _retry_after(headers: Any, body: str) -> float:
    """Seconds to wait after a 429: ``Retry-After`` header first, then JSON."""
    raw = None
    if headers is not None:
        try:
            raw = headers.get("Retry-After")
        except AttributeError:
            raw = None
    if raw:
        try:
            return min(MAX_RETRY_WAIT * 4, max(0.0, float(raw)))
        except (TypeError, ValueError):
            pass
    try:
        data = json.loads(body)
    except (TypeError, ValueError):
        return DEFAULT_RETRY_AFTER
    try:
        value = data.get("retry_after")
    except AttributeError:
        return DEFAULT_RETRY_AFTER
    if value is None:
        return DEFAULT_RETRY_AFTER
    try:
        return min(MAX_RETRY_WAIT * 4, max(0.0, float(value)))
    except (TypeError, ValueError):
        return DEFAULT_RETRY_AFTER


def _normalise_url(url: str) -> str:
    text = (url or "").strip()
    if not text:
        return DEFAULT_URL
    if "://" not in text:
        text = "https://" + text
    return text.rstrip("/")


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


def _truncate_bytes(text: str, limit: int) -> str:
    """Truncate to at most ``limit`` UTF-8 bytes without splitting a character."""
    text = text or ""
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    ellipsis = "…".encode("utf-8")
    cut = raw[: max(0, limit - len(ellipsis))]
    while cut:
        try:
            return cut.decode("utf-8").rstrip() + "…"
        except UnicodeDecodeError:
            cut = cut[:-1]
    return "…"[:limit]


def _plural_en(count: int, one: str, many: str) -> str:
    """English plural for 1 / other."""
    return one if count == 1 else many


def _is_http_url(url: str) -> bool:
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    return parts.scheme.lower() in ("http", "https") and bool(parts.netloc)


def _url_problem(url: str) -> str | None:
    """Reject anything but a bare http(s) server root (topic lives elsewhere)."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return "NTFY_URL is not a valid URL"
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return "NTFY_URL must be an absolute http(s) URL"
    if parts.username is not None or parts.password is not None:
        return "NTFY_URL must not contain userinfo - use NTFY_TOKEN instead"
    try:
        parts.port
    except ValueError:
        return "NTFY_URL has an invalid port"
    if parts.path not in ("", "/"):
        return "NTFY_URL must not contain a path - put the topic in NTFY_TOPIC"
    if parts.query or parts.fragment:
        return (
            "NTFY_URL must not contain a query or fragment - "
            "put the topic in NTFY_TOPIC"
        )
    return None


def _safe_target(url: str) -> str:
    """Describe the server for logs without leaking userinfo, topic or token."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return "<invalid ntfy URL>"
    host = parts.hostname
    if not parts.scheme or not host:
        return "<invalid ntfy URL>"
    try:
        port = parts.port
    except ValueError:
        port = None
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if port:
        host = f"{host}:{port}"
    return f"{parts.scheme}://{host}/…"
