"""Polling of one unlisted YouTube playlist: "add a video from the phone".

The user drops a link into an unlisted playlist; this module lists the playlist
ids with ``yt-dlp --flat-playlist`` and queues every unknown video exactly the
same way ``POST /api/jobs`` does.  It is deliberately small and dependency-free
(no YouTube API, no OAuth, no cookies):

* only the video ids are read - no metadata and no download happen here,
* a video already present in ``videos`` is never touched, not even when it is
  ``failed`` or ``deleted`` (the poller must not resurrect retention deletes),
* a listing error is logged and retried on the next tick; it never stops the
  application and never sends a notification,
* removing a video from the playlist is a no-op for Audiobookshelf: retention
  keeps owning the episode.
"""
from __future__ import annotations

import logging
import re
import subprocess
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from .config import Config
from .db import Database, utc_now
from .urls import VIDEO_ID_RE, InvalidPlaylistURL, extract_playlist_id

logger = logging.getLogger(__name__)

#: yt-dlp flags used to read the playlist without downloading anything.
LIST_ARGS: tuple[str, ...] = (
    "--flat-playlist",
    "--print",
    "%(id)s",
    "--no-warnings",
    "--ignore-errors",
)

Runner = Callable[[Sequence[str], float], Any]
Lister = Callable[[str, float], list[str]]


class PlaylistError(RuntimeError):
    """Listing a playlist failed (yt-dlp missing, timed out or non-zero exit).

    The message is safe to log and to expose: the playlist URL/id (an unlisted
    playlist id is a capability token) is redacted by
    :func:`redact_playlist_secrets` before this exception is built.
    """


def redact_playlist_secrets(text: str, url: str) -> str:
    """Remove the playlist URL and id from ``text``.

    ``yt-dlp`` formats extractor errors as ``ERROR: [youtube:tab] <id>: ...``,
    so a raw stderr line would leak the (unlisted) playlist id into logs and
    ``/health``.  Both the exact URL and the bare id are replaced, and any
    leftover ``list=<id>`` query value is masked as a last resort.
    """
    cleaned = str(text or "")
    raw_url = str(url or "").strip()
    try:
        playlist_id = extract_playlist_id(raw_url) if raw_url else ""
    except InvalidPlaylistURL:
        playlist_id = ""
    for secret in (raw_url, playlist_id):
        if secret:
            cleaned = cleaned.replace(secret, "<playlist>")
    return re.sub(r"(list=)[A-Za-z0-9_-]{6,}", r"\1<playlist>", cleaned)


def parse_playlist_ids(output: str) -> list[str]:
    """Return valid video ids from ``yt-dlp --print "%(id)s"`` output.

    Blank lines, warnings and any other garbage are ignored; duplicates keep
    their first position so one playlist entry is queued once.  ``yt-dlp`` may
    also print ``NA`` for an entry it could not resolve - that is simply not a
    valid id and gets dropped here.
    """
    seen: set[str] = set()
    ids: list[str] = []
    for line in str(output or "").splitlines():
        candidate = line.strip().lstrip("\ufeff").strip()
        if not candidate or not VIDEO_ID_RE.match(candidate):
            continue
        if candidate in seen:
            continue
        seen.add(candidate)
        ids.append(candidate)
    return ids


def _default_runner(args: Sequence[str], timeout: float) -> Any:
    return subprocess.run(
        list(args),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def list_playlist_ids(
    url: str,
    *,
    command: str = "yt-dlp",
    extra_args: Sequence[str] = (),
    timeout: float = 120.0,
    runner: Runner | None = None,
) -> list[str]:
    """List the video ids of ``url`` without downloading (network!).

    ``extra_args`` mirrors ``defaults.ytdlp_args`` so the poller works on hosts
    where YouTube needs the same extractor workarounds as the downloader.  The
    URL passed to yt-dlp is canonicalised so a trailing dot or a tracking
    parameter cannot dodge the ``youtube:tab`` extractor.
    """
    raw_url = str(url or "").strip()
    try:
        playlist_id = extract_playlist_id(raw_url)
    except InvalidPlaylistURL:
        playlist_id = ""
    target = (
        f"https://www.youtube.com/playlist?list={playlist_id}"
        if playlist_id
        else raw_url
    )
    args = [command, *LIST_ARGS, *extra_args, target]
    execute = runner or _default_runner
    try:
        completed = execute(args, timeout)
    except subprocess.TimeoutExpired as exc:
        raise PlaylistError(f"yt-dlp did not respond within {int(timeout)} s") from exc
    except OSError as exc:
        raise PlaylistError(f"cannot start yt-dlp ({command}): {exc}") from exc

    returncode = int(getattr(completed, "returncode", 0) or 0)
    stdout = getattr(completed, "stdout", "") or ""
    stderr = getattr(completed, "stderr", "") or ""
    if returncode != 0:
        detail = stderr.strip().splitlines()[-1] if stderr.strip() else ""
        detail = redact_playlist_secrets(detail, raw_url)
        message = f"yt-dlp exited with code {returncode}"
        raise PlaylistError(f"{message}: {detail}" if detail else message)
    return parse_playlist_ids(stdout)


@dataclass
class PlaylistPollResult:
    """Outcome of one poll (kept for ``GET /api/system`` and the logs)."""

    started_at: str
    finished_at: str
    seen: int = 0
    queued: list[str] = field(default_factory=list)
    skipped: int = 0
    error: str | None = None
    disabled: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "seen": self.seen,
            "queued": list(self.queued),
            "skipped": self.skipped,
            "error": self.error,
            "disabled": self.disabled,
        }


