"""yt-dlp self-update: version checks, staged install, pip fallback, loop."""
from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import tempfile
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from yt2abs.config import parse_config
from yt2abs.updater import (
    UpdateError,
    YtDlpUpdater,
    download_release,
    fetch_latest_version,
    fetch_release_sums,
    install_release,
    parse_sums,
    release_asset,
    release_tag,
)

BINARY = b"#!/bin/sh\necho 2026.09.01\n"
BINARY_SHA = hashlib.sha256(BINARY).hexdigest()


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        self.close()


class FailingResponse(FakeResponse):
    """Serves ``fail_after`` bytes, then drops the connection mid-stream."""

    def __init__(self, payload: bytes, fail_after: int) -> None:
        super().__init__(payload)
        self.fail_after = fail_after

    def read(self, size: int = -1) -> bytes:
        remaining = self.fail_after - self.tell()
        if remaining <= 0:
            raise OSError("connection reset by peer")
        if size is None or size < 0:
            size = remaining
        return super().read(min(size, remaining))


class UrlOpener:
    """Serves canned bodies per URL and records every request."""

    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = routes
        self.urls: list[str] = []

    def __call__(self, url: str, *, timeout: float):
        self.urls.append(url)
        value = self.routes.get(url)
        if value is None:
            raise urllib.error.URLError(f"no route for {url}")
        if isinstance(value, Exception):
            raise value
        return FakeResponse(value)


