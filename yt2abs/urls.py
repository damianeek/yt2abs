"""YouTube URL normalisation.

Only public YouTube video URLs are accepted: ``watch``, ``youtu.be``,
``shorts``, ``embed``, ``live`` and ``v`` paths. Extra query parameters
(``t``, ``list``, ``si``, ...) are ignored; the video id is the only thing
that matters.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
#: Playlist ids seen in the wild: ``PL...`` (user playlists), ``UU...`` (uploads),
#: ``OL...``/``LL...`` (albums/likes), ``RD...`` (mixes).  Kept permissive but
#: bounded so a pasted sentence never reaches yt-dlp as a playlist link.
PLAYLIST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,128}$")

YOUTUBE_HOSTS = frozenset(
    {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtube-nocookie.com",
        "www.youtube-nocookie.com",
        "youtu.be",
        "www.youtu.be",
    }
)
SHORT_HOSTS = frozenset({"youtu.be", "www.youtu.be"})
PATH_PREFIXES = frozenset({"shorts", "embed", "live", "v"})


class InvalidYouTubeURL(ValueError):
    """Raised when a URL is not a supported YouTube video URL."""


class InvalidPlaylistURL(ValueError):
    """Raised when a URL is not a YouTube playlist link (``/playlist?list=``)."""


#: Hosts serving the classic playlist page (``youtu.be`` is video-only).
PLAYLIST_HOSTS = frozenset(
    {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
    }
)


def extract_video_id(url: str) -> str:
    """Return the 11-character video id or raise :class:`InvalidYouTubeURL`."""
    if not isinstance(url, str) or not url.strip():
        raise InvalidYouTubeURL("Provide a YouTube video link.")

    raw = url.strip()
    if "://" not in raw:
        raw = "https://" + raw

    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https"):
        raise InvalidYouTubeURL("Only http/https YouTube links are supported.")

    host = (parsed.hostname or "").lower().strip(".")
    if host not in YOUTUBE_HOSTS:
        raise InvalidYouTubeURL("This link does not point to YouTube.")

    candidate: str | None
    if host in SHORT_HOSTS:
        segment = parsed.path.lstrip("/").split("/", 1)[0]
        candidate = segment or None
    else:
        parts = [part for part in parsed.path.split("/") if part]
        if not parts:
            raise InvalidYouTubeURL("The link does not contain a video id.")
        head = parts[0].lower()
        if head == "watch":
            values = parse_qs(parsed.query).get("v") or []
            candidate = values[0] if values else None
        elif head in PATH_PREFIXES and len(parts) > 1:
            candidate = parts[1]
        else:
            raise InvalidYouTubeURL("Unrecognised YouTube link format.")

    if not candidate or not VIDEO_ID_RE.match(candidate):
        raise InvalidYouTubeURL("The link does not contain a valid YouTube video id.")
    return candidate


def canonical_url(video_id: str) -> str:
    """Canonical watch URL for a video id."""
    if not VIDEO_ID_RE.match(video_id or ""):
        raise InvalidYouTubeURL(f"Invalid video id: {video_id!r}")
    return f"https://www.youtube.com/watch?v={video_id}"


def extract_playlist_id(url: str) -> str:
    """Return the playlist id from a YouTube ``/playlist?list=`` URL.

    Only the playlist page is accepted: a watch URL that merely carries a
    ``list=`` parameter (``youtube.com/watch?v=...&list=...``) points at one
    video, and polling it would silently queue the whole playlist.  Playlist
    privacy is not checked here - yt-dlp (without cookies) only sees public and
    unlisted playlists.
    """
    if not isinstance(url, str) or not url.strip():
        raise InvalidPlaylistURL("Provide a YouTube playlist link.")

    raw = url.strip()
    if "://" not in raw:
        raw = "https://" + raw

    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https"):
        raise InvalidPlaylistURL("Only http/https YouTube links are supported.")

    host = (parsed.hostname or "").lower().strip(".")
    if host not in PLAYLIST_HOSTS:
        raise InvalidPlaylistURL("This link does not point to a YouTube playlist.")

    if parsed.path.rstrip("/") != "/playlist":
        raise InvalidPlaylistURL(
            "The link must point to the playlist page (youtube.com/playlist?list=...)."
        )

    values = parse_qs(parsed.query).get("list") or []
    candidate = (values[0] or "").strip() if values else ""
    if not candidate or not PLAYLIST_ID_RE.match(candidate):
        raise InvalidPlaylistURL("The link does not contain a valid playlist id.")
    return candidate
