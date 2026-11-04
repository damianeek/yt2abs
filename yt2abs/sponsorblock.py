"""SponsorBlock per channel: parsing config values and talking to yt-dlp.

Supported values for a channel / defaults ``sponsorblock`` key:

    off                          nothing is done (default, backwards compatible)
    remove                       yt-dlp cuts the segments out of the file
    mark                        yt-dlp keeps the audio but writes chapters
    mute                        yt2abs mutes the segments locally with ffmpeg
    remove:sponsor,selfpromo     explicit category list
    mute:sponsor,intro           explicit category list for mute
    {mode: remove, categories: [sponsor, selfpromo]}   mapping form
    [sponsor, selfpromo]         list form == remove with those categories

Only ``remove`` and ``mark`` are delegated to yt-dlp (``--sponsorblock-remove``
/ ``--sponsorblock-mark``). yt-dlp has no native "mute" mode, so ``mute`` is
implemented here: segments are fetched from the SponsorBlock API and the file
is re-encoded with an ffmpeg volume filter.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

DEFAULT_API = "https://sponsor.ajay.app"

MODE_OFF = "off"
MODE_REMOVE = "remove"
MODE_MARK = "mark"
MODE_MUTE = "mute"
MODES = (MODE_OFF, MODE_REMOVE, MODE_MARK, MODE_MUTE)

DEFAULT_CATEGORIES = ("sponsor",)

# Mirrors yt_dlp.postprocessor.sponsorblock.SponsorBlockPP.CATEGORIES.
MARK_CATEGORIES = (
    "sponsor",
    "intro",
    "outro",
    "selfpromo",
    "preview",
    "filler",
    "interaction",
    "music_offtopic",
    "hook",
    "poi_highlight",
    "chapter",
)
NON_SKIPPABLE_CATEGORIES = ("poi_highlight", "chapter")
REMOVE_CATEGORIES = tuple(c for c in MARK_CATEGORIES if c not in NON_SKIPPABLE_CATEGORIES)

_OFF_TOKENS = {"", "off", "false", "none", "no", "0"}


class SponsorBlockConfigError(ValueError):
    """Raised for malformed sponsorblock settings."""


@dataclass(frozen=True)
class Setting:
    """Normalised sponsorblock setting for one channel."""

    mode: str = MODE_OFF
    categories: tuple[str, ...] = ()

    @property
    def enabled(self) -> bool:
        return self.mode != MODE_OFF

    def as_dict(self) -> dict:
        return {"mode": self.mode, "categories": list(self.categories)}


def _allowed_categories(mode: str) -> tuple[str, ...]:
    if mode == MODE_MARK:
        return MARK_CATEGORIES
    return REMOVE_CATEGORIES


def _expand_categories(tokens: Iterable[str], mode: str) -> tuple[str, ...]:
    """Expand ``all`` / ``default`` / ``-cat`` tokens into a concrete list."""
    allowed = _allowed_categories(mode)
    selected: set[str] = set()
    explicit: list[str] = []
    for raw in tokens:
        token = str(raw).strip()
        if not token:
            continue
        if token == "all":
            selected |= set(allowed)
            continue
        if token == "default":
            if mode == MODE_REMOVE:
                selected |= set(REMOVE_CATEGORIES) - {"filler"}
            else:
                selected |= set(allowed)
            continue
        if token.startswith("-"):
            name = token[1:]
            if name not in allowed:
                raise SponsorBlockConfigError(
                    f"unknown SponsorBlock category {name!r}; valid categories for {mode}: {', '.join(allowed)}"
                )
            selected.discard(name)
            continue
        if token not in allowed:
            raise SponsorBlockConfigError(
                f"unknown SponsorBlock category {token!r}; valid categories for {mode}: {', '.join(allowed)}"
            )
        explicit.append(token)
    for name in explicit:
        selected.add(name)
    if not selected:
        raise SponsorBlockConfigError("SponsorBlock category list is empty after expansion")
    return tuple(c for c in allowed if c in selected)


def _parse_categories(value: object, mode: str) -> tuple[str, ...]:
    if value is None:
        return DEFAULT_CATEGORIES
    if isinstance(value, str):
        tokens = [t for t in (part.strip() for part in value.split(",")) if t]
    elif isinstance(value, (list, tuple, set)):
        tokens = [str(t) for t in value]
    else:
        raise SponsorBlockConfigError(f"sponsorblock categories must be a string or a list, got {type(value).__name__}")
    return _expand_categories(tokens, mode)


def parse_setting(value: object) -> Setting:
    """Normalise any accepted ``sponsorblock`` value into a :class:`Setting`."""
    if value is None or value is False:
        return Setting(MODE_OFF, ())
    if value is True:
        return Setting(MODE_REMOVE, DEFAULT_CATEGORIES)

    if isinstance(value, str):
        text = value.strip()
        if text.lower() in _OFF_TOKENS:
            return Setting(MODE_OFF, ())
        mode_text, _, cats_text = text.partition(":")
        mode = mode_text.strip().lower()
        if mode in MODES:
            if mode == MODE_OFF:
                return Setting(MODE_OFF, ())
            return Setting(mode, _parse_categories(cats_text or None, mode))
        # A bare category list is an implicit "remove".
        return Setting(MODE_REMOVE, _parse_categories(text, MODE_REMOVE))

    if isinstance(value, Mapping):
        extra = set(value) - {"mode", "categories"}
        if extra:
            raise SponsorBlockConfigError(f"unknown sponsorblock keys: {', '.join(sorted(map(str, extra)))}")
        mode = str(value.get("mode", MODE_REMOVE)).strip().lower()
        if mode not in MODES:
            raise SponsorBlockConfigError(f"unknown sponsorblock mode {mode!r}; valid modes: {', '.join(MODES)}")
        if mode == MODE_OFF:
            return Setting(MODE_OFF, ())
        return Setting(mode, _parse_categories(value.get("categories"), mode))

    if isinstance(value, (list, tuple, set)):
        return Setting(MODE_REMOVE, _parse_categories(list(value), MODE_REMOVE))

    raise SponsorBlockConfigError(f"sponsorblock must be a string, list or mapping, got {type(value).__name__}")


def format_setting(setting: Setting) -> str:
    """Render a setting back into the compact config.yaml/WebUI string form."""
    if setting.mode == MODE_OFF:
        return "off"
    return f"{setting.mode}:{','.join(setting.categories)}"


def ytdlp_args(setting: Setting, *, api: str | None = None) -> list[str]:
    """yt-dlp arguments for a setting (empty for ``off`` and ``mute``)."""
    if setting.mode == MODE_OFF:
        return []
    if setting.mode == MODE_REMOVE:
        flag = "--sponsorblock-remove"
    elif setting.mode == MODE_MARK:
        flag = "--sponsorblock-mark"
    else:  # MODE_MUTE is handled locally with ffmpeg
        return []
    args = [flag, ",".join(setting.categories)]
    if api and api.rstrip("/") != DEFAULT_API:
        args += ["--sponsorblock-api", api.rstrip("/")]
    return args


# --------------------------------------------------------------------------
# SponsorBlock API (used by "mute")
# --------------------------------------------------------------------------


def video_hash(video_id: str) -> str:
    """First 4 chars of the SHA-256 of a video id (as recommended by the API)."""
    return hashlib.sha256(video_id.encode("ascii")).hexdigest()[:4]


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    category: str
    uuid: str = ""

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


def _http_json(url: str, timeout: int):
    request = urllib.request.Request(url, headers={"User-Agent": "yt2abs/0.3"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 (fixed https endpoint)
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:  # API returns 404 when nothing was found for the hash bucket
            return None
        raise


def fetch_segments(
    video_id: str,
    categories: Sequence[str] = DEFAULT_CATEGORIES,
    *,
    api: str = DEFAULT_API,
    timeout: int = 20,
    video_duration: float | None = None,
    json_fetch: Callable[[str, int], object] | None = None,
) -> list[Segment]:
    """Fetch skippable SponsorBlock segments for one video."""
    query = urllib.parse.urlencode(
        {
            "service": "YouTube",
            "categories": json.dumps(list(categories)),
            "actionTypes": json.dumps(["skip"]),
        }
    )
    url = f"{api.rstrip('/')}/api/skipSegments/{video_hash(video_id)}?{query}"
    payload = (json_fetch or _http_json)(url, timeout)
    if not isinstance(payload, list):
        return []

    def normalise(entry: Mapping, *, expected_video: str | None) -> Segment | None:
        category = str(entry.get("category", ""))
        if categories and category not in categories:
            return None
        if entry.get("actionType", "skip") != "skip":
            return None
        span = entry.get("segment") or []
        if len(span) != 2:
            return None
        start, end = float(span[0]), float(span[1])
        if end <= start:
            return None
        if video_duration:
            reported = float(entry.get("videoDuration") or 0)
            if reported and abs(reported - video_duration) > 1:
                return None
        return Segment(start=start, end=end, category=category, uuid=str(entry.get("UUID", "")))

    segments: list[Segment] = []
    for entry in payload:
        if not isinstance(entry, Mapping):
            continue
        if entry.get("videoID") not in (None, video_id):
            continue
        # The hashed endpoint groups segments per video ({"videoID": ..., "segments": [...]});
        # the ?videoID= endpoint returns a flat list of segments.
        nested = entry.get("segments")
        if isinstance(nested, list):
            for child in nested:
                if isinstance(child, Mapping):
                    segment = normalise(child, expected_video=video_id)
                    if segment is not None:
                        segments.append(segment)
            continue
        segment = normalise(entry, expected_video=video_id)
        if segment is not None:
            segments.append(segment)
    segments.sort(key=lambda s: s.start)
    return segments


def _merge_spans(segments: Sequence[Segment]) -> list[tuple[float, float]]:
    spans: list[list[float]] = []
    for segment in sorted(segments, key=lambda s: s.start):
        if spans and segment.start <= spans[-1][1]:
            spans[-1][1] = max(spans[-1][1], segment.end)
        else:
            spans.append([segment.start, segment.end])
    return [(start, end) for start, end in spans]


def build_mute_filter(segments: Sequence[Segment]) -> str | None:
    """ffmpeg ``-af`` expression muting all given segments, or ``None``."""
    spans = _merge_spans(segments)
    if not spans:
        return None
    condition = "+".join(f"between(t,{start:.3f},{end:.3f})" for start, end in spans)
    return f"volume=enable='{condition}':volume=0"


def build_mute_command(
    source: str | Path, target: str | Path, audio_filter: str, *, ffmpeg: str = "ffmpeg"
) -> list[str]:
    """Build the ffmpeg command that mutes ``segments`` in place.

    The mapping is explicit: only the first audio stream is decoded and
    re-encoded, while an embedded cover (an ``attached_pic`` video stream that
    yt-dlp's ``--embed-thumbnail`` writes into the m4a) is stream-copied.  A
    bare ``ffmpeg -i in.m4a -c:a aac out.m4a`` would pick the cover up through
    implicit mapping and re-encode it as H.264, which the ``ipod``/mp4 muxer
    rejects (``Could not find tag for codec h264``).  The ``?`` suffix keeps
    audio-only files (``bestaudio`` fallback) working.
    """
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-map",
        "0:a:0",
        "-af",
        audio_filter,
        "-c:a",
        "aac",
        "-q:a",
        "2",
        "-map",
        "0:v?",
        "-c:v",
        "copy",
        str(target),
    ]


def mute_segments(
    path: str | Path,
    segments: Sequence[Segment],
    *,
    ffmpeg: str = "ffmpeg",
    runner: Callable = subprocess.run,
    timeout: int = 1800,
) -> bool:
    """Mute the given segments inside an existing audio file (re-encodes audio).

    The cover stream is copied untouched, so a file produced with
    ``--embed-thumbnail`` keeps its artwork and stays valid.
    """
    audio_filter = build_mute_filter(segments)
    if audio_filter is None:
        return False
    source = Path(path)
    target = source.with_name(f"{source.stem}.muted{source.suffix}")
    command = build_mute_command(source, target, audio_filter, ffmpeg=ffmpeg)
    try:
        runner(command, check=True, timeout=timeout)
        if not target.exists() or target.stat().st_size == 0:
            raise RuntimeError(f"ffmpeg produced no output for {source.name}")
        os.replace(target, source)
    except BaseException:
        # Never leave a half-written .muted.m4a behind: the next attempt would
        # find it in tmp_dir and the file would be counted as a duplicate.
        target.unlink(missing_ok=True)
        raise
    return True
