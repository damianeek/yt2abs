"""Import pipeline: metadata -> audio -> atomic move -> Audiobookshelf import.

The pipeline is idempotent on purpose: a file that is already published to the
library (``videos.file_path`` or any ``*[<video_id>].*`` file in the channel
folder) is reused instead of being downloaded again.  That keeps a retry after
a failed ABS import cheap and prevents a second episode (a changed YouTube
title would otherwise produce a new file name).
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from typing import Any, Callable, Mapping

from .abs_client import AbsClient, AbsError, AbsNotFound
from .config import Config
from .db import Database
from .downloader import DownloadError
from .metadata import (
    VideoMetadata,
    download_cover,
    fetch_channel_avatar_url,
    fetch_metadata,
)
from .urls import canonical_url

logger = logging.getLogger(__name__)

EMBED_ARGS = ("--embed-metadata", "--embed-thumbnail", "--embed-chapters")
FALLBACK_ARGS = ("--embed-metadata", "--format", "bestaudio")
COVER_FILENAME = "cover.jpg"
_INVALID_FOLDER_CHARS = set('<>:"\\|?*')
_FOLDER_SLASH = "\u29f8"  # U+29F8 BIG SOLIDUS, same glyph yt-dlp uses in file names



class PipelineError(RuntimeError):
    """A job could not be completed."""


class AbsImportError(PipelineError):
    """Audiobookshelf did not index the new episode."""


@dataclass(frozen=True)
class ImportOutcome:
    video_id: str
    file_path: str
    abs_item_id: str
    abs_episode_id: str
    channel_id: str
    channel_title: str
    title: str
    description: str
    published_at: str | None

    def as_fields(self) -> dict[str, Any]:
        return {
            "file_path": self.file_path,
            "abs_item_id": self.abs_item_id,
            "abs_episode_id": self.abs_episode_id,
            "channel_id": self.channel_id,
            "channel_title": self.channel_title,
            "title": self.title,
            "description": self.description,
            "published_at": self.published_at,
        }


def sanitize_folder_name(name: str, *, fallback: str = "channel") -> str:
    """Make a channel title safe as a single directory name."""
    cleaned = "".join(
        _FOLDER_SLASH if char == "/" else (" " if char in _INVALID_FOLDER_CHARS else char)
        for char in name
    )
    cleaned = "".join(char for char in cleaned if ord(char) >= 32)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    encoded = cleaned.encode("utf-8")
    if len(encoded) > 180:  # stay well below the usual 255-byte NAME_MAX
        cleaned = encoded[:180].decode("utf-8", errors="ignore").rstrip(" .")
    return cleaned or fallback


def _episode_description(metadata: VideoMetadata) -> str:
    source = f"Source: {canonical_url(metadata.video_id)}"
    description = (metadata.description or "").strip()
    if not description:
        return source
    return f"{description}\n\n{source}"


def episode_dates(metadata: VideoMetadata) -> tuple[str | None, int | None]:
    """Return ``(pubDate in RFC-822, publishedAt in epoch ms)`` for ABS.

    ABS keeps the two fields independent: it sorts and displays episodes by
    ``publishedAt`` (epoch ms) while ``pubDate`` is only a string used in the
    RSS feed.  Both are derived from yt-dlp's upload timestamp; when only a
    date is known, midnight UTC is used.
    """
    moment: datetime | None = None
    if metadata.published_timestamp:
        moment = datetime.fromtimestamp(metadata.published_timestamp, tz=timezone.utc)
    elif metadata.published_at:
        try:
            moment = datetime.fromisoformat(metadata.published_at).replace(tzinfo=timezone.utc)
        except ValueError:
            moment = None
    if moment is None:
        return None, None
    return format_datetime(moment), int(moment.timestamp() * 1000)


class ImportPipeline:
    def __init__(
        self,
        config: Config,
        db: Database,
        abs_client: AbsClient,
        *,
        metadata_fetcher: Callable[..., VideoMetadata] = fetch_metadata,
        avatar_fetcher: Callable[..., str | None] = fetch_channel_avatar_url,
        cover_downloader: Callable[[str, str | Path], bool] = download_cover,
        downloader: Callable[..., Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if downloader is None:
            from .downloader import download_for_channel

            downloader = download_for_channel
        self.config = config
        self.db = db
        self.abs = abs_client
        self.metadata_fetcher = metadata_fetcher
        self.avatar_fetcher = avatar_fetcher
        self.cover_downloader = cover_downloader
        self.downloader = downloader
        self.sleep = sleep
        self.monotonic = monotonic

    # ------------------------------------------------------------------ public
    def process(
        self, job: Mapping[str, Any], report: Callable[[str], None] | None = None
    ) -> ImportOutcome:
        notify = report or (lambda stage: None)
        video_id = str(job["video_id"])
        url = canonical_url(video_id)

        if not self.config.library_path:
            raise PipelineError("Library path is not configured (paths.library_path / abs.library_path).")
        if not self.config.abs.configured:
            raise PipelineError(
                "Audiobookshelf is not configured (abs.url, ABS_API_KEY, abs.library_id, abs.library_path)."
            )

        notify("metadata")
        metadata = self.metadata_fetcher(
            url, extra=self.config.defaults.ytdlp_args, ytdlp=self.config.ytdlp_command
        )
        if metadata.video_id != video_id:
            raise PipelineError(
                f"yt-dlp returned a different video ({metadata.video_id}) than the marked one ({video_id})."
            )
        if metadata.is_live:
            raise PipelineError("Live streams are not supported.")

        channel = self.config.channel(metadata.channel_id)
        folder_name = self._resolve_channel_folder(metadata, channel.name)
        self.db.update_video(
            video_id,
            channel_id=metadata.channel_id,
            channel_title=metadata.channel_title,
            title=metadata.title,
            description=metadata.description,
            published_at=metadata.published_at,
            stage="downloading",
        )

        tmp_dir = Path(self.config.paths.tmp_dir)
        destination_dir = Path(self.config.library_path) / folder_name
        try:
            existing = self._find_existing_media(
                destination_dir, video_id, preferred=job.get("file_path")
            )
            if existing is not None:
                # A previous attempt already published the audio (ABS import or
                # metadata edit failed afterwards). Re-downloading would waste
                # time and, when the YouTube title changed, create a duplicate.
                logger.info(
                    "%s: reusing the file already in the library (%s)", video_id, existing
                )
                self.db.update_video(video_id, file_path=str(existing), stage="importing")
                destination = existing
                # A retry can find two files when the title changed between
                # attempts: keep the one we are about to import, drop the rest.
                self._remove_duplicate_media(destination_dir, video_id, keep=destination)
            else:
                destination = self._download_and_publish(
                    url, video_id, metadata, destination_dir, tmp_dir, notify
                )

            notify("importing")
            item, episode = self._wait_for_abs(folder_name, destination.name)
            episode_id = str(episode.get("id") or "")
            item_id = str(item.get("id") or "")
            if not episode_id or not item_id:
                raise AbsImportError("ABS returned an episode or podcast without an id.")

            pub_date, published_at_ms = episode_dates(metadata)
            self.abs.update_episode(
                item_id,
                episode_id,
                title=metadata.title,
                description=_episode_description(metadata),
                pub_date=pub_date,
                published_at_ms=published_at_ms,
            )
            try:
                self.abs.edit_podcast(
                    item_id,
                    {"title": metadata.channel_title, "author": metadata.channel_title},
                )
            except AbsError as exc:  # podcast metadata is cosmetic, the folder name is authoritative
                logger.warning("ABS podcast metadata update failed: %s", exc)

            return ImportOutcome(
                video_id=video_id,
                file_path=str(destination),
                abs_item_id=item_id,
                abs_episode_id=episode_id,
                channel_id=metadata.channel_id,
                channel_title=metadata.channel_title,
                title=metadata.title,
                description=metadata.description,
                published_at=metadata.published_at,
            )
        finally:
            self._cleanup_tmp(tmp_dir, video_id)

    # ---------------------------------------------------------------- internals
    def discard(self, fields: Mapping[str, Any]) -> None:
        """Remove an import that finished after the WebUI deleted the job."""
        item_id = str(fields.get("abs_item_id") or "")
        episode_id = str(fields.get("abs_episode_id") or "")
        if item_id and episode_id:
            try:
                self.abs.delete_episode(item_id, episode_id, hard=True)
                logger.info("discarded ABS episode %s of a deleted job", episode_id)
            except AbsNotFound:
                pass
            except AbsError as exc:
                logger.warning("could not discard ABS episode %s: %s", episode_id, exc)
        path = str(fields.get("file_path") or "")
        if path:
            try:
                Path(path).unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("could not remove %s of a deleted job: %s", path, exc)

    def _download_and_publish(
        self,
        url: str,
        video_id: str,
        metadata: VideoMetadata,
        destination_dir: Path,
        tmp_dir: Path,
        notify: Callable[[str], None],
    ) -> Path:
        """Download the audio and publish it atomically into the library."""
        # Cheap preflight: a broken/unreachable ABS must fail before hours of
        # downloading, not after the file was published.
        try:
            self.abs.get_library(self.config.abs.library_id)
        except AbsError as exc:
            raise PipelineError(
                f"Audiobookshelf is not responding before download: {exc}"
            ) from exc

        self.db.update_video(video_id, file_path=None)
        notify("downloading")
        tmp_dir.mkdir(parents=True, exist_ok=True)
        try:
            download = self.downloader(
                url,
                tmp_dir,
                self.config,
                channel_id=metadata.channel_id,
                video_id=video_id,
                extra=EMBED_ARGS,
            )
        except DownloadError as exc:
            if "Requested format is not available" not in str(exc):
                raise
            logger.warning("m4a audio unavailable, falling back to bestaudio: %s", exc)
            download = self.downloader(
                url,
                tmp_dir,
                self.config,
                channel_id=metadata.channel_id,
                video_id=video_id,
                extra=FALLBACK_ARGS,
            )

        downloaded = getattr(download, "path", None)
        if not downloaded or not Path(downloaded).exists():
            raise PipelineError("yt-dlp did not return an audio file.")

        notify("publishing")
        destination_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_cover(destination_dir, metadata)

        source = Path(downloaded)
        destination = destination_dir / source.name
        if source.suffix.lower() != ".m4a":
            logger.warning(
                "downloaded container is %s (m4a unavailable), keeping it unchanged", source.suffix
            )
        self._atomic_move(source, destination)
        self.db.update_video(video_id, file_path=str(destination), stage="importing")
        self._remove_duplicate_media(destination_dir, video_id, keep=destination)
        return destination

    @staticmethod
    def _find_existing_media(
        directory: Path, video_id: str, *, preferred: str | None = None
    ) -> Path | None:
        """Find an already published file for ``video_id`` in ``directory``.

        yt-dlp names the file after the video title, so a retry after a title
        change would create a second file (and a second ABS episode) for the
        same video. Any ``*[<video_id>].*`` file counts as the published one.
        """
        marker = f"[{video_id}]"
        candidates: list[Path] = []
        seen: set[Path] = set()
        if preferred:
            candidate = Path(preferred)
            if (
                candidate.is_file()
                and marker in candidate.name
                and not candidate.name.startswith(".")
                and candidate.stat().st_size > 0
            ):
                # The DB knows which file was published; trust it over mtime so
                # a stale copy with an older title cannot win the import.
                return candidate
        if directory.is_dir():
            for entry in directory.iterdir():
                if entry in seen or not entry.is_file() or entry.name.startswith("."):
                    continue
                if marker not in entry.name:
                    continue
                try:
                    if entry.stat().st_size == 0:
                        continue
                except OSError:  # pragma: no cover - file vanished mid-scan
                    continue
                candidates.append(entry)
        if not candidates:
            return None
        # Prefer m4a (the format the pipeline asks yt-dlp for), then the newest.
        candidates.sort(
            key=lambda path: (path.suffix.lower() == ".m4a", path.stat().st_mtime),
            reverse=True,
        )
        return candidates[0]

    @staticmethod
    def _remove_duplicate_media(directory: Path, video_id: str, *, keep: Path) -> None:
        """Drop other copies of the same video (old title, ``.webm`` fallback)."""
        marker = f"[{video_id}]"
        if not directory.is_dir():
            return
        for entry in directory.iterdir():
            if entry == keep or not entry.is_file() or marker not in entry.name:
                continue
            try:
                entry.unlink()
                logger.info("removed duplicate file for %s: %s", video_id, entry.name)
            except OSError as exc:  # pragma: no cover - permissions/disk
                logger.warning("could not remove duplicate %s: %s", entry, exc)

    @staticmethod
    def _cleanup_tmp(tmp_dir: Path, video_id: str) -> None:
        """Remove leftovers of this video from ``tmp_dir`` (best effort)."""
        marker = f"[{video_id}]"
        if not tmp_dir.is_dir():
            return
        for entry in tmp_dir.iterdir():
            if not entry.is_file() or marker not in entry.name:
                continue
            try:
                entry.unlink()
            except OSError as exc:  # pragma: no cover - permissions/disk
                logger.warning("could not remove temporary file %s: %s", entry, exc)

    def _resolve_channel_folder(self, metadata: VideoMetadata, config_name: str | None) -> str:
        existing = self.db.get_channel(metadata.channel_id)
        if existing:
            return str(existing["folder_name"])

        title = (config_name or metadata.channel_title or metadata.channel_id).strip()
        base = sanitize_folder_name(title, fallback=metadata.channel_id)
        candidate = base
        suffix = 2
        while True:
            owner = self.db.folder_in_use(candidate)
            if owner is None or owner == metadata.channel_id:
                break
            candidate = f"{base} ({suffix})"
            suffix += 1
        self.db.save_channel(metadata.channel_id, metadata.channel_title, candidate)
        return candidate

    def _ensure_cover(self, destination_dir: Path, metadata: VideoMetadata) -> None:
        cover_path = destination_dir / COVER_FILENAME
        if cover_path.exists():
            return
        avatar_url: str | None = None
        try:
            avatar_url = self.avatar_fetcher(
                metadata.channel_url,
                extra=self.config.defaults.ytdlp_args,
                ytdlp=self.config.ytdlp_command,
            )
        except Exception as exc:  # noqa: BLE001 - cover is best effort
            logger.warning("channel avatar lookup failed: %s", exc)
        for url in (avatar_url, metadata.thumbnail_url):
            if url and self.cover_downloader(url, cover_path):
                self.db.mark_cover_written(metadata.channel_id)
                return
        logger.warning("could not write %s for %s", COVER_FILENAME, metadata.channel_id)

    @staticmethod
    def _atomic_move(source: Path, destination: Path) -> None:
        try:
            os.replace(source, destination)
            return
        except OSError as exc:
            if getattr(exc, "errno", None) != 18:  # EXDEV: different mount points
                raise
            # Docker bind mounts are separate mount points even on the same host
            # filesystem, so rename() cannot cross them. Copy next to the target
            # under a dot-name and rename inside the destination directory: ABS
            # never sees a partial .m4a file.
            logger.warning(
                "cross-device move (%s -> %s): using copy + atomic publish",
                source,
                destination,
            )
        staging = destination.with_name(f".{destination.name}.part")
        try:
            shutil.copyfile(source, staging)
            os.replace(staging, destination)
        except Exception:
            staging.unlink(missing_ok=True)
            raise
        source.unlink(missing_ok=True)

    def _wait_for_abs(self, folder_name: str, filename: str) -> tuple[dict[str, Any], dict[str, Any]]:
        library_id = self.config.abs.library_id
        expected_path = os.path.join(self.config.abs.library_path, folder_name)
        deadline = self.monotonic() + self.config.abs.scan_timeout
        item_id: str | None = None
        scan_requested = False
        last_error: Exception | None = None

        while True:
            try:
                items = self.abs.list_library_items(library_id)
                item = self.abs.find_item_by_path(library_id, expected_path, items=items)
                if item:
                    item_id = str(item.get("id") or "")
                    full_item = self.abs.get_item(item_id)
                    episode = self.abs.find_episode_by_filename(
                        item_id, filename, item=full_item
                    )
                    if episode:
                        return full_item, episode
                    if not scan_requested:
                        # Scan only the item. Asking for a library scan and an
                        # item scan at the same time made ABS 2.37 index the
                        # same file twice (two episodes, one inode), which the
                        # retry-after-publish path then imported as a duplicate.
                        self.abs.scan_item(item_id)
                        scan_requested = True
                elif not scan_requested:
                    # New channel: nothing exists yet, so only a library scan
                    # can discover the folder. Never follow it with an item
                    # scan while that scan may still be running.
                    self.abs.scan_library(library_id)
                    scan_requested = True
            except AbsNotFound as exc:
                last_error = exc
            except AbsError as exc:
                last_error = exc

            if self.monotonic() >= deadline:
                break
            self.sleep(self.config.abs.scan_interval)

        detail = f" Last error: {last_error}" if last_error else ""
        if item_id:
            raise AbsImportError(
                f"ABS did not find episode {filename} in podcast {folder_name} "
                f"within {self.config.abs.scan_timeout:.0f} s.{detail}"
            )
        raise AbsImportError(
            f"ABS did not index podcast {expected_path} within "
            f"{self.config.abs.scan_timeout:.0f} s.{detail}"
        )