class FakeRunner:
    """``--version`` reports ``version``; pip flips it to ``pip_version``."""

    def __init__(
        self,
        version: str = "2026.08.01",
        *,
        pip_returncode: int = 0,
        pip_version: str = "2026.09.01",
        pip_stderr: str = "ERROR: no network",
        on_version: Any = None,
    ) -> None:
        self.version = version
        self.pip_returncode = pip_returncode
        self.pip_version = pip_version
        self.pip_stderr = pip_stderr
        self.on_version = on_version
        self.calls: list[list[str]] = []

    def __call__(self, command: list[str], **kwargs: Any) -> Any:
        self.calls.append(list(command))
        if "--version" in command:
            if self.on_version is not None:
                self.on_version(command[0])
            return SimpleNamespace(returncode=0, stdout=f"{self.version}\n", stderr="")
        if "-m" in command and "pip" in command:
            if self.pip_returncode == 0:
                self.version = self.pip_version
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            return SimpleNamespace(
                returncode=self.pip_returncode, stdout="", stderr=self.pip_stderr
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def pip_calls(self) -> list[list[str]]:
        return [call for call in self.calls if "pip" in call]


class MissingBinaryRunner(FakeRunner):
    """No managed binary until something writes an executable ``yt-dlp``."""

    def __init__(self, new_version: str = "2026.09.01") -> None:
        super().__init__("")
        self.new_version = new_version

    def __call__(self, command: list[str], **kwargs: Any) -> Any:
        self.calls.append(list(command))
        if "--version" in command:
            path = Path(command[0])
            if path.name == "yt-dlp" and path.exists():
                return SimpleNamespace(
                    returncode=0, stdout=f"{self.new_version}\n", stderr=""
                )
            return SimpleNamespace(returncode=1, stdout="", stderr="not found")
        return SimpleNamespace(returncode=0, stdout="", stderr="")


class FakeStopEvent:
    def __init__(self, waits: list[bool] | None = None) -> None:
        self.waits = list(waits or [])
        self.timeouts: list[float] = []

    def is_set(self) -> bool:
        return False

    def wait(self, timeout: float) -> bool:
        self.timeouts.append(timeout)
        return self.waits.pop(0) if self.waits else True


def make_config(tmp: str, **ytdlp: Any):
    return parse_config({"paths": {"data_dir": tmp}, "ytdlp": {"bin_dir": tmp, **ytdlp}})


def fake_installer(runner: FakeRunner, version: str = "2026.09.01"):
    """Writes the new binary to ``target`` and reports the new version."""

    def install(expected: str, target: str | Path, *, asset: str):
        path = Path(target)
        path.write_bytes(BINARY)
        path.chmod(0o755)
        runner.version = version
        return version, BINARY_SHA

    return install


class UpdaterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def make_updater(self, runner, **kwargs: Any) -> YtDlpUpdater:
        config = kwargs.pop("config", None) or make_config(str(self.root))
        return YtDlpUpdater(
            config,
            runner=runner,
            latest_fetcher=kwargs.pop("latest_fetcher", lambda: "2026.08.01"),
            installer=kwargs.pop("installer", fake_installer(runner)),
            asset_resolver=kwargs.pop("asset_resolver", lambda: "yt-dlp_linux"),
        )

    def old_binary(self, payload: bytes = b"old-binary") -> Path:
        target = self.root / "yt-dlp"
        target.write_bytes(payload)
        target.chmod(0o755)
        return target

    # ------------------------------------------------------------ happy paths
    def test_up_to_date_does_not_install(self) -> None:
        runner = FakeRunner("2026.08.01")
        installs: list[str] = []
        updater = self.make_updater(
            runner, installer=lambda *a, **k: installs.append(a[1])  # type: ignore[func-returns-value]
        )
        result = updater.update()
        self.assertFalse(result.updated)
        self.assertFalse(result.skipped)
        self.assertEqual(result.latest, "2026.08.01")
        self.assertEqual(result.current, "2026.08.01")
        self.assertEqual(installs, [])
        self.assertEqual(updater.current_version, "2026.08.01")

    def test_newer_version_installs_binary_and_reports_sha256(self) -> None:
        runner = FakeRunner("2026.08.01")
        updater = self.make_updater(runner, latest_fetcher=lambda: "2026.09.01")
        result = updater.update()
        self.assertTrue(result.updated)
        self.assertEqual(result.method, "binary")
        self.assertEqual(result.previous, "2026.08.01")
        self.assertEqual(result.current, "2026.09.01")
        self.assertEqual(result.sha256, BINARY_SHA)
        self.assertTrue((self.root / "yt-dlp").exists())

    def test_missing_binary_is_installed(self) -> None:
        runner = MissingBinaryRunner()

        def install(expected: str, target: str | Path, *, asset: str):
            path = Path(target)
            path.write_bytes(BINARY)
            path.chmod(0o755)
            return "2026.09.01", BINARY_SHA

        updater = self.make_updater(
            runner, latest_fetcher=lambda: "2026.09.01", installer=install
        )
        result = updater.update()
        self.assertTrue(result.updated)
        self.assertEqual(result.method, "binary")

    # -------------------------------------------------- never break the binary
    def test_download_failure_keeps_the_working_binary(self) -> None:
        """Regression (review #3): a failed download must not unlink the target."""
        target = self.old_binary()
        before = target.read_bytes()
        runner = FakeRunner("2026.08.01", pip_returncode=1)

        def broken(*args: Any, **kwargs: Any):
            raise UpdateError("HTTP 503 from github.com")

        updater = self.make_updater(
            runner, latest_fetcher=lambda: "2026.09.01", installer=broken
        )
        result = updater.update()
        self.assertFalse(result.updated)
        self.assertTrue(target.exists(), "the working binary was deleted")
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(result.current, "2026.08.01")
        self.assertIn("binary update failed", result.error or "")
        self.assertIn("HTTP 503", result.error or "")

    def test_wrong_version_keeps_the_working_binary(self) -> None:
        target = self.old_binary()
        before = target.read_bytes()
        runner = FakeRunner("2026.08.01", pip_returncode=1)

        def wrong(expected: str, destination: str | Path, *, asset: str):
            return "2021.01.01", BINARY_SHA

        updater = self.make_updater(
            runner, latest_fetcher=lambda: "2026.09.01", installer=wrong
        )
        result = updater.update()
        self.assertFalse(result.updated)
        self.assertTrue(target.exists())
        self.assertEqual(target.read_bytes(), before)
        self.assertIn("expected 2026.09.01", result.error or "")

    def test_newer_than_pypi_does_not_downgrade(self) -> None:
        target = self.old_binary()
        before = target.read_bytes()
        runner = FakeRunner("2026.10.01")
        installs: list[str] = []
        updater = self.make_updater(
            runner,
            latest_fetcher=lambda: "2026.08.01",
            installer=lambda *a, **k: installs.append(a[0]),  # type: ignore[func-returns-value]
        )
        result = updater.update()
        self.assertFalse(result.updated)
        self.assertFalse(result.skipped)
        self.assertIsNone(result.error)
        self.assertEqual(result.current, "2026.10.01")
        self.assertEqual(installs, [])
        self.assertEqual(target.read_bytes(), before)

    def test_old_binary_still_runs_after_a_failed_update(self) -> None:
        """The acceptance check: a real executable keeps answering ``--version``."""
        target = self.root / "yt-dlp"
        target.write_text("#!/bin/sh\necho 2026.08.01\n")
        target.chmod(0o755)
        updater = self.make_updater(
            subprocess.run,
            latest_fetcher=lambda: "2026.09.01",
            installer=lambda *a, **k: (_ for _ in ()).throw(UpdateError("HTTP 503")),
        )
        result = updater.update()
        self.assertFalse(result.updated)
        self.assertIn("HTTP 503", result.error or "")
        self.assertEqual(
            subprocess.run(
                [str(target), "--version"], capture_output=True, text=True, timeout=30
            ).stdout.strip(),
            "2026.08.01",
        )
        self.assertEqual(updater.version_of(), "2026.08.01")

    # -------------------------------------------------------- skip conditions
    def test_pinned_command_skips_the_update(self) -> None:
        runner = FakeRunner("2026.08.01")
        installs: list[str] = []
        config = parse_config(
            {
                "paths": {"data_dir": str(self.root)},
                "ytdlp": {"bin_dir": str(self.root), "command": "/custom/yt-dlp"},
            }
        )
        updater = self.make_updater(
            runner,
            config=config,
            installer=lambda *a, **k: installs.append(a[0]),  # type: ignore[func-returns-value]
        )
        result = updater.update()
        self.assertTrue(result.skipped)
        self.assertFalse(result.updated)
        self.assertEqual(installs, [])
        self.assertIn("pinned", result.error or "")

    def test_env_pinned_command_skips_the_update(self) -> None:
        from yt2abs.config import apply_env_overrides

        runner = FakeRunner("2026.08.01")
        installs: list[str] = []
        document = apply_env_overrides(
            {"paths": {"data_dir": str(self.root)}, "ytdlp": {"bin_dir": str(self.root)}},
            {"YT2ABS_YTDLP": "/custom/yt-dlp"},
        )
        updater = self.make_updater(
            runner,
            config=parse_config(document),
            installer=lambda *a, **k: installs.append(a[0]),  # type: ignore[func-returns-value]
        )
        result = updater.update()
        self.assertTrue(result.skipped)
        self.assertEqual(installs, [])

    def test_disabled_update_is_skipped(self) -> None:
        runner = FakeRunner("2026.08.01")
        updater = self.make_updater(
            runner, config=make_config(str(self.root), update=False)
        )
        result = updater.update()
        self.assertTrue(result.skipped)
        self.assertFalse(result.updated)
        self.assertEqual(result.error, "disabled by configuration")

    def test_missing_latest_version_reports_error(self) -> None:
        runner = FakeRunner("2026.08.01")
        updater = self.make_updater(runner, latest_fetcher=lambda: None)
        result = updater.update()
        self.assertFalse(result.updated)
        self.assertIn("latest", result.error or "")

    def test_unparsable_latest_version_reports_error(self) -> None:
        runner = FakeRunner("2026.08.01")
        updater = self.make_updater(runner, latest_fetcher=lambda: "nightly")
        result = updater.update()
        self.assertFalse(result.updated)
        self.assertIn("cannot parse", result.error or "")

    def test_version_prefix_and_suffix_are_equivalent(self) -> None:
        runner = FakeRunner("2026.08.01.120000")
        updater = self.make_updater(runner, latest_fetcher=lambda: "2026.08.01")
        result = updater.update()
        self.assertFalse(result.updated)

    def test_version_zero_padding_is_equivalent(self) -> None:
        runner = FakeRunner("2026.08.19")
        updater = self.make_updater(runner, latest_fetcher=lambda: "2026.8.19")
        result = updater.update()
        self.assertFalse(result.updated)

    # ------------------------------------------------------------ pip fallback
    def test_binary_failure_falls_back_to_pip_without_managed_binary(self) -> None:
        runner = FakeRunner("2026.08.01")
        updater = self.make_updater(
            runner,
            config=make_config(str(self.root), bin_dir=str(self.root / "missing")),
            latest_fetcher=lambda: "2026.09.01",
            installer=lambda *a, **k: (_ for _ in ()).throw(UpdateError("no network")),
        )
        result = updater.update()
        self.assertTrue(result.updated)
        self.assertEqual(result.method, "pip")
        self.assertEqual(result.current, "2026.09.01")
        self.assertEqual(len(runner.pip_calls()), 1)

    def test_pip_fallback_is_skipped_for_the_managed_binary(self) -> None:
        """pip cannot update the file the pipeline actually runs."""
        self.old_binary()
        runner = FakeRunner("2026.08.01")
        updater = self.make_updater(
            runner,
            latest_fetcher=lambda: "2026.09.01",
            installer=lambda *a, **k: (_ for _ in ()).throw(UpdateError("no network")),
        )
        result = updater.update()
        self.assertFalse(result.updated)
        self.assertEqual(runner.pip_calls(), [])
        self.assertIn("pip fallback skipped", result.error or "")

    def test_pip_permission_denied_reports_error_without_touching_binary(self) -> None:
        target = self.old_binary()
        before = target.read_bytes()
        runner = FakeRunner(
            "2026.08.01",
            pip_returncode=1,
            pip_stderr=(
                "ERROR: Could not install packages due to an OSError: "
                "[Errno 13] Permission denied: '/usr/local/lib/python3.12/site-packages'"
            ),
        )
        updater = self.make_updater(
            runner,
            config=make_config(str(self.root), bin_dir=str(self.root / "missing")),
            latest_fetcher=lambda: "2026.09.01",
            installer=lambda *a, **k: (_ for _ in ()).throw(UpdateError("no network")),
        )
        result = updater.update()
        self.assertFalse(result.updated)
        self.assertIn("Permission denied", result.error or "")
        self.assertEqual(target.read_bytes(), before)

    # ------------------------------------------------------------------ loop
    def test_run_forever_runs_on_start_and_every_interval(self) -> None:
        runner = FakeRunner("2026.09.01")
        updater = self.make_updater(runner, latest_fetcher=lambda: "2026.09.01")
        calls: list[int] = []
        updater.update = lambda: calls.append(1) or updater.last_result  # type: ignore[method-assign]
        stop = FakeStopEvent([False, True])
        updater.run_forever(stop, 3600)
        self.assertEqual(len(calls), 2)
        self.assertEqual(stop.timeouts, [3600.0, 3600.0])

    def test_run_forever_clamps_short_intervals_to_one_minute(self) -> None:
        updater = self.make_updater(FakeRunner("2026.09.01"))
        updater.update = lambda: None  # type: ignore[method-assign]
        stop = FakeStopEvent([True])
        updater.run_forever(stop, 5)
        self.assertEqual(stop.timeouts, [60.0])

    def test_run_forever_stops_when_the_event_is_set(self) -> None:
        updater = self.make_updater(FakeRunner("2026.09.01"))
        calls: list[int] = []
        updater.update = lambda: calls.append(1)  # type: ignore[method-assign]
        stop = FakeStopEvent([])
        stop.is_set = lambda: True  # type: ignore[method-assign]
        updater.run_forever(stop, 3600)
        self.assertEqual(calls, [])

    def test_run_forever_survives_a_crashing_update(self) -> None:
        """A bug inside one iteration must not kill the background thread."""
        updater = self.make_updater(FakeRunner("2026.09.01"))
        calls: list[int] = []

        def boom() -> None:
            calls.append(1)
            raise RuntimeError("unexpected")

        updater.update = boom  # type: ignore[method-assign]
        stop = FakeStopEvent([False, True])
        with self.assertLogs("yt2abs.updater", level="ERROR") as logs:
            updater.run_forever(stop, 120)
        self.assertEqual(len(calls), 2)
        self.assertTrue(any("crashed" in line for line in logs.output))


class HelperTests(unittest.TestCase):
    def test_release_asset_mapping(self) -> None:
        self.assertEqual(release_asset("Linux", "x86_64"), "yt-dlp_linux")
        self.assertEqual(release_asset("Linux", "aarch64"), "yt-dlp_linux_aarch64")
        self.assertEqual(release_asset("Darwin", "arm64"), "yt-dlp_macos")
        self.assertEqual(release_asset("Windows", "AMD64"), "yt-dlp.exe")
        self.assertIsNone(release_asset("Linux", "riscv64"))

    def test_release_tag_zero_pads_the_date(self) -> None:
        self.assertEqual(release_tag("2026.8.19"), "2026.08.19")
        self.assertEqual(release_tag("2026.08.19"), "2026.08.19")
        self.assertEqual(release_tag("v2026.8.1"), "2026.08.01")

    def test_release_tag_rejects_non_numeric_versions(self) -> None:
        with self.assertRaises(UpdateError):
            release_tag("nightly")
        with self.assertRaises(UpdateError):
            release_tag("2026.08.19.post1")

    def test_parse_sums(self) -> None:
        document = (
            f"{BINARY_SHA}  yt-dlp_linux\n"
            f"{'A' * 64} *yt-dlp.exe\n"
            "garbage line\n"
            f"{'b' * 64}  yt-dlp_macos\n"
        )
        sums = parse_sums(document)
        self.assertEqual(sums["yt-dlp_linux"], BINARY_SHA)
        self.assertEqual(sums["yt-dlp.exe"], "a" * 64)
        self.assertEqual(sums["yt-dlp_macos"], "b" * 64)
        self.assertNotIn("garbage", sums)

    def test_fetch_latest_version(self) -> None:
        payload = json.dumps({"info": {"version": "2026.09.01"}}).encode()

        def opener(url: str, *, timeout: float):
            return FakeResponse(payload)

        self.assertEqual(fetch_latest_version(opener=opener), "2026.09.01")

    def test_fetch_latest_version_handles_failure(self) -> None:
        def opener(url: str, *, timeout: float):
            raise OSError("offline")

        self.assertIsNone(fetch_latest_version(opener=opener))

    def test_fetch_release_sums_errors_are_reported(self) -> None:
        with self.assertRaises(UpdateError):
            fetch_release_sums("2026.09.01", opener=UrlOpener({}))
        with self.assertRaises(UpdateError):
            fetch_release_sums(
                "2026.09.01",
                opener=UrlOpener(
                    {
                        "https://github.com/yt-dlp/yt-dlp/releases/download/2026.09.01/SHA2-256SUMS": b"junk\n"
                    }
                ),
            )

    def test_oversized_sums_document_is_rejected(self) -> None:
        opener = UrlOpener(
            {
                "https://github.com/yt-dlp/yt-dlp/releases/download/2026.09.01/SHA2-256SUMS": b"x"
                * (1024 * 1024 + 1)
            }
        )
        with self.assertRaises(UpdateError) as caught:
            fetch_release_sums("2026.09.01", opener=opener)
        self.assertIn("unexpectedly large", str(caught.exception))


class DownloadReleaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_download_release_is_atomic_and_executable(self) -> None:
        payload = b"#!/bin/sh\necho 2026.09.01\n"
        opener = UrlOpener({"https://example.test/yt-dlp": payload})
        target = self.root / "yt-dlp"
        target.write_bytes(b"old")
        checksum = download_release("https://example.test/yt-dlp", target, opener=opener)
        self.assertEqual(checksum, hashlib.sha256(payload).hexdigest())
        self.assertEqual(target.read_bytes(), payload)
        self.assertTrue(target.stat().st_mode & 0o111)
        self.assertFalse(target.with_name(".yt-dlp.part").exists())

    def test_download_failure_leaves_the_target_untouched(self) -> None:
        target = self.root / "yt-dlp"
        target.write_bytes(b"old")
        opener = UrlOpener({"https://example.test/yt-dlp": OSError("offline")})
        with self.assertRaises(OSError):
            download_release("https://example.test/yt-dlp", target, opener=opener)
        self.assertEqual(target.read_bytes(), b"old")
        self.assertFalse(target.with_name(".yt-dlp.part").exists())

    def test_failed_verification_leaves_the_target_untouched(self) -> None:
        target = self.root / "yt-dlp"
        target.write_bytes(b"old")
        opener = UrlOpener({"https://example.test/yt-dlp": BINARY})

        def verify(path: Path, checksum: str) -> None:
            raise UpdateError("checksum mismatch")

        with self.assertRaises(UpdateError):
            download_release(
                "https://example.test/yt-dlp", target, opener=opener, verify=verify
            )
        self.assertEqual(target.read_bytes(), b"old")
        self.assertFalse(target.with_name(".yt-dlp.part").exists())

    def test_verify_sees_the_staged_file_while_the_target_is_untouched(self) -> None:
        """Review #3: validation must happen before ``os.replace``."""
        target = self.root / "yt-dlp"
        target.write_bytes(b"old")
        opener = UrlOpener({"https://example.test/yt-dlp": BINARY})
        seen: dict[str, Any] = {}

        def verify(path: Path, checksum: str) -> None:
            seen["staged"] = path.name
            seen["target_during_verify"] = target.read_bytes()

        download_release(
            "https://example.test/yt-dlp", target, opener=opener, verify=verify
        )
        self.assertEqual(seen["staged"], ".yt-dlp.part")
        self.assertEqual(seen["target_during_verify"], b"old")
        self.assertEqual(target.read_bytes(), BINARY)

    def test_download_dropped_mid_stream_keeps_the_target(self) -> None:
        target = self.root / "yt-dlp"
        target.write_bytes(b"old")

        def opener(url: str, *, timeout: float):
            return FailingResponse(BINARY, fail_after=10)

        with self.assertRaises(OSError):
            download_release("https://example.test/yt-dlp", target, opener=opener)
        self.assertEqual(target.read_bytes(), b"old")
        self.assertFalse(target.with_name(".yt-dlp.part").exists())

    def test_oversized_download_is_rejected(self) -> None:
        target = self.root / "yt-dlp"
        target.write_bytes(b"old")
        opener = UrlOpener({"https://example.test/yt-dlp": b"x" * 64})
        with self.assertRaises(UpdateError):
            download_release(
                "https://example.test/yt-dlp", target, opener=opener, max_bytes=32
            )
        self.assertEqual(target.read_bytes(), b"old")
        self.assertFalse(target.with_name(".yt-dlp.part").exists())

    def test_empty_download_is_rejected(self) -> None:
        target = self.root / "yt-dlp"
        target.write_bytes(b"old")
        opener = UrlOpener({"https://example.test/yt-dlp": b""})
        with self.assertRaises(UpdateError):
            download_release("https://example.test/yt-dlp", target, opener=opener)
        self.assertEqual(target.read_bytes(), b"old")


class InstallReleaseTests(unittest.TestCase):
    """The real download -> SHA256 -> --version -> replace pipeline."""

    VERSION = "2026.9.1"
    TAG = "2026.09.01"
    ASSET = "yt-dlp_linux"
    BINARY_URL = (
        "https://github.com/yt-dlp/yt-dlp/releases/download/2026.09.01/yt-dlp_linux"
    )
    SUMS_URL = (
        "https://github.com/yt-dlp/yt-dlp/releases/download/2026.09.01/SHA2-256SUMS"
    )

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.target = self.root / "yt-dlp"
        self.target.write_bytes(b"old-binary")
        self.target.chmod(0o755)

    def routes(self, *, sums: bytes | None = None) -> dict[str, Any]:
        return {
            self.SUMS_URL: sums
            if sums is not None
            else f"{BINARY_SHA}  {self.ASSET}\n".encode(),
            self.BINARY_URL: BINARY,
        }

    def test_verified_release_is_installed(self) -> None:
        opener = UrlOpener(self.routes())
        version, checksum = install_release(
            self.VERSION,
            self.target,
            asset=self.ASSET,
            opener=opener,
            runner=FakeRunner("2026.09.01"),
        )
        self.assertEqual(version, "2026.09.01")
        self.assertEqual(checksum, BINARY_SHA)
        self.assertEqual(self.target.read_bytes(), BINARY)
        self.assertTrue(self.target.stat().st_mode & 0o111)

    def test_checksum_mismatch_keeps_the_old_binary(self) -> None:
        opener = UrlOpener(self.routes(sums=f"{'0' * 64}  {self.ASSET}\n".encode()))
        with self.assertRaises(UpdateError) as caught:
            install_release(
                self.VERSION,
                self.target,
                asset=self.ASSET,
                opener=opener,
                runner=FakeRunner("2026.09.01"),
            )
        self.assertIn("SHA256 mismatch", str(caught.exception))
        self.assertEqual(self.target.read_bytes(), b"old-binary")
        self.assertFalse(self.target.with_name(".yt-dlp.part").exists())

    def test_staged_binary_with_wrong_version_is_rejected(self) -> None:
        opener = UrlOpener(self.routes())
        with self.assertRaises(UpdateError) as caught:
            install_release(
                self.VERSION,
                self.target,
                asset=self.ASSET,
                opener=opener,
                runner=FakeRunner("2021.01.01"),
            )
        self.assertIn("reports 2021.01.01", str(caught.exception))
        self.assertEqual(self.target.read_bytes(), b"old-binary")
        self.assertFalse(self.target.with_name(".yt-dlp.part").exists())

    def test_staged_binary_that_cannot_run_is_rejected(self) -> None:
        """A corrupt/foreign executable fails ``--version`` before the swap."""

        def broken_runner(command: list[str], **kwargs: Any):
            raise OSError("Exec format error")

        opener = UrlOpener(self.routes())
        with self.assertRaises(UpdateError) as caught:
            install_release(
                self.VERSION,
                self.target,
                asset=self.ASSET,
                opener=opener,
                runner=broken_runner,
            )
        self.assertIn("reports no version", str(caught.exception))
        self.assertEqual(self.target.read_bytes(), b"old-binary")
        self.assertFalse(self.target.with_name(".yt-dlp.part").exists())

    def test_binary_missing_from_sums_is_rejected(self) -> None:
        opener = UrlOpener(self.routes(sums=f"{'0' * 64}  yt-dlp\n".encode()))
        with self.assertRaises(UpdateError) as caught:
            install_release(
                self.VERSION,
                self.target,
                asset=self.ASSET,
                opener=opener,
                runner=FakeRunner("2026.09.01"),
            )
        self.assertIn("no entry", str(caught.exception))
        self.assertEqual(self.target.read_bytes(), b"old-binary")

    def test_binary_download_failure_keeps_the_old_binary(self) -> None:
        routes = self.routes()
        routes[self.BINARY_URL] = OSError("connection reset")
        with self.assertRaises(OSError):
            install_release(
                self.VERSION,
                self.target,
                asset=self.ASSET,
                opener=UrlOpener(routes),
                runner=FakeRunner("2026.09.01"),
            )
        self.assertEqual(self.target.read_bytes(), b"old-binary")
        self.assertFalse(self.target.with_name(".yt-dlp.part").exists())

    def test_only_the_pinned_tag_url_is_requested(self) -> None:
        opener = UrlOpener(self.routes())
        install_release(
            self.VERSION,
            self.target,
            asset=self.ASSET,
            opener=opener,
            runner=FakeRunner("2026.09.01"),
        )
        self.assertEqual(opener.urls, [self.SUMS_URL, self.BINARY_URL])
        self.assertTrue(all("/2026.09.01/" in url for url in opener.urls))
        self.assertFalse(any("/latest/" in url for url in opener.urls))


if __name__ == "__main__":
    unittest.main()
