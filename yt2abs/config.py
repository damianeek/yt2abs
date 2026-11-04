"""config.yaml loading and per-channel policy resolution.

Schema (only the keys used by this module are listed; unknown keys are kept
untouched so other parts of the service, e.g. the ``abs`` and ``worker``
sections, can grow):

    defaults:
      retention_days: 14          # null = keep forever
      protect_unfinished: true
      sponsorblock: off           # off | remove | mark | mute | <mode>:<cats>
      sponsorblock_api: https://sponsor.ajay.app
    channels:
      UCzz4CoEgSgWNs9ZAvRMhW2A:
        name: Example channel
        retention_days: 7         # overrides defaults, null = keep
        sponsorblock: remove:sponsor,selfpromo
    playlist:
      enabled: false              # poll an unlisted playlist ("add from phone")
      url: https://www.youtube.com/playlist?list=PL...
      interval_minutes: 15        # >= 5; the runtime kill-switch is env-only

``channels`` may also be a list of mappings with a ``channel_id`` key.
PyYAML is used when importable; a JSON document is accepted as a fallback
(JSON is valid YAML) so the module stays usable in a bare stdlib environment.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import math
import os
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse

from .sponsorblock import DEFAULT_API, Setting, SponsorBlockConfigError, parse_setting
from .urls import InvalidPlaylistURL, extract_playlist_id

try:  # pragma: no cover - depends on the interpreter
    import yaml  # type: ignore
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore

DEFAULT_CONFIG_FILENAMES = ("config.yaml", "config.yml")
DEFAULT_RETENTION_DAYS = 14
DEFAULT_PROTECT_UNFINISHED = True
DEFAULT_ABS_URL = "http://localhost:13378"
DEFAULT_DATA_DIR = "data"
DEFAULT_TMP_DIR = "data/tmp"
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_RETRY_BACKOFF = (60, 300, 900)
DEFAULT_POLL_INTERVAL = 2.0
DEFAULT_RETENTION_INTERVAL_MINUTES = 60
DEFAULT_API_RATE_LIMIT = 60
DEFAULT_PUBLIC_RATE_LIMIT = 10
DEFAULT_YTDLP_UPDATE_INTERVAL_HOURS = 24
DEFAULT_YTDLP_BIN_DIRNAME = "bin"
DEFAULT_RECONCILE_MAX_DELETIONS = 20
DEFAULT_RECONCILE_MAX_FRACTION = 0.2
DEFAULT_PLAYLIST_INTERVAL_MINUTES = 15
MIN_PLAYLIST_INTERVAL_MINUTES = 5
DEFAULT_PLAYLIST_TIMEOUT = 120.0

logger = logging.getLogger(__name__)

#: Shortest sensible allow-list prefix per IP version.  Anything wider than
#: /16 (IPv4) or /48 (IPv6) covers far more than one reverse proxy, so it is
#: accepted only with a warning: a trusted peer picks the rate-limit key and the
#: ``Secure`` flag, so a wrong entry hands those to every address in the range.
TRUSTED_PROXY_MIN_PREFIX = {4: 16, 6: 48}

logger = logging.getLogger(__name__)

_MISSING = object()

#: YouTube channel ids are ``UC`` followed by 22 URL-safe base64 characters.
#: Anything else (a handle, a URL fragment, a typo) is reported as a warning
#: instead of silently falling back to ``defaults`` at retention time.
CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")


class ConfigError(ValueError):
    """Raised for malformed configuration."""


@dataclass(frozen=True)
class Defaults:
    retention_days: int | None = DEFAULT_RETENTION_DAYS
    protect_unfinished: bool = DEFAULT_PROTECT_UNFINISHED
    sponsorblock: Setting = Setting()
    sponsorblock_api: str = DEFAULT_API
    ytdlp_args: tuple[str, ...] = ()
    raw: Mapping[str, Any] = None  # type: ignore[assignment]


@dataclass(frozen=True)
class Channel:
    channel_id: str
    name: str | None
    retention_days: int | None
    sponsorblock: Setting
    protect_unfinished: bool
    raw: Mapping[str, Any] = None  # type: ignore[assignment]


@dataclass(frozen=True)
class AbsConfig:
    """Audiobookshelf connection settings (API key comes from the environment)."""

    url: str = DEFAULT_ABS_URL
    api_key: str = ""
    library_id: str = ""
    library_path: str = ""
    #: ABS user whose playback progress protects unfinished episodes. Empty
    #: means the owner of the API key (legacy behaviour, single account only).
    progress_user: str = ""
    timeout: float = 30.0
    scan_timeout: float = 90.0
    scan_interval: float = 3.0

    @property
    def configured(self) -> bool:
        return bool(self.url and self.api_key and self.library_id and self.library_path)


@dataclass(frozen=True)
class PathsConfig:
    """Local filesystem paths used by the worker."""

    data_dir: str = DEFAULT_DATA_DIR
    tmp_dir: str = DEFAULT_TMP_DIR
    library_path: str = ""  # optional override when yt2abs sees a different mount

    @property
    def database_path(self) -> str:
        return str(Path(self.data_dir) / "yt2abs.sqlite3")


@dataclass(frozen=True)
class WorkerConfig:
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    retry_backoff: tuple[int, ...] = DEFAULT_RETRY_BACKOFF
    poll_interval: float = DEFAULT_POLL_INTERVAL


@dataclass(frozen=True)
class ApiConfig:
    """Rate limits and proxy assumptions for the HTTP surface.

    The service is LAN-only: there is no token and no cookie, so reachability
    of the port is the access control.  ``public_url`` is optional and only
    tells the app its own https address when it sits behind a reverse proxy (it
    is used to build the bookmarklet link and to restore the scheme on
    redirects); it does not enable any authentication.

    ``trusted_proxies`` is a CIDR allow-list of reverse proxies.  Forwarding
    headers (``X-Forwarded-For`` / ``X-Real-IP``) are only honoured when the TCP
    peer matches this list, so a client cannot pick its own rate-limit bucket by
    sending a forged header.  Entries are exact: host bits are rejected instead
    of being silently widened, and entries wider than /16 (IPv4) or /48 (IPv6)
    log a warning.  ``trust_proxy`` is the legacy switch: ``true`` with an empty
    list trusts every peer and only logs a warning.

    An allow-list that covers *every* peer (``0.0.0.0/0``, ``::/0``, or split
    prefixes such as ``0.0.0.0/1`` + ``128.0.0.0/1``) hands the rate-limit
    bucket to anyone, so it is refused unless ``trust_any_peer`` is explicitly
    set to ``true``.
    """

    public_url: str = ""
    #: Extra dotted host names the service answers to.  IP literals, localhost
    #: and single-label names are always accepted; see ``security.host_allowed``.
    allowed_hosts: tuple[str, ...] = ()
    rate_limit_per_minute: int = DEFAULT_API_RATE_LIMIT
    public_rate_limit_per_minute: int = DEFAULT_PUBLIC_RATE_LIMIT
    trust_proxy: bool = False
    trusted_proxies: tuple[str, ...] = ()
    trust_any_peer: bool = False


@dataclass(frozen=True)
class RetentionConfig:
    """Scheduling and safety rails of the hourly retention job."""

    interval_minutes: int = DEFAULT_RETENTION_INTERVAL_MINUTES
    run_on_startup: bool = True
    #: Compute the plan but never write (statuses, ABS deletes, notifications).
    dry_run: bool = False
    #: A pass that would reconcile more than the smaller of
    #: ``reconcile_max_deletions`` and ``reconcile_max_fraction * checked``
    #: disappeared episodes is reported and skipped instead of marking them
    #: deleted. ``0`` disables the respective cap.
    reconcile_max_deletions: int = DEFAULT_RECONCILE_MAX_DELETIONS
    reconcile_max_fraction: float = DEFAULT_RECONCILE_MAX_FRACTION

    @property
    def interval_seconds(self) -> float:
        return float(max(1, self.interval_minutes) * 60)

    def reconcile_limit(self, checked: int) -> int:
        """How many disappeared episodes one pass may reconcile."""
        checked = max(0, int(checked))
        if checked == 0:
            return 0
        caps: list[int] = []
        if self.reconcile_max_deletions > 0:
            caps.append(self.reconcile_max_deletions)
        if self.reconcile_max_fraction > 0:
            caps.append(max(1, math.ceil(checked * self.reconcile_max_fraction)))
        if not caps:
            return checked
        return min(min(caps), checked)


@dataclass(frozen=True)
class YtDlpConfig:
    """yt-dlp self-update behaviour and the managed binary location."""

    update: bool = True
    interval_hours: int = DEFAULT_YTDLP_UPDATE_INTERVAL_HOURS
    bin_dir: str = ""
    command: str = ""

    @property
    def interval_seconds(self) -> float:
        return float(max(1, self.interval_hours) * 3600)


@dataclass(frozen=True)
class PlaylistConfig:
    """Polling of one unlisted YouTube playlist ("add from phone").

    ``enabled`` is the config-level switch; the runtime kill-switch
    ``YT2ABS_PLAYLIST_ENABLED=0`` is applied by the application factory, the
    same way ``YT2ABS_WORKER_ENABLED`` works for the worker.
    """

    enabled: bool = False
    url: str = ""
    interval_minutes: int = DEFAULT_PLAYLIST_INTERVAL_MINUTES
    timeout: float = DEFAULT_PLAYLIST_TIMEOUT

    @property
    def configured(self) -> bool:
        return bool(self.url)

    @property
    def interval_seconds(self) -> float:
        return float(max(MIN_PLAYLIST_INTERVAL_MINUTES, self.interval_minutes) * 60)


@dataclass(frozen=True)
class Config:
    path: Path | None
    defaults: Defaults
    channels: Mapping[str, Channel]
    raw: Mapping[str, Any] = None  # type: ignore[assignment]
    abs: AbsConfig = AbsConfig()
    paths: PathsConfig = PathsConfig()
    worker: WorkerConfig = WorkerConfig()
    api: ApiConfig = ApiConfig()
    retention: RetentionConfig = RetentionConfig()
    ytdlp: YtDlpConfig = YtDlpConfig()
    #: Non-fatal problems found while parsing (shown by ``yt2abs check``).
    warnings: tuple[str, ...] = ()
    playlist: PlaylistConfig = PlaylistConfig()

    def channel(self, channel_id: str | None) -> Channel:
        """Resolved policy for a channel id; unknown/missing ids get the defaults."""
        key = (channel_id or "").strip()
        if key and key in self.channels:
            return self.channels[key]
        return Channel(
            channel_id=key,
            name=None,
            retention_days=self.defaults.retention_days,
            sponsorblock=self.defaults.sponsorblock,
            protect_unfinished=self.defaults.protect_unfinished,
            raw={},
        )

    @property
    def library_path(self) -> str:
        """Directory the worker writes episodes to (as seen by this process)."""
        return self.paths.library_path or self.abs.library_path

    @property
    def ytdlp_bin_dir(self) -> str:
        """Writable directory holding the self-updated yt-dlp binary."""
        return self.ytdlp.bin_dir or str(Path(self.paths.data_dir) / DEFAULT_YTDLP_BIN_DIRNAME)

    @property
    def ytdlp_command(self) -> str:
        """Command (or absolute path) used to invoke yt-dlp.

        A self-updated binary in ``ytdlp_bin_dir`` wins over ``PATH`` so a
        restart always uses the newest version downloaded by the updater.
        """
        if self.ytdlp.command:
            return self.ytdlp.command
        managed = Path(self.ytdlp_bin_dir) / "yt-dlp"
        if managed.is_file() and os.access(managed, os.X_OK):
            return str(managed)
        return os.environ.get("YT2ABS_YTDLP", "").strip() or "yt-dlp"


def _parse_retention(value: Any, *, where: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{where}: retention_days must be an integer or null, got {value!r}")
    if value < 0:
        raise ConfigError(f"{where}: retention_days must be >= 0, got {value!r}")
    return value


def _parse_bool(value: Any, *, where: str) -> bool:
    if isinstance(value, bool):
        return value
    raise ConfigError(f"{where}: expected a boolean, got {value!r}")


def _parse_flag(value: Any, *, where: str, default: bool) -> bool:
    """Boolean that also accepts the usual env-var spellings (1/true/on/...)."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("1", "true", "yes", "on"):
            return True
        if text in ("", "0", "false", "no", "off"):
            return False
    raise ConfigError(f"{where}: expected a boolean, got {value!r}")