class PlaylistPoller:
    """Periodic playlist check; runs in its own thread next to the worker."""

    def __init__(
        self,
        config: Config,
        db: Database,
        *,
        command: str | None = None,
        extra_args: Sequence[str] | None = None,
        runner: Runner | None = None,
        lister: Lister | None = None,
        clock: Callable[[], str] = utc_now,
    ) -> None:
        self.config = config
        self.db = db
        self._command = command
        self._extra_args = None if extra_args is None else tuple(extra_args)
        self.runner = runner
        self.lister = lister
        self.clock = clock
        self.last_result: PlaylistPollResult | None = None
        self.last_run_at: str | None = None
        self._lock = threading.Lock()

    @property
    def command(self) -> str:
        """Resolve the yt-dlp binary lazily: an updater-fetched build may appear
        in ``data/bin`` after this poller was constructed."""
        return self._command or self.config.ytdlp_command

    @property
    def extra_args(self) -> tuple[str, ...]:
        """``defaults.ytdlp_args`` read lazily, like :attr:`command`."""
        if self._extra_args is not None:
            return self._extra_args
        return tuple(self.config.defaults.ytdlp_args)

    # ------------------------------------------------------------------ public
    def poll_once(self) -> PlaylistPollResult:
        """List the playlist and queue unknown ids; never raises on list errors.

        Storage errors still propagate (the caller in :meth:`run_forever` logs
        and retries); a broken playlist only produces ``result.error``.
        """
        cfg = self.config.playlist
        with self._lock:
            started = self.clock()
            if not cfg.enabled or not cfg.url:
                result = PlaylistPollResult(
                    started_at=started, finished_at=started, disabled=True
                )
                self.last_result = result
                self.last_run_at = started
                logger.debug("playlist polling is disabled")
                return result

            try:
                ids = self._list(cfg.url, cfg.timeout)
            except PlaylistError as exc:
                finished = self.clock()
                # A custom lister may raise with raw text; redact again so the
                # unlisted id never reaches the log or /api/system.
                error = redact_playlist_secrets(str(exc), cfg.url)
                result = PlaylistPollResult(
                    started_at=started, finished_at=finished, error=error
                )
                self.last_result = result
                self.last_run_at = started
                logger.warning("playlist listing failed (will retry next tick): %s", error)
                return result

            queued: list[str] = []
            skipped = 0
            for video_id in ids:
                _, created = self.db.queue_new(video_id, source="playlist")
                if created:
                    queued.append(video_id)
                    logger.info("playlist: queued %s", video_id)
                else:
                    skipped += 1

            finished = self.clock()
            result = PlaylistPollResult(
                started_at=started,
                finished_at=finished,
                seen=len(ids),
                queued=queued,
                skipped=skipped,
            )
            self.last_result = result
            self.last_run_at = started
            logger.info(
                "playlist poll: seen=%d queued=%d skipped=%d",
                result.seen,
                len(queued),
                skipped,
            )
            return result

    def run_forever(
        self,
        stop_event: threading.Event,
        interval_seconds: float | None = None,
        *,
        run_immediately: bool = True,
    ) -> None:
        cfg = self.config.playlist
        interval = float(
            cfg.interval_seconds if interval_seconds is None else interval_seconds
        )
        logger.info(
            "playlist poller started (interval=%ss)", int(interval)
        )
        if run_immediately:
            self._safe_poll()
        while not stop_event.wait(max(1.0, interval)):
            self._safe_poll()
        logger.info("playlist poller stopped")

    # ---------------------------------------------------------------- internals
    def _list(self, url: str, timeout: float) -> list[str]:
        if self.lister is not None:
            return list(self.lister(url, timeout))
        return list_playlist_ids(
            url,
            command=self.command,
            extra_args=self.extra_args,
            timeout=timeout,
            runner=self.runner,
        )

    def _safe_poll(self) -> None:
        try:
            self.poll_once()
        except Exception:  # noqa: BLE001 - polling must never kill the thread
            logger.exception("playlist poll iteration failed")
