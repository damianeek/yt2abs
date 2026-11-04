"""Per-channel policy overrides edited in the WebUI.

``config.yaml`` is the read-only baseline (the container mounts it read-only),
so the WebUI stores only the fields the operator changed in the SQLite
``settings`` table.  Everything that was not overridden keeps inheriting from
``config.yaml``/``defaults``; a later ``config.yaml`` change therefore still
reaches untouched channels.

The stored document is a mapping ``{channel_id: {field: value}}`` where a
missing key means "inherit".  Two values need explicit handling:

* ``retention_days: null`` means **keep forever** for that channel,
* ``retention_days: 7`` means seven days,
* no ``retention_days`` key means "use config.yaml / defaults".
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import threading
from dataclasses import dataclass, replace
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

from .config import Channel, Config
from .db import Database
from .sponsorblock import (
    Setting,
    SponsorBlockConfigError,
    format_setting,
    parse_setting,
)

logger = logging.getLogger(__name__)

#: ``settings`` key holding the JSON document with all WebUI channel overrides.
OVERRIDES_SETTING = "channels.overrides"

#: YouTube channel ids are ``UC`` + 22 URL-safe characters.  The lower bound is
#: loose on purpose so hand-written test ids keep working.
CHANNEL_ID_PATTERN = re.compile(r"^UC[A-Za-z0-9_-]{10,64}$")

#: Sentinel: leave the stored value untouched.
UNSET: Any = object()
#: Sentinel: drop the stored override so the value inherits from config.yaml.
INHERIT: Any = object()

#: Upper bound for editable retention (100 years); prevents timedelta overflow.
MAX_RETENTION_DAYS = 36500


class ChannelInputError(ValueError):
    """A channel id/link or a channel policy value is invalid."""


@dataclass(frozen=True)
class ChannelOverride:
    """Fields explicitly overridden for one channel (all others inherit)."""

    channel_id: str
    name: str | None = None
    name_set: bool = False
    retention_days: int | None = None
    retention_set: bool = False
    sponsorblock: str | None = None
    sponsorblock_set: bool = False
    protect_unfinished: bool | None = None
    protect_set: bool = False

    @property
    def empty(self) -> bool:
        return not (
            self.name_set
            or self.retention_set
            or self.sponsorblock_set
            or self.protect_set
        )

    def to_mapping(self) -> dict[str, Any]:
        data: dict[str, Any] = {}
        if self.name_set:
            data["name"] = self.name
        if self.retention_set:
            data["retention_days"] = self.retention_days
        if self.sponsorblock_set:
            data["sponsorblock"] = self.sponsorblock
        if self.protect_set:
            data["protect_unfinished"] = self.protect_unfinished
        return data

    @classmethod
    def from_mapping(cls, channel_id: str, raw: Any) -> "ChannelOverride":
        if raw is None:
            raw = {}
        if not isinstance(raw, Mapping):
            raise ChannelInputError(
                f"channels.{channel_id}: override must be a mapping, got {type(raw).__name__}"
            )
        known = {"name", "retention_days", "sponsorblock", "protect_unfinished"}
        unknown = sorted(set(map(str, raw)) - known)
        if unknown:
            logger.warning(
                "channels.%s: ignoring unknown override keys: %s",
                channel_id,
                ", ".join(unknown),
            )
        name = raw.get("name")
        if name is not None:
            if not isinstance(name, str):
                raise ChannelInputError(f"channels.{channel_id}.name must be a string")
            name = name.strip() or None
        retention_set = "retention_days" in raw
        retention = _validate_retention(raw.get("retention_days"), where=f"channels.{channel_id}")
        sponsorblock_set = "sponsorblock" in raw and raw.get("sponsorblock") is not None
        sponsorblock = None
        if sponsorblock_set:
            sponsorblock = _validate_sponsorblock(
                raw.get("sponsorblock"), where=f"channels.{channel_id}"
            )
        protect = raw.get("protect_unfinished")
        protect_set = "protect_unfinished" in raw and protect is not None
        if protect_set and not isinstance(protect, bool):
            raise ChannelInputError(
                f"channels.{channel_id}.protect_unfinished must be a boolean or null"
            )
        return cls(
            channel_id=channel_id,
            name=name,
            name_set="name" in raw,
            retention_days=retention,
            retention_set=retention_set,
            sponsorblock=sponsorblock,
            sponsorblock_set=sponsorblock_set,
            protect_unfinished=protect,
            protect_set=protect_set,
        )


def _validate_retention(value: Any, *, where: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ChannelInputError(f"{where}.retention_days must be an integer or null")
    if value < 0:
        raise ChannelInputError(f"{where}.retention_days must be >= 0")
    if value > MAX_RETENTION_DAYS:
        raise ChannelInputError(
            f"{where}.retention_days must be <= {MAX_RETENTION_DAYS}"
        )
    return value


def _validate_sponsorblock(value: Any, *, where: str) -> str:
    try:
        setting = parse_setting(value)
    except SponsorBlockConfigError as exc:
        raise ChannelInputError(f"{where}.sponsorblock: {exc}") from exc
    return format_setting(setting)


class ChannelPolicyStore:
    """Reads/writes the WebUI channel overrides stored in SQLite."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ loading
    def load(self) -> dict[str, ChannelOverride]:
        raw = self.db.get_setting(OVERRIDES_SETTING)
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("channels: stored overrides are not valid JSON; ignoring them")
            return {}
        if not isinstance(data, Mapping):
            logger.warning("channels: stored overrides are not a mapping; ignoring them")
            return {}
        overrides: dict[str, ChannelOverride] = {}
        for channel_id, entry in data.items():
            key = str(channel_id).strip()
            if not key:
                continue
            try:
                overrides[key] = ChannelOverride.from_mapping(key, entry)
            except ChannelInputError as exc:
                logger.warning("channels: ignoring invalid override for %s: %s", key, exc)
        return overrides

    def _save(self, overrides: Mapping[str, ChannelOverride]) -> None:
        document = {
            channel_id: override.to_mapping()
            for channel_id, override in overrides.items()
        }
        self.db.set_setting(
            OVERRIDES_SETTING,
            json.dumps(document, ensure_ascii=False, sort_keys=True),
        )

    # ------------------------------------------------------------------ mutating
    def upsert(
        self,
        channel_id: str,
        *,
        name: Any = UNSET,
        retention_days: Any = UNSET,
        sponsorblock: Any = UNSET,
        protect_unfinished: Any = UNSET,
    ) -> ChannelOverride:
        with self._lock:
            return self._upsert_locked(
                channel_id,
                name=name,
                retention_days=retention_days,
                sponsorblock=sponsorblock,
                protect_unfinished=protect_unfinished,
            )

    def _upsert_locked(
        self,
        channel_id: str,
        *,
        name: Any = UNSET,
        retention_days: Any = UNSET,
        sponsorblock: Any = UNSET,
        protect_unfinished: Any = UNSET,
    ) -> ChannelOverride:
        """Create or update one override.

        ``UNSET`` keeps the current value, ``INHERIT`` removes the override for
        that field.  ``retention_days=None`` explicitly means "keep forever".
        """
        channel_id = channel_id.strip()
        if not channel_id:
            raise ChannelInputError("channel_id must not be empty")
        overrides = self.load()
        current = overrides.get(channel_id, ChannelOverride(channel_id=channel_id))

        changes: dict[str, Any] = {}
        if name is not UNSET:
            if name is INHERIT or name is None:
                changes.update(name=None, name_set=False)
            else:
                text = str(name).strip()
                changes.update(name=text or None, name_set=bool(text))
        if retention_days is not UNSET:
            if retention_days is INHERIT:
                changes.update(retention_days=None, retention_set=False)
            else:
                changes.update(
                    retention_days=_validate_retention(
                        retention_days, where=f"channels.{channel_id}"
                    ),
                    retention_set=True,
                )
        if sponsorblock is not UNSET:
            if sponsorblock is INHERIT or sponsorblock is None:
                changes.update(sponsorblock=None, sponsorblock_set=False)
            else:
                changes.update(
                    sponsorblock=_validate_sponsorblock(
                        sponsorblock, where=f"channels.{channel_id}"
                    ),
                    sponsorblock_set=True,
                )
        if protect_unfinished is not UNSET:
            if protect_unfinished is INHERIT or protect_unfinished is None:
                changes.update(protect_unfinished=None, protect_set=False)
            else:
                if not isinstance(protect_unfinished, bool):
                    raise ChannelInputError(
                        f"channels.{channel_id}.protect_unfinished must be a boolean or null"
                    )
                changes.update(
                    protect_unfinished=protect_unfinished, protect_set=True
                )

        updated = replace(current, **changes)
        overrides[channel_id] = updated
        self._save(overrides)
        logger.info("channels: saved override for %s: %s", channel_id, updated.to_mapping())
        return updated

    def clear(self, channel_id: str) -> bool:
        with self._lock:
            return self._clear_locked(channel_id)

    def _clear_locked(self, channel_id: str) -> bool:
        overrides = self.load()
        if channel_id not in overrides:
            return False
        del overrides[channel_id]
        self._save(overrides)
        logger.info("channels: removed override for %s", channel_id)
        return True

    # ------------------------------------------------------------------ applying
    def apply(self, config: Config) -> Config:
        """Return a copy of ``config`` with all stored overrides applied."""
        overrides = self.load()
        if not overrides:
            return config
        channels = dict(config.channels)
        for channel_id, override in overrides.items():
            base = channels.get(channel_id)
            if base is None:
                base = Channel(
                    channel_id=channel_id,
                    name=None,
                    retention_days=config.defaults.retention_days,
                    sponsorblock=config.defaults.sponsorblock,
                    protect_unfinished=config.defaults.protect_unfinished,
                    raw={},
                )
            sponsorblock: Setting = base.sponsorblock
            if override.sponsorblock_set:
                sponsorblock = parse_setting(override.sponsorblock or "off")
            raw = dict(base.raw)
            raw.update(override.to_mapping())
            channels[channel_id] = Channel(
                channel_id=channel_id,
                name=override.name if override.name_set else base.name,
                retention_days=(
                    override.retention_days
                    if override.retention_set
                    else base.retention_days
                ),
                sponsorblock=sponsorblock,
                protect_unfinished=(
                    bool(override.protect_unfinished)
                    if override.protect_set
                    else base.protect_unfinished
                ),
                raw=raw,
            )
        return replace(config, channels=channels)