def _parse_sponsorblock(value: Any, *, where: str) -> Setting:
    try:
        return parse_setting(value)
    except SponsorBlockConfigError as exc:
        raise ConfigError(f"{where}: {exc}") from exc


def _parse_str_list(value: Any, *, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        raise ConfigError(f"{where}: must be a list of strings, got a single string")
    if not isinstance(value, (list, tuple)):
        raise ConfigError(f"{where}: must be a list of strings, got {type(value).__name__}")
    parsed = []
    for item in value:
        if not isinstance(item, str):
            raise ConfigError(f"{where}: list items must be strings, got {item!r}")
        parsed.append(item)
    return tuple(parsed)


def _parse_non_negative_int(value: Any, *, where: str) -> int:
    if isinstance(value, str) and value.strip().lstrip("+").isdigit():
        value = int(value.strip())
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{where}: expected an integer, got {value!r}")
    if value < 0:
        raise ConfigError(f"{where}: must be >= 0, got {value!r}")
    return value


def _parse_positive_number(value: Any, *, where: str) -> float:
    if isinstance(value, str):
        try:
            value = float(value.strip())
        except ValueError:
            pass
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{where}: expected a number, got {value!r}")
    if value <= 0:
        raise ConfigError(f"{where}: must be > 0, got {value!r}")
    return float(value)


def _parse_fraction(value: Any, *, where: str) -> float:
    """Number in the inclusive range 0..1 (``0`` disables the cap)."""
    if isinstance(value, str):
        try:
            value = float(value.strip())
        except ValueError:
            pass
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{where}: expected a number, got {value!r}")
    if not 0 <= float(value) <= 1:
        raise ConfigError(f"{where}: must be between 0 and 1, got {value!r}")
    return float(value)


def _parse_string(value: Any, *, where: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ConfigError(f"{where}: expected a string, got {value!r}")
    return value.strip()


def _section(data: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    raw = data.get(name) or {}
    if not isinstance(raw, Mapping):
        raise ConfigError(f"{name}: must be a mapping")
    return raw


def _parse_abs(data: Mapping[str, Any]) -> AbsConfig:
    raw = _section(data, "abs")
    return AbsConfig(
        url=_parse_string(raw.get("url", DEFAULT_ABS_URL), where="abs.url").rstrip("/")
        or DEFAULT_ABS_URL,
        api_key=_parse_string(raw.get("api_key"), where="abs.api_key"),
        library_id=_parse_string(raw.get("library_id"), where="abs.library_id"),
        library_path=_parse_string(raw.get("library_path"), where="abs.library_path"),
        progress_user=_parse_string(raw.get("progress_user"), where="abs.progress_user"),
        timeout=_parse_positive_number(raw.get("timeout", 30.0), where="abs.timeout"),
        scan_timeout=_parse_positive_number(
            raw.get("scan_timeout", 90.0), where="abs.scan_timeout"
        ),
        scan_interval=_parse_positive_number(
            raw.get("scan_interval", 3.0), where="abs.scan_interval"
        ),
    )


def _parse_paths(data: Mapping[str, Any]) -> PathsConfig:
    raw = _section(data, "paths")
    return PathsConfig(
        data_dir=_parse_string(raw.get("data_dir", DEFAULT_DATA_DIR), where="paths.data_dir")
        or DEFAULT_DATA_DIR,
        tmp_dir=_parse_string(raw.get("tmp_dir", DEFAULT_TMP_DIR), where="paths.tmp_dir")
        or DEFAULT_TMP_DIR,
        library_path=_parse_string(
            raw.get("library_path"), where="paths.library_path"
        ),
    )


def _parse_worker(data: Mapping[str, Any]) -> WorkerConfig:
    raw = _section(data, "worker")
    backoff_raw = raw.get("retry_backoff_seconds", list(DEFAULT_RETRY_BACKOFF))
    if not isinstance(backoff_raw, (list, tuple)) or not backoff_raw:
        raise ConfigError("worker.retry_backoff_seconds: must be a non-empty list of integers")
    backoff = tuple(
        _parse_non_negative_int(item, where="worker.retry_backoff_seconds")
        for item in backoff_raw
    )
    return WorkerConfig(
        max_attempts=_parse_non_negative_int(
            raw.get("max_attempts", DEFAULT_MAX_ATTEMPTS), where="worker.max_attempts"
        ),
        retry_backoff=backoff,
        poll_interval=_parse_positive_number(
            raw.get("poll_interval", DEFAULT_POLL_INTERVAL), where="worker.poll_interval"
        ),
    )


def _parse_public_url(value: Any) -> str:
    """Validate ``api.public_url`` (optional, absolute http(s) URL)."""
    url = _parse_string(value, where="api.public_url").rstrip("/")
    if not url:
        return ""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ConfigError("api.public_url: must be an absolute http(s) URL")
    return url


def _ipv4_mapped_network(
    network: ipaddress.IPv4Network | ipaddress.IPv6Network,
) -> ipaddress.IPv4Network | None:
    """Reduce a network inside ``::ffff:0:0/96`` to its IPv4 equivalent.

    A dual-stack listener reports IPv4 peers as ``::ffff:a.b.c.d``, so the
    address an operator copies out of the access log is the mapped spelling.
    ``client_ip`` normalises such peers back to IPv4, so an allow-list entry in
    the mapped spelling has to be reduced as well - otherwise it would silently
    stop matching the very peer it was copied from (regression caught by the
    independent review of this change).
    """
    if not isinstance(network, ipaddress.IPv6Network) or network.prefixlen < 96:
        return None
    mapped = network.network_address.ipv4_mapped
    if mapped is None:
        return None
    return ipaddress.IPv4Network(f"{mapped}/{network.prefixlen - 96}", strict=True)


def _trusted_proxies_cover_every_peer(entries: Sequence[str]) -> bool:
    """Whether the allow-list as a whole trusts every IPv4 or every IPv6 peer.

    ``0.0.0.0/0`` is the obvious spelling, but ``0.0.0.0/1`` plus
    ``128.0.0.0/1`` (or ``::/1`` plus ``8000::/1``) covers exactly the same
    space and would slip past a check that only looks at ``prefixlen == 0``.
    Collapsing the networks answers the question for the union, so the explicit
    ``api.trust_any_peer`` switch cannot be bypassed by splitting a prefix.
    """
    networks = [ipaddress.ip_network(entry) for entry in entries]
    ipv4 = [network for network in networks if isinstance(network, ipaddress.IPv4Network)]
    ipv6 = [network for network in networks if isinstance(network, ipaddress.IPv6Network)]
    if ipv4 and list(ipaddress.collapse_addresses(ipv4)) == [
        ipaddress.IPv4Network("0.0.0.0/0")
    ]:
        return True
    if ipv6 and list(ipaddress.collapse_addresses(ipv6)) == [
        ipaddress.IPv6Network("::/0")
    ]:
        return True
    return False


def _parse_trusted_network(text: str, *, where: str) -> str:
    """Canonicalise one allow-list entry, rejecting silently widened networks.

    ``ip_network(..., strict=False)`` turns a typo such as ``10.1.2.1/8`` into
    ``10.0.0.0/8``, i.e. it trusts the whole LAN.  An entry with host bits set
    therefore fails closed with the two spellings the operator probably meant,
    instead of widening the allow-list behind their back.
    """
    try:
        network = ipaddress.ip_network(text, strict=False)
    except ValueError as exc:
        raise ConfigError(f"{where}: invalid CIDR block: {text!r}") from exc
    if "/" in text:
        try:
            address = ipaddress.ip_address(text.split("/", 1)[0].strip())
        except ValueError as exc:
            raise ConfigError(f"{where}: invalid CIDR block: {text!r}") from exc
        if address != network.network_address:
            host_prefix = "/32" if address.version == 4 else "/128"
            raise ConfigError(
                f"{where}: {text!r} has host bits set and would widen to "
                f"{network} - write {network} for the whole network or "
                f"{address}{host_prefix} for the single proxy"
            )
    mapped = _ipv4_mapped_network(network)
    if mapped is not None:
        network = mapped
    normalised = str(network)
    if network.prefixlen == 0:
        # Only reachable when the operator spelled the range out: the default
        # config ships an empty list, so nothing widens to "everybody" silently.
        logger.warning(
            "%s: %s trusts every peer - each client can spoof X-Forwarded-For "
            "and pick its own rate-limit bucket; with a token this also needs "
            "api.trust_any_peer: true, prefer the exact proxy address",
            where,
            normalised,
        )
    elif network.prefixlen < TRUSTED_PROXY_MIN_PREFIX[network.version]:
        host_prefix = 32 if network.version == 4 else 128
        logger.warning(
            "%s: %s covers %d addresses (prefix shorter than /%d) - list the "
            "exact address of the reverse proxy as seen in the access log "
            "(`client_addr`) as a /%d entry instead",
            where,
            normalised,
            network.num_addresses,
            TRUSTED_PROXY_MIN_PREFIX[network.version],
            host_prefix,
        )
    return normalised


def _parse_cidr_list(value: Any, *, where: str) -> tuple[str, ...]:
    """Normalised CIDR allow-list; accepts a list or a comma-separated string."""
    if value is None or value == "":
        return ()
    if isinstance(value, str):
        items: Sequence[Any] = [item.strip() for item in value.split(",")]
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        raise ConfigError(f"{where}: must be a list of CIDR blocks")
    networks: list[str] = []
    for item in items:
        text = _parse_string(item, where=where).strip()
        if not text:
            continue
        normalised = _parse_trusted_network(text, where=where)
        if normalised not in networks:
            networks.append(normalised)
    return tuple(networks)


def _parse_host_list(value: Any, *, where: str) -> tuple[str, ...]:
    """Accept a list or a comma-separated string of host names."""
    if value is None:
        return ()
    if isinstance(value, str):
        entries = [item.strip() for item in value.split(",")]
    elif isinstance(value, (list, tuple)):
        entries = [str(item).strip() for item in value]
    else:
        raise ConfigError(f"{where}: expected a list or a comma-separated string")
    names: list[str] = []
    for entry in entries:
        if not entry:
            continue
        if any(char.isspace() for char in entry) or "/" in entry:
            raise ConfigError(f"{where}: {entry!r} is not a host name")
        lowered = entry.rstrip(".").lower()
        if lowered not in names:
            names.append(lowered)
    return tuple(names)


def _parse_api(data: Mapping[str, Any]) -> ApiConfig:
    raw = _section(data, "api")
    trust_proxy = _parse_flag(
        raw.get("trust_proxy"), where="api.trust_proxy", default=False
    )
    trusted_proxies = _parse_cidr_list(
        raw.get("trusted_proxies"), where="api.trusted_proxies"
    )
    trust_any_peer = _parse_flag(
        raw.get("trust_any_peer"), where="api.trust_any_peer", default=False
    )
    if trust_proxy and not trusted_proxies:
        # ``trust_proxy: true`` without an allow-list trusts every peer: any
        # client can then spoof X-Forwarded-For and pick its own rate-limit
        # bucket.  LAN-only service, so this is a warning, not a startup error.
        logger.warning(
            "api.trust_proxy: true without api.trusted_proxies trusts every peer - "
            "set api.trusted_proxies to the exact reverse-proxy address "
            "(e.g. 10.1.2.1/32) when a proxy is in front of the service"
        )
    if trusted_proxies and _trusted_proxies_cover_every_peer(trusted_proxies):
        # A range that covers every peer is the same "trust everyone" mode the
        # empty list is.  The LAN-only service keeps working, but the operator
        # should say so explicitly.
        if trust_any_peer:
            logger.warning(
                "api.trust_any_peer: true - the allow-list covers every peer, so "
                "any client can spoof X-Forwarded-For and pick its own rate-limit "
                "bucket; never expose this port outside the LAN"
            )
        else:
            logger.warning(
                "api.trusted_proxies: the allow-list covers every peer, so any "
                "client can spoof X-Forwarded-For and pick its own rate-limit "
                "bucket - list the exact proxy address (e.g. 10.1.2.1/32) or set "
                "api.trust_any_peer: true to accept that explicitly"
            )
    return ApiConfig(
        public_url=_parse_public_url(raw.get("public_url")),
        allowed_hosts=_parse_host_list(raw.get("allowed_hosts"), where="api.allowed_hosts"),
        rate_limit_per_minute=max(
            0,
            _parse_non_negative_int(
                raw.get("rate_limit_per_minute", DEFAULT_API_RATE_LIMIT),
                where="api.rate_limit_per_minute",
            ),
        ),
        public_rate_limit_per_minute=max(
            0,
            _parse_non_negative_int(
                raw.get("public_rate_limit_per_minute", DEFAULT_PUBLIC_RATE_LIMIT),
                where="api.public_rate_limit_per_minute",
            ),
        ),
        trust_proxy=trust_proxy,
        trusted_proxies=trusted_proxies,
        trust_any_peer=trust_any_peer,
    )


def _parse_retention_section(data: Mapping[str, Any]) -> RetentionConfig:
    raw = _section(data, "retention")
    return RetentionConfig(
        interval_minutes=max(
            1,
            _parse_non_negative_int(
                raw.get("interval_minutes", DEFAULT_RETENTION_INTERVAL_MINUTES),
                where="retention.interval_minutes",
            ),
        ),
        run_on_startup=_parse_flag(
            raw.get("run_on_startup"), where="retention.run_on_startup", default=True
        ),
        dry_run=_parse_flag(raw.get("dry_run"), where="retention.dry_run", default=False),
        reconcile_max_deletions=_parse_non_negative_int(
            raw.get("reconcile_max_deletions", DEFAULT_RECONCILE_MAX_DELETIONS),
            where="retention.reconcile_max_deletions",
        ),
        reconcile_max_fraction=_parse_fraction(
            raw.get("reconcile_max_fraction", DEFAULT_RECONCILE_MAX_FRACTION),
            where="retention.reconcile_max_fraction",
        ),
    )


def _parse_ytdlp(data: Mapping[str, Any]) -> YtDlpConfig:
    raw = _section(data, "ytdlp")
    return YtDlpConfig(
        update=_parse_flag(raw.get("update"), where="ytdlp.update", default=True),
        interval_hours=max(
            1,
            _parse_non_negative_int(
                raw.get("interval_hours", DEFAULT_YTDLP_UPDATE_INTERVAL_HOURS),
                where="ytdlp.interval_hours",
            ),
        ),
        bin_dir=_parse_string(raw.get("bin_dir"), where="ytdlp.bin_dir"),
        command=_parse_string(raw.get("command"), where="ytdlp.command"),
    )


def _parse_playlist(data: Mapping[str, Any]) -> PlaylistConfig:
    """Validate the ``playlist`` section (disabled by default).

    A playlist URL is validated whenever it is set, even while polling is
    disabled, so a typo surfaces on ``yt2abs.cli check`` instead of at 03:00 on
    the first poll.  ``interval_minutes`` below
    :data:`MIN_PLAYLIST_INTERVAL_MINUTES` is rejected rather than clamped:
    polling YouTube more often than every five minutes is never intended.
    """
    raw = _section(data, "playlist")
    enabled = _parse_flag(raw.get("enabled"), where="playlist.enabled", default=False)
    url = _parse_string(raw.get("url"), where="playlist.url")
    interval = _parse_non_negative_int(
        raw.get("interval_minutes", DEFAULT_PLAYLIST_INTERVAL_MINUTES),
        where="playlist.interval_minutes",
    )
    if interval < MIN_PLAYLIST_INTERVAL_MINUTES:
        raise ConfigError(
            "playlist.interval_minutes: must be >= "
            f"{MIN_PLAYLIST_INTERVAL_MINUTES}, got {interval!r}"
        )
    if url:
        try:
            extract_playlist_id(url)
        except InvalidPlaylistURL as exc:
            raise ConfigError(f"playlist.url: {exc}") from exc
    elif enabled:
        raise ConfigError("playlist.url: required when playlist.enabled is true")
    return PlaylistConfig(
        enabled=enabled,
        url=url,
        interval_minutes=interval,
        timeout=_parse_positive_number(
            raw.get("timeout", DEFAULT_PLAYLIST_TIMEOUT), where="playlist.timeout"
        ),
    )


def _read_document(path: Path) -> Mapping[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read config {path}: {exc}") from exc

    if yaml is not None:
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:  # type: ignore[attr-defined]
            raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    else:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ConfigError(
                f"{path}: PyYAML is not installed and the file is not valid JSON "
                f"(JSON is accepted as a YAML subset): {exc}"
            ) from exc

    if data is None:
        data = {}
    if not isinstance(data, Mapping):
        raise ConfigError(f"{path}: top level of the config must be a mapping")
    return data


def parse_config(data: Mapping[str, Any], *, path: Path | None = None) -> Config:
    """Build a :class:`Config` from an already parsed document."""
    defaults_raw = data.get("defaults") or {}
    if not isinstance(defaults_raw, Mapping):
        raise ConfigError("defaults: must be a mapping")

    defaults = Defaults(
        retention_days=_parse_retention(defaults_raw.get("retention_days", DEFAULT_RETENTION_DAYS), where="defaults"),
        protect_unfinished=_parse_bool(
            defaults_raw.get("protect_unfinished", DEFAULT_PROTECT_UNFINISHED), where="defaults"
        ),
        sponsorblock=_parse_sponsorblock(defaults_raw.get("sponsorblock"), where="defaults"),
        sponsorblock_api=str(defaults_raw.get("sponsorblock_api") or DEFAULT_API).rstrip("/"),
        ytdlp_args=_parse_str_list(defaults_raw.get("ytdlp_args"), where="defaults.ytdlp_args"),
        raw=dict(defaults_raw),
    )

    channels_raw = data.get("channels") or {}
    if isinstance(channels_raw, Mapping):
        items: Sequence[tuple[str, Any]] = [(str(k), v) for k, v in channels_raw.items()]
    elif isinstance(channels_raw, (list, tuple)):
        items = []
        for index, entry in enumerate(channels_raw):
            if not isinstance(entry, Mapping):
                raise ConfigError(f"channels[{index}]: must be a mapping")
            channel_id = entry.get("channel_id") or entry.get("id")
            if not channel_id:
                raise ConfigError(f"channels[{index}]: missing channel_id")
            items.append((str(channel_id), entry))
    else:
        raise ConfigError("channels: must be a mapping (or a list of mappings)")

    channels: dict[str, Channel] = {}
    warnings: list[str] = []
    for channel_id, raw in items:
        if raw is None:
            raw = {}
        if not isinstance(raw, Mapping):
            raise ConfigError(f"channels.{channel_id}: must be a mapping")
        where = f"channels.{channel_id}"
        if not CHANNEL_ID_RE.match(channel_id):
            message = (
                f"{where}: channel id does not look like a YouTube id "
                "(UC + 22 characters); this entry will never match a video and "
                "the channel will silently use defaults"
            )
            warnings.append(message)
            logger.warning(message)
        channels[channel_id] = Channel(
            channel_id=channel_id,
            name=str(raw["name"]) if raw.get("name") else None,
            retention_days=_parse_retention(
                raw.get("retention_days", _MISSING) if "retention_days" in raw else defaults.retention_days,
                where=where,
            ),
            sponsorblock=_parse_sponsorblock(raw.get("sponsorblock"), where=where)
            if "sponsorblock" in raw
            else defaults.sponsorblock,
            protect_unfinished=_parse_bool(
                raw.get("protect_unfinished", defaults.protect_unfinished), where=where
            ),
            raw=dict(raw),
        )

    return Config(
        path=path,
        defaults=defaults,
        channels=channels,
        raw=dict(data),
        abs=_parse_abs(data),
        paths=_parse_paths(data),
        worker=_parse_worker(data),
        api=_parse_api(data),
        retention=_parse_retention_section(data),
        ytdlp=_parse_ytdlp(data),
        warnings=tuple(warnings),
        playlist=_parse_playlist(data),
    )


_ENV_OVERRIDES: Mapping[str, tuple[str, str]] = {
    "ABS_URL": ("abs", "url"),
    "ABS_API_KEY": ("abs", "api_key"),
    "ABS_LIBRARY_ID": ("abs", "library_id"),
    "ABS_LIBRARY_PATH": ("abs", "library_path"),
    "ABS_PROGRESS_USER": ("abs", "progress_user"),
    "YT2ABS_DATA_DIR": ("paths", "data_dir"),
    "YT2ABS_TMP_DIR": ("paths", "tmp_dir"),
    "YT2ABS_LIBRARY_DIR": ("paths", "library_path"),
    "YT2ABS_PUBLIC_URL": ("api", "public_url"),
    "YT2ABS_ALLOWED_HOSTS": ("api", "allowed_hosts"),
    "YT2ABS_API_RATE_LIMIT": ("api", "rate_limit_per_minute"),
    "YT2ABS_PUBLIC_RATE_LIMIT": ("api", "public_rate_limit_per_minute"),
    "YT2ABS_TRUST_PROXY": ("api", "trust_proxy"),
    "YT2ABS_TRUSTED_PROXIES": ("api", "trusted_proxies"),
    "YT2ABS_TRUST_ANY_PEER": ("api", "trust_any_peer"),
    "YT2ABS_RETENTION_INTERVAL_MINUTES": ("retention", "interval_minutes"),
    "YT2ABS_RETENTION_ON_STARTUP": ("retention", "run_on_startup"),
    "YT2ABS_RETENTION_DRY_RUN": ("retention", "dry_run"),
    "YT2ABS_YTDLP_UPDATE": ("ytdlp", "update"),
    "YT2ABS_YTDLP_UPDATE_INTERVAL_HOURS": ("ytdlp", "interval_hours"),
    "YT2ABS_YTDLP_BIN_DIR": ("ytdlp", "bin_dir"),
    "YT2ABS_YTDLP": ("ytdlp", "command"),
}


def apply_env_overrides(data: Mapping[str, Any], env: Mapping[str, str] | None = None) -> Mapping[str, Any]:
    """Overlay selected settings from environment variables (env wins)."""
    source = os.environ if env is None else env
    result: dict[str, Any] = {
        key: (dict(value) if isinstance(value, Mapping) else value)
        for key, value in data.items()
    }
    for env_name, (section, key) in _ENV_OVERRIDES.items():
        value = source.get(env_name)
        if value is None or value == "":
            continue
        section_data = result.get(section)
        if not isinstance(section_data, Mapping):
            section_data = {}
        section_data = dict(section_data)
        section_data[key] = value
        result[section] = section_data
    return result


def load_config(path: str | Path | None = None) -> Config:
    """Load the config from ``path`` or the first default filename that exists."""
    if path is None:
        from_env = os.environ.get("YT2ABS_CONFIG")
        if from_env:
            path = from_env
    if path is not None:
        resolved = Path(path)
        if not resolved.exists():
            raise ConfigError(f"config file not found: {resolved}")
    else:
        resolved = None
        for candidate in DEFAULT_CONFIG_FILENAMES:
            if Path(candidate).exists():
                resolved = Path(candidate)
                break
        if resolved is None:
            raise ConfigError(
                "no config file found (looked for: " + ", ".join(DEFAULT_CONFIG_FILENAMES) + ")"
            )
    return parse_config(apply_env_overrides(_read_document(resolved)), path=resolved)
