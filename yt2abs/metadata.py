"""Video metadata and cover art retrieval through yt-dlp / HTTP."""
from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

UPLOAD_DATE_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})$")
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) yt2abs/0.1"
DEFAULT_TIMEOUT = 180


class MetadataError(RuntimeError):
    """yt-dlp could not provide usable metadata."""


@dataclass(frozen=True)
class VideoMetadata:
    video_id: str
    channel_id: str
    channel_title: str
    channel_url: str
    title: str
    description: str
    published_at: str | None
    thumbnail_url: str | None
    duration: float | None = None
    chapters: tuple[dict[str, Any], ...] = ()
    is_live: bool = False
    #: Upload time from yt-dlp (epoch seconds).  ABS needs it as ``publishedAt``
    #: (epoch ms) for correct sorting; ``published_at`` alone loses the time.
    published_timestamp: int | None = None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)


def _last_error(stderr: str) -> str:
    lines = [line.strip() for line in (stderr or "").splitlines() if line.strip()]
    errors = [line for line in lines if line.startswith("ERROR:")]
    return errors[0] if errors else (lines[-1] if lines else "unknown yt-dlp error")


def _parse_timestamp(info: Mapping[str, Any]) -> int | None:
    """Upload timestamp (epoch seconds) from yt-dlp metadata."""
    for key in ("timestamp", "release_timestamp"):
        value = info.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return int(value)
    return None


def _parse_upload_date(info: Mapping[str, Any]) -> str | None:
    raw = info.get("upload_date")
    if isinstance(raw, str):
        match = UPLOAD_DATE_RE.match(raw)
        if match:
            year, month, day = match.groups()
            return f"{year}-{month}-{day}"
    timestamp = _parse_timestamp(info)
    if timestamp is not None:
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).date().isoformat()
    return None


def _first_entry(document: Any) -> Mapping[str, Any]:
    if isinstance(document, Mapping):
        entries = document.get("entries")
        if entries:
            for entry in entries:
                if isinstance(entry, Mapping):
                    return entry
        return document
    raise MetadataError("yt-dlp returned an unexpected document")


def fetch_metadata(
    url: str,
    *,
    ytdlp: str = "yt-dlp",
    runner: Callable[..., Any] = subprocess.run,
    timeout: int = DEFAULT_TIMEOUT,
    extra: Sequence[str] = (),
) -> VideoMetadata:
    """Fetch video metadata with one ``yt-dlp --dump-single-json`` call."""
    command = [
        ytdlp,
        "--dump-single-json",
        "--no-playlist",
        "--no-warnings",
        "--skip-download",
        *extra,
        url,
    ]
    try:
        result = runner(command, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        raise MetadataError(f"yt-dlp metadata failed: {exc}") from exc
    if getattr(result, "returncode", 1) != 0:
        raise MetadataError(f"yt-dlp metadata failed: {_last_error(getattr(result, 'stderr', ''))}")
    try:
        document = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise MetadataError("yt-dlp returned invalid JSON metadata") from exc

    info = _first_entry(document)
    video_id = str(info.get("id") or "").strip()
    channel_id = str(info.get("channel_id") or "").strip()
    if not video_id:
        raise MetadataError("yt-dlp metadata has no video id")
    if not channel_id:
        raise MetadataError("yt-dlp metadata has no channel id (is the video public?)")

    title = str(info.get("title") or video_id).strip()
    description = str(info.get("description") or "").strip()
    channel_title = str(info.get("channel") or info.get("uploader") or channel_id).strip()
    channel_url = str(info.get("channel_url") or info.get("uploader_url") or "").strip()
    if not channel_url:
        channel_url = f"https://www.youtube.com/channel/{channel_id}"

    thumbnails = info.get("thumbnails") or []
    thumbnail_url = None
    if isinstance(thumbnails, list) and thumbnails:
        ordered = [
            item
            for item in thumbnails
            if isinstance(item, Mapping) and isinstance(item.get("url"), str)
        ]
        if ordered:
            thumbnail_url = max(
                ordered, key=lambda item: int(item.get("width") or 0)
            )["url"]
    if not thumbnail_url and isinstance(info.get("thumbnail"), str):
        thumbnail_url = info["thumbnail"]

    chapters: list[dict[str, Any]] = []
    raw_chapters = info.get("chapters") or []
    if isinstance(raw_chapters, list):
        for chapter in raw_chapters:
            if isinstance(chapter, Mapping):
                chapters.append(dict(chapter))

    live_status = str(info.get("live_status") or "")
    is_live = bool(info.get("is_live")) or live_status in ("is_live", "is_upcoming", "post_live")

    return VideoMetadata(
        video_id=video_id,
        channel_id=channel_id,
        channel_title=channel_title,
        channel_url=channel_url,
        title=title,
        description=description,
        published_at=_parse_upload_date(info),
        thumbnail_url=thumbnail_url,
        duration=float(info["duration"]) if isinstance(info.get("duration"), (int, float)) else None,
        chapters=tuple(chapters),
        is_live=is_live,
        published_timestamp=_parse_timestamp(info),
        raw=info,
    )


def _pick_avatar(thumbnails: Any) -> str | None:
    if not isinstance(thumbnails, list):
        return None
    candidates = [
        item
        for item in thumbnails
        if isinstance(item, Mapping) and isinstance(item.get("url"), str)
    ]
    preferred = [
        item
        for item in candidates
        if str(item.get("id") or "") == "avatar_uncropped"
        or str(item.get("id") or "").startswith("avatar")
    ]
    pool = preferred or []
    if not pool:
        return None
    best = max(pool, key=lambda item: int(item.get("width") or 0))
    return str(best["url"])


def fetch_channel_avatar_url(
    channel_url: str,
    *,
    ytdlp: str = "yt-dlp",
    runner: Callable[..., Any] = subprocess.run,
    timeout: int = DEFAULT_TIMEOUT,
    extra: Sequence[str] = (),
) -> str | None:
    """Return the channel avatar URL (``avatar_uncropped`` thumbnail)."""
    command = [
        ytdlp,
        "--dump-single-json",
        "--flat-playlist",
        "--playlist-items",
        "0",
        "--no-warnings",
        "--skip-download",
        *extra,
        channel_url,
    ]
    try:
        result = runner(command, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    if getattr(result, "returncode", 1) != 0:
        return None
    try:
        document = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return None
    if not isinstance(document, Mapping):
        return None
    return _pick_avatar(document.get("thumbnails"))


def download_cover(
    url: str,
    destination: str | Path,
    *,
    timeout: float = 60.0,
    opener: Callable[..., Any] = urlopen,
) -> bool:
    """Download ``url`` into ``destination`` atomically; returns success."""
    if not url:
        return False
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".part")
    request = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with opener(request, timeout=timeout) as response:  # noqa: S310 - http(s) only
            data = response.read()
    except (HTTPError, URLError, TimeoutError, OSError):
        return False
    if not data:
        return False
    temporary.write_bytes(data)
    os.replace(temporary, target)
    return True