# ---------------------------------------------------------------------------
# Channel references: raw id, /channel/ link, @handle (resolved by yt-dlp)
# ---------------------------------------------------------------------------


def parse_channel_reference(value: str) -> str:
    """Return a channel id for a raw id or a ``youtube.com/channel/<id>`` link."""
    text = (value or "").strip()
    if not text:
        raise ChannelInputError("Provide a channel ID or a YouTube channel link.")
    if CHANNEL_ID_PATTERN.fullmatch(text):
        return text

    candidate = text if "://" in text else "https://" + text
    parsed = urlsplit(candidate)
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host not in ("youtube.com", "m.youtube.com", "youtu.be"):
        raise ChannelInputError(
            "This is not a YouTube link. Provide a channel ID (UC...) or a "
            "https://www.youtube.com/channel/UC... address"
        )
    segments = [segment for segment in parsed.path.split("/") if segment]
    channel_id = ""
    if "channel" in segments:
        index = segments.index("channel")
        if index + 1 < len(segments):
            channel_id = segments[index + 1]
    if not channel_id or not CHANNEL_ID_PATTERN.fullmatch(channel_id):
        raise ChannelInputError(
            "Cannot recognise a channel ID in this link. Use a "
            "https://www.youtube.com/channel/UC... address or paste the ID itself."
        )
    return channel_id


