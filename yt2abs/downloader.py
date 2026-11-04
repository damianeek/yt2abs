"""yt-dlp integration: audio download with the channel's SponsorBlock policy."""
from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from .config import Config
from .sponsorblock import (
    DEFAULT_API,
    MODE_MUTE,
    Setting,
    fetch_segments,
    mute_segments,
    ytdlp_args,
)

DEFAULT_OUTPUT_TEMPLATE = "%(upload_date>%Y-%m-%d)s - %(title).200B [%(id)s].%(ext)s"
AUDIO_FORMAT = "bestaudio[ext=m4a]"
VIDEO_ID_RE = re.compile(r"\[(?P<id>[A-Za-z0-9_-]{11})\]")


class DownloadError(RuntimeError):
    """Raised when yt-dlp exits with a non-zero status."""


@dataclass
class DownloadResult:
    url: str
    path: str | None
    video_id: str | None
    channel_id: str | None
    setting: Setting
    muted: bool = False
    segments: int = 0
    ytdlp_args: list[str] = field(default_factory=list)

    @property
    def mode(self) -> str:
        return self.setting.mode

    def as_dict(self) -> dict:
        return {
            "url": self.url,
            "path": self.path,
            "video_id": self.video_id,
            "channel_id": self.channel_id,
            "sponsorblock": self.setting.as_dict(),
            "muted": self.muted,
            "segments": self.segments,
            "ytdlp_args": list(self.ytdlp_args),
        }


def build_audio_args(
    *,
    output_template: str = DEFAULT_OUTPUT_TEMPLATE,
    setting: Setting | None = None,
    api: str | None = None,
    extra: Sequence[str] = (),
) -> list[str]:
    """yt-dlp arguments for audio-only download honouring the sponsorblock setting."""
    setting = setting or Setting()
    args = ["--format", AUDIO_FORMAT, "--output", output_template]
    args += ytdlp_args(setting, api=api)
    args += list(extra)
    return args


def probe_metadata(
    url: str, *, ytdlp: str = "yt-dlp", runner: Callable = subprocess.run, timeout: int = 120
) -> dict[str, str | None]:
    """Best-effort ``channel_id``/``id`` lookup (one cheap metadata request)."""
    command = [ytdlp, "--simulate", "--no-warnings", "--quiet", "--print", "%(channel_id)s|%(id)s", url]
    result = runner(command, capture_output=True, text=True, timeout=timeout)
    if getattr(result, "returncode", 1) != 0:
        return {"channel_id": None, "video_id": None}
    for line in reversed((result.stdout or "").splitlines()):
        line = line.strip()
        if not line:
            continue
        channel_id, _, video_id = line.partition("|")
        return {
            "channel_id": channel_id.strip() or None,
            "video_id": video_id.strip() or None,
        }
    return {"channel_id": None, "video_id": None}


def download(
    video_url: str,
    dest_dir: str | Path,
    *,
    setting: Setting | None = None,
    api: str | None = None,
    ytdlp: str = "yt-dlp",
    runner: Callable = subprocess.run,
    timeout: int = 3600,
    extra: Sequence[str] = (),
    video_id: str | None = None,
    channel_id: str | None = None,
    log: Callable[[str], None] = print,
) -> DownloadResult:
    """Download audio for ``video_url`` into ``dest_dir`` and post-process it.

    ``remove``/``mark`` are handled by yt-dlp itself; ``mute`` is applied by
    :func:`yt2abs.sponsorblock.mute_segments` after a successful download.
    """
    setting = setting or Setting()
    destination = Path(dest_dir)
    destination.mkdir(parents=True, exist_ok=True)
    output_template = str(destination / DEFAULT_OUTPUT_TEMPLATE)

    args = build_audio_args(output_template=output_template, setting=setting, api=api, extra=extra)
    command = [
        ytdlp,
        "--no-warnings",
        "--quiet",
        "--print",
        "after_move:filepath",
        *args,
        video_url,
    ]
    log("yt-dlp: " + " ".join(command))
    result = runner(command, capture_output=True, text=True, timeout=timeout)
    if getattr(result, "returncode", 1) != 0:
        stderr_lines = [line.strip() for line in (getattr(result, "stderr", "") or "").splitlines() if line.strip()]
        errors = [line for line in stderr_lines if line.startswith("ERROR:")]
        detail = (errors[0] if errors else stderr_lines[-1]) if stderr_lines else "unknown error"
        raise DownloadError(f"yt-dlp failed for {video_url}: {detail}")

    path: Path | None = None
    for line in reversed((result.stdout or "").splitlines()):
        line = line.strip()
        if line:
            path = Path(line)
            break

    if video_id is None and path is not None:
        match = VIDEO_ID_RE.search(path.name)
        video_id = match.group("id") if match else None

    outcome = DownloadResult(
        url=video_url,
        path=str(path) if path else None,
        video_id=video_id,
        channel_id=channel_id,
        setting=setting,
        ytdlp_args=list(args),
    )

    if setting.mode == MODE_MUTE and path is not None and path.exists():
        segments = fetch_segments(
            video_id or "",
            setting.categories,
            api=api or DEFAULT_API,
            video_duration=_probe_duration(path, runner=runner),
        )
        outcome.segments = len(segments)
        if segments:
            outcome.muted = mute_segments(path, segments, runner=runner, timeout=timeout)
            log(f"mute: wyciszono {len(segments)} segment(ow) w {path.name}")
        else:
            log("mute: no SponsorBlock segments for this video")
    return outcome


def _probe_duration(path: Path, *, ffprobe: str = "ffprobe", runner: Callable = subprocess.run) -> float | None:
    command = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    try:
        result = runner(command, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - environment dependent
        return None
    if getattr(result, "returncode", 1) != 0:
        return None
    try:
        return float((result.stdout or "").strip())
    except ValueError:
        return None


def download_for_channel(
    video_url: str,
    dest_dir: str | Path,
    config: Config,
    *,
    channel_id: str | None = None,
    video_id: str | None = None,
    extra: Sequence[str] = (),
    **kwargs,
) -> DownloadResult:
    """Resolve the channel policy from ``config`` and download accordingly."""
    ytdlp = str(kwargs.get("ytdlp") or config.ytdlp_command or "yt-dlp")
    kwargs["ytdlp"] = ytdlp
    resolved_channel = (channel_id or "").strip() or None
    if resolved_channel is None:
        meta = probe_metadata(
            video_url,
            ytdlp=ytdlp,
            runner=kwargs.get("runner", subprocess.run),
        )
        resolved_channel = meta.get("channel_id")
        video_id = video_id or meta.get("video_id")

    channel = config.channel(resolved_channel)
    return download(
        video_url,
        dest_dir,
        setting=channel.sponsorblock,
        api=config.defaults.sponsorblock_api,
        channel_id=resolved_channel,
        video_id=video_id,
        extra=list(extra) + list(config.defaults.ytdlp_args),
        **kwargs,
    )
