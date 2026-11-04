"""yt-dlp self-update: at startup and once a day.

Update pipeline (see :func:`install_release`):

1. :func:`fetch_latest_version` reads the newest version from PyPI;
2. the updater proceeds only when that version is strictly newer than the
   running binary (:func:`_version_key`, no downgrades);
3. the asset is downloaded from the *pinned* release tag
   (``releases/download/<tag>/``), never from ``releases/latest/``;
4. the download goes to ``<target>.part`` and its SHA256 is compared with the
   release's ``SHA2-256SUMS`` entry;
5. the staged file must report the expected version via ``--version``;
6. only then is it ``os.replace``-d over the managed binary.

Any failure removes the staged file and leaves the previous working binary
untouched, so a broken network or a tampered download can never delete the
last working ``/data/bin/yt-dlp``.

The pip fallback is best effort and unsupported in the container image
(site-packages is read-only and ``HOME`` is ephemeral), so it is logged loudly.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .config import Config

logger = logging.getLogger(__name__)

PYPI_URL = "https://pypi.org/pypi/yt-dlp/json"
RELEASE_URL = "https://github.com/yt-dlp/yt-dlp/releases/download/{tag}/{asset}"
SUMS_URL = "https://github.com/yt-dlp/yt-dlp/releases/download/{tag}/SHA2-256SUMS"
SUMS_ASSET = "SHA2-256SUMS"
MAX_BINARY_BYTES = 160 * 1024 * 1024
MAX_SUMS_BYTES = 1024 * 1024
CHUNK_BYTES = 256 * 1024


class UpdateError(RuntimeError):
    """yt-dlp could not be updated."""


@dataclass
class UpdateResult:
    checked_at: str
    previous: str | None = None
    latest: str | None = None
    current: str | None = None
    updated: bool = False
    skipped: bool = False
    method: str | None = None
    sha256: str | None = None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "checked_at": self.checked_at,
            "previous": self.previous,
            "latest": self.latest,
            "current": self.current,
            "updated": self.updated,
            "skipped": self.skipped,
            "method": self.method,
            "sha256": self.sha256,
            "error": self.error,
        }


def release_asset(
    system: str | None = None, machine: str | None = None
) -> str | None:
    """Name of the standalone yt-dlp asset for this platform."""
    system = (system or platform.system()).lower()
    machine = (machine or platform.machine()).lower()
    if system == "linux":
        if machine in ("x86_64", "amd64"):
            return "yt-dlp_linux"
        if machine in ("aarch64", "arm64"):
            return "yt-dlp_linux_aarch64"
        if machine.startswith("armv7"):
            return "yt-dlp_linux_armv7l"
        return None
    if system == "darwin":
        return "yt-dlp_macos"
    if system == "windows":
        return "yt-dlp.exe"
    return None


def release_tag(version: str) -> str:
    """GitHub release tag for a PyPI version (``2026.8.19`` -> ``2026.08.19``)."""
    parts = version.strip().lstrip("v").split(".")
    if not parts or not all(part.isdigit() for part in parts):
        raise UpdateError(f"cannot derive a release tag from version {version!r}")
    return ".".join(part.zfill(2) if len(part) == 1 else part for part in parts)


def _open_url(url: str, *, timeout: float):
    request = urllib.request.Request(url, headers={"User-Agent": "yt2abs/0.2"})
    return urllib.request.urlopen(request, timeout=timeout)  # noqa: S310 - fixed https URLs


def fetch_latest_version(
    url: str = PYPI_URL, *, opener: Callable[..., Any] | None = None, timeout: float = 15.0
) -> str | None:
    """Latest yt-dlp version from PyPI (None when unavailable)."""
    opener = opener or _open_url
    try:
        with opener(url, timeout=timeout) as response:
            document = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        logger.warning("yt-dlp update check failed: %s", exc)
        return None
    version = str(((document or {}).get("info") or {}).get("version") or "").strip()
    return version or None


def parse_sums(document: str) -> dict[str, str]:
    """Parse ``SHA2-256SUMS`` (``<sha256>  <filename>`` per line)."""
    sums: dict[str, str] = {}
    for line in document.splitlines():
        fields = line.split()
        if len(fields) < 2:
            continue
        digest = fields[0].lower()
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            continue
        sums[fields[-1].lstrip("*")] = digest
    return sums


def fetch_release_sums(
    tag: str, *, opener: Callable[..., Any] | None = None, timeout: float = 30.0
) -> dict[str, str]:
    """SHA256 checksums published for the pinned release tag."""
    opener = opener or _open_url
    url = SUMS_URL.format(tag=tag)
    try:
        with opener(url, timeout=timeout) as response:
            body = response.read(MAX_SUMS_BYTES + 1)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise UpdateError(f"cannot fetch {SUMS_ASSET} for {tag}: {exc}") from exc
    if len(body) > MAX_SUMS_BYTES:
        raise UpdateError(f"{SUMS_ASSET} for {tag} is unexpectedly large")
    sums = parse_sums(body.decode("utf-8", "replace"))
    if not sums:
        raise UpdateError(f"{SUMS_ASSET} for {tag} has no usable checksums")
    return sums


def download_release(
    url: str,
    destination: str | Path,
    *,
    opener: Callable[..., Any] | None = None,
    timeout: float = 180.0,
    verify: Callable[[Path, str], None] | None = None,
    max_bytes: int = MAX_BINARY_BYTES,
) -> str:
    """Download ``url`` next to ``destination`` and atomically promote it.

    The body is written to ``.<name>.part``; ``verify`` receives the staged
    path and its SHA256 hex digest *before* anything replaces ``destination``.
    When ``verify`` (or the transfer itself) fails, the staged file is removed
    and ``destination`` is left untouched. Returns the SHA256 hex digest.
    """
    opener = opener or _open_url
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.part")
    digest = hashlib.sha256()
    written = 0
    try:
        with opener(url, timeout=timeout) as response:
            with temporary.open("wb") as handle:
                while True:
                    chunk = response.read(CHUNK_BYTES)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > max_bytes:
                        raise UpdateError("yt-dlp binary is unexpectedly large")
                    digest.update(chunk)
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
        if written == 0:
            raise UpdateError("empty yt-dlp binary")
        os.chmod(temporary, 0o755)
        checksum = digest.hexdigest()
        if verify is not None:
            verify(temporary, checksum)
        os.replace(temporary, target)
        _fsync_directory(target.parent)
        return checksum
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _fsync_directory(directory: Path) -> None:
    """Persist the rename itself (best effort: not every filesystem allows it)."""
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def command_version(runner: Callable[..., Any], command: str) -> str | None:
    """Version printed by ``<command> --version`` (None when it cannot run)."""
    try:
        result = runner([command, "--version"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if getattr(result, "returncode", 1) != 0:
        return None
    lines = [
        line.strip()
        for line in (getattr(result, "stdout", "") or "").splitlines()
        if line.strip()
    ]
    return lines[-1] if lines else None


def install_release(
    version: str,
    destination: str | Path,
    *,
    asset: str,
    opener: Callable[..., Any] | None = None,
    runner: Callable[..., Any] = subprocess.run,
    timeout: float = 180.0,
) -> tuple[str, str]:
    """Install ``version`` of ``asset`` after checksum and version validation.

    Returns ``(reported_version, sha256)`` for the staged binary. Raises
    :class:`UpdateError` on any mismatch; ``destination`` is replaced only on
    full success.
    """
    tag = release_tag(version)
    expected = fetch_release_sums(tag, opener=opener).get(asset)
    if not expected:
        raise UpdateError(f"{SUMS_ASSET} for {tag} has no entry for {asset}")

    reported: list[str] = []

    def verify(path: Path, checksum: str) -> None:
        if checksum != expected:
            raise UpdateError(
                f"SHA256 mismatch for {asset} {tag}: got {checksum}, expected {expected}"
            )
        found = command_version(runner, str(path))
        if not _same_version(found, version):
            raise UpdateError(
                f"downloaded {asset} reports {found or 'no version'}, expected {version}"
            )
        reported.append(found or version)

    url = RELEASE_URL.format(tag=tag, asset=asset)
    logger.info("yt-dlp update: downloading %s %s", asset, tag)
    checksum = download_release(
        url, destination, opener=opener, timeout=timeout, verify=verify
    )
    logger.info("yt-dlp update: %s verified (sha256=%s)", asset, checksum)
    return (reported[0] if reported else version), checksum


def _version_key(value: str) -> tuple[int, ...]:
    parts = value.strip().lstrip("v").split(".")
    key: list[int] = []
    for part in parts[:3]:
        digits = "".join(char for char in part if char.isdigit())
        if not digits:
            break
        key.append(int(digits))
    return tuple(key)


def _same_version(current: str | None, latest: str | None) -> bool:
    if not current or not latest:
        return False
    left = current.strip().lstrip("v")
    right = latest.strip().lstrip("v")
    if left == right or left.startswith(right + "."):
        return True
    # PyPI publishes "2026.8.19" while the binary prints "2026.08.19".
    left_key, right_key = _version_key(left), _version_key(right)
    return bool(left_key and right_key and left_key == right_key)


class YtDlpUpdater:
    """Checks PyPI and refreshes the local yt-dlp executable."""

    def __init__(
        self,
        config: Config,
        *,
        runner: Callable[..., Any] = subprocess.run,
        latest_fetcher: Callable[[], str | None] = fetch_latest_version,
        installer: Callable[..., tuple[str, str | None]] = install_release,
        asset_resolver: Callable[[], str | None] = release_asset,
    ) -> None:
        self.config = config
        self.runner = runner
        self.latest_fetcher = latest_fetcher
        self.installer = installer
        self.asset_resolver = asset_resolver
        self.last_result: UpdateResult | None = None
        self.current_version: str | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ public
    @property
    def bin_path(self) -> Path:
        return Path(self.config.ytdlp_bin_dir) / "yt-dlp"

    def version_of(self, command: str | None = None) -> str | None:
        return command_version(self.runner, command or self.config.ytdlp_command)

    def update(self) -> UpdateResult:
        with self._lock:
            result = UpdateResult(checked_at=_utc_now())
            try:
                self._update_inner(result)
            except Exception as exc:  # noqa: BLE001 - never break startup on updates
                result.error = f"{type(exc).__name__}: {exc}"
                logger.warning("yt-dlp update failed: %s", result.error)
            result.current = self.version_of() or result.current
            self.current_version = result.current or self.current_version
            self.last_result = result
            return result

    def run_forever(self, stop_event, interval_seconds: float) -> None:
        interval = max(60.0, float(interval_seconds))
        logger.info("yt-dlp updater started (interval=%ss)", int(interval))
        while not stop_event.is_set():
            try:
                self.update()
            except Exception as exc:  # noqa: BLE001 - the thread must survive a bug
                logger.error("yt-dlp updater iteration crashed: %s", exc)
            if stop_event.wait(interval):
                break
        logger.info("yt-dlp updater stopped")

    # ---------------------------------------------------------------- internals
    def _update_inner(self, result: UpdateResult) -> None:
        if not self.config.ytdlp.update:
            result.skipped = True
            result.error = "disabled by configuration"
            logger.info("yt-dlp update disabled by configuration")
            return

        if self.config.ytdlp.command:
            result.skipped = True
            result.error = (
                "yt-dlp command is pinned (ytdlp.command / YT2ABS_YTDLP); "
                "automatic updates are disabled"
            )
            logger.info("yt-dlp update skipped: %s", result.error)
            return

        result.previous = self.version_of()
        result.latest = self.latest_fetcher()
        if not result.latest:
            result.error = "cannot determine the latest yt-dlp version"
            return

        latest_key = _version_key(result.latest)
        if not latest_key:
            result.error = f"cannot parse the latest yt-dlp version {result.latest!r}"
            return
        previous_key = _version_key(result.previous) if result.previous else ()
        if previous_key and previous_key >= latest_key:
            result.current = result.previous
            if previous_key == latest_key:
                logger.info("yt-dlp %s is up to date", result.previous)
            else:
                logger.info(
                    "yt-dlp %s is newer than PyPI %s; not downgrading",
                    result.previous,
                    result.latest,
                )
            return

        logger.info(
            "yt-dlp update available: %s -> %s", result.previous or "(missing)", result.latest
        )
        errors: list[str] = []
        if self._install_binary(result, errors):
            return
        if self._install_with_pip(result, errors):
            return
        result.error = "; ".join(errors) or "update failed"
        logger.error("yt-dlp update failed: %s", result.error)

    def _install_binary(self, result: UpdateResult, errors: list[str]) -> bool:
        asset = self.asset_resolver()
        if not asset:
            errors.append("no standalone yt-dlp binary for this platform")
            return False
        target = self.bin_path
        try:
            version, checksum = self.installer(result.latest, target, asset=asset)
        except Exception as exc:  # noqa: BLE001 - fall back to pip, keep the old binary
            errors.append(f"binary update failed: {type(exc).__name__}: {exc}")
            logger.warning(
                "yt-dlp binary update failed (%s); keeping %s", exc, target
            )
            return False

        if not _same_version(version, result.latest):
            errors.append(
                f"installed binary reports {version or 'no version'}, expected {result.latest}"
            )
            return False

        result.current = version
        result.sha256 = checksum
        result.updated = True
        result.method = "binary"
        logger.info("yt-dlp binary updated: %s -> %s (%s)", result.previous, version, target)
        return True

    def _install_with_pip(self, result: UpdateResult, errors: list[str]) -> bool:
        """Best-effort fallback: unsupported in the container image, hence loud."""
        command = self.config.ytdlp_command
        if Path(command) == self.bin_path:
            errors.append(
                "pip fallback skipped: the managed binary is not on pip's install path"
            )
            return False
        logger.warning(
            "yt-dlp: falling back to pip (best effort, unsupported in the container image)"
        )
        pip_command = [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--upgrade",
            "--disable-pip-version-check",
            "yt-dlp",
        ]
        try:
            completed = self.runner(
                pip_command, capture_output=True, text=True, timeout=300
            )
        except (OSError, subprocess.SubprocessError) as exc:
            errors.append(f"pip update failed: {exc}")
            return False
        if getattr(completed, "returncode", 1) != 0:
            detail = (getattr(completed, "stderr", "") or "").strip().splitlines()
            errors.append("pip update failed: " + (detail[-1] if detail else "unknown error"))
            return False

        version = self.version_of(command)
        result.current = version
        result.method = "pip"
        if not _same_version(version, result.latest):
            errors.append(
                f"pip finished but {command} still reports {version or 'no version'}, "
                f"expected {result.latest}"
            )
            return False
        result.updated = True
        logger.info("yt-dlp updated with pip: %s -> %s", result.previous, version)
        return True


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