def looks_like_youtube_url(value: str) -> bool:
    text = (value or "").strip()
    candidate = text if "://" in text else "https://" + text
    host = (urlsplit(candidate).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host in ("youtube.com", "m.youtube.com", "youtu.be")


def resolve_channel_via_ytdlp(
    reference: str,
    *,
    command: str = "yt-dlp",
    timeout: float = 60.0,
    extra_args: Sequence[str] = (),
) -> str:
    """Resolve an @handle/custom/video YouTube URL to a channel id using yt-dlp."""
    if not looks_like_youtube_url(reference):
        raise ChannelInputError(f"Invalid YouTube link: {reference!r}")
    url = reference.strip()
    if "://" not in url:
        url = "https://" + url
    arguments = [
        command,
        *extra_args,
        "--skip-download",
        "--playlist-items",
        "1",
        "--print",
        "channel_id",
        "--",
        url,
    ]
    try:
        completed = subprocess.run(
            arguments,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ChannelInputError(f"Could not start yt-dlp: {exc}") from exc
    output = (completed.stdout or "").strip().splitlines()
    for line in output:
        candidate = line.strip()
        if CHANNEL_ID_PATTERN.fullmatch(candidate):
            return candidate
    detail = (completed.stderr or "").strip().splitlines()
    hint = detail[-1] if detail else f"kod {completed.returncode}"
    raise ChannelInputError(
        "Could not resolve a channel from this link. Provide a channel ID (UC...) "
        f"or a .../channel/UC... address. yt-dlp: {hint}"
    )


def resolve_channel_reference(
    reference: str,
    *,
    command: str = "yt-dlp",
    timeout: float = 60.0,
    extra_args: Sequence[str] = (),
) -> str:
    """Parse an explicit reference first, then fall back to a yt-dlp lookup."""
    try:
        return parse_channel_reference(reference)
    except ChannelInputError as exc:
        if not looks_like_youtube_url(reference):
            raise
        try:
            return resolve_channel_via_ytdlp(
                reference, command=command, timeout=timeout, extra_args=extra_args
            )
        except ChannelInputError as lookup_error:
            raise ChannelInputError(f"{exc} {lookup_error}") from lookup_error


def channel_payload(
    config: Config,
    channel_id: str,
    *,
    override: ChannelOverride | None = None,
    in_config: bool = False,
    db_row: Mapping[str, Any] | None = None,
    base_config: Config | None = None,
) -> dict[str, Any]:
    """Serialise one channel with its effective policy and the source of each value."""
    channel = config.channels.get(channel_id)
    if channel is None:
        channel = config.channel(channel_id)
    inherited = (base_config or config).channel(channel_id)
    db_name = str((db_row or {}).get("title") or "").strip()
    name = (channel.name or "").strip() or db_name or channel_id

    def source(flag: bool) -> str:
        if flag:
            return "ui"
        return "config" if in_config else "defaults"

    return {
        "channel_id": channel_id,
        "name": name,
        "retention_days": channel.retention_days,
        "retention_source": source(bool(override and override.retention_set)),
        "inherited_retention_days": inherited.retention_days,
        "sponsorblock": format_setting(channel.sponsorblock),
        "sponsorblock_mode": channel.sponsorblock.mode,
        "sponsorblock_categories": list(channel.sponsorblock.categories),
        "sponsorblock_source": source(bool(override and override.sponsorblock_set)),
        "inherited_sponsorblock": format_setting(inherited.sponsorblock),
        "inherited_sponsorblock_mode": inherited.sponsorblock.mode,
        "inherited_sponsorblock_categories": list(inherited.sponsorblock.categories),
        "protect_unfinished": channel.protect_unfinished,
        "protect_unfinished_source": source(bool(override and override.protect_set)),
        "inherited_protect_unfinished": inherited.protect_unfinished,
        "in_config": in_config,
        "has_override": override is not None,
        "source": "ui" if override is not None else ("config" if in_config else "db"),
        "folder_name": (db_row or {}).get("folder_name"),
    }
