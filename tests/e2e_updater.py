"""E2E: safe yt-dlp self-update (staged download, SHA256, version gate).

Default mode runs entirely offline against a local release mirror and proves
the failure paths that matter:

1. verified release -> staged file checked (SHA256 + ``--version``) -> replaced
   atomically, ``.part`` cleaned up, the new binary runs;
2. tampered ``SHA2-256SUMS`` -> nothing is replaced, the old binary still runs;
3. download that reports the wrong version -> nothing is replaced;
4. local binary newer than PyPI -> no download at all (no downgrade);
5. PyPI unreachable -> error reported, old binary untouched;
6. pinned ``ytdlp.command`` -> updater skips, zero HTTP requests;
7. background loop -> runs once at start, then waits the interval.

``--real`` additionally installs from the real PyPI/GitHub with an old fake
binary in place and checks the installed version and SHA256.

Run from the repository root:

    .venv/bin/python tests/e2e_updater.py
    .venv/bin/python tests/e2e_updater.py --real   # network required
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from yt2abs import updater as updater_module
from yt2abs.config import parse_config
from yt2abs.updater import UpdateError, YtDlpUpdater, fetch_latest_version

OLD = "2026.08.01"
RELEASE_VERSION = "2026.9.1"
TAG = "2026.09.01"
ASSET = "yt-dlp_linux"
RESULTS: list[tuple[str, bool, str]] = []


class Mirror(BaseHTTPRequestHandler):
    """Serves one fake PyPI document plus the release files for a single tag."""

    pypi_version = RELEASE_VERSION
    binary = b""
    sums = b""
    requests: list[str] = []

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self.__class__.requests.append(self.path)
        if self.path == "/pypi/yt-dlp/json":
            body = json.dumps({"info": {"version": self.pypi_version}}).encode()
            self._respond(200, "application/json", body)
            return
        if self.path == f"/dl/{TAG}/SHA2-256SUMS":
            self._respond(200, "text/plain", self.sums)
            return
        if self.path == f"/dl/{TAG}/{ASSET}":
            self._respond(200, "application/octet-stream", self.binary)
            return
        self._respond(404, "text/plain", b"not found")

    def _respond(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:  # keep the output readable
        return


def check(name: str, condition: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(condition), detail))
    print(("PASS " if condition else "FAIL ") + name + (f" | {detail}" if detail else ""))
    return bool(condition)


def executable(script: str, version: str) -> bytes:
    return f"#!/bin/sh\necho {version}\n".encode()


def run_version(path: Path) -> str:
    completed = subprocess.run(
        [str(path), "--version"], capture_output=True, text=True, timeout=30
    )
    return (completed.stdout or "").strip()


def write_binary(path: Path, version: str) -> bytes:
    payload = executable("#!/bin/sh", version)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    path.chmod(0o755)
    return payload


def make_updater(root: Path, *, base: str | None = None, **ytdlp: Any) -> YtDlpUpdater:
    config = parse_config(
        {
            "paths": {"data_dir": str(root)},
            "ytdlp": {"bin_dir": str(root / "bin"), **ytdlp},
        }
    )
    if base:
        pypi_url = f"{base}/pypi/yt-dlp/json"
        return YtDlpUpdater(
            config,
            # ``fetch_latest_version`` binds its default URL at import time, so
            # the mirror has to be injected through an explicit fetcher.
            latest_fetcher=lambda: fetch_latest_version(url=pypi_url),
        )
    return YtDlpUpdater(config)


def point_module_at(base: str) -> None:
    updater_module.RELEASE_URL = base + f"/dl/{TAG}/{{asset}}"
    updater_module.SUMS_URL = base + f"/dl/{TAG}/SHA2-256SUMS"


class FakeStop:
    def __init__(self, waits: list[bool]) -> None:
        self.waits = list(waits)
        self.timeouts: list[float] = []

    def is_set(self) -> bool:
        return False

    def wait(self, timeout: float) -> bool:
        self.timeouts.append(timeout)
        return self.waits.pop(0) if self.waits else True


def local_scenarios(server: Mirror, base: str) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        target = root / "bin" / "yt-dlp"

        # 1. verified release -------------------------------------------------
        Mirror.pypi_version = RELEASE_VERSION
        Mirror.binary = executable("#!/bin/sh", "2026.09.01")
        Mirror.sums = (
            f"{hashlib.sha256(Mirror.binary).hexdigest()}  {ASSET}\n".encode()
        )
        write_binary(target, OLD)
        result = make_updater(root, base=base).update()
        check("verified release installed", result.updated and result.method == "binary", str(result.as_dict()))
        check("sha256 reported", result.sha256 == hashlib.sha256(Mirror.binary).hexdigest(), str(result.sha256))
        check("binary replaced", target.read_bytes() == Mirror.binary)
        check("installed binary runs", run_version(target) == "2026.09.01", run_version(target))
        check("staged file cleaned up", not target.with_name(".yt-dlp.part").exists())

        # 2. tampered checksums ----------------------------------------------
        before = write_binary(target, OLD)
        Mirror.sums = f"{'0' * 64}  {ASSET}\n".encode()
        result = make_updater(root, base=base).update()
        check("tampered release rejected", not result.updated and "SHA256 mismatch" in (result.error or ""), result.error or "")
        check("old binary kept after bad checksum", target.read_bytes() == before and run_version(target) == OLD)
        check("no staged file after bad checksum", not target.with_name(".yt-dlp.part").exists())

        # 3. download reporting the wrong version ----------------------------
        Mirror.binary = executable("#!/bin/sh", "2021.01.01")
        Mirror.sums = f"{hashlib.sha256(Mirror.binary).hexdigest()}  {ASSET}\n".encode()
        result = make_updater(root, base=base).update()
        check(
            "wrong version rejected",
            not result.updated and "reports 2021.01.01" in (result.error or ""),
            result.error or "",
        )
        check("old binary kept after wrong version", target.read_bytes() == before and run_version(target) == OLD)

        # 4. no downgrade ----------------------------------------------------
        write_binary(target, "2026.10.01")
        newer = target.read_bytes()
        Mirror.pypi_version = "2026.8.19"
        Mirror.requests.clear()
        result = make_updater(root, base=base).update()
        check("newer local binary is not downgraded", not result.updated and "2026.10.01" in (result.error or result.current or ""), str(result.as_dict()))
        check("no release download for a downgrade", not [path for path in Mirror.requests if path.startswith("/dl/")])
        check("binary untouched on downgrade refusal", target.read_bytes() == newer)

        # 5. PyPI unreachable -------------------------------------------------
        Mirror.pypi_version = RELEASE_VERSION
        write_binary(target, OLD)
        result = make_updater(root, base="http://127.0.0.1:1").update()
        check("offline check reports an error", not result.updated and "latest yt-dlp version" in (result.error or ""), result.error or "")
        check("old binary kept when offline", run_version(target) == OLD)

        # 6. pinned command ---------------------------------------------------
        Mirror.requests.clear()
        result = make_updater(root, command="/custom/yt-dlp").update()
        check("pinned command skips the update", result.skipped and "pinned" in (result.error or ""), result.error or "")
        check("pinned command makes no HTTP requests", Mirror.requests == [])
        check("pinned command keeps the local binary", run_version(target) == OLD)

        # 7. background loop ---------------------------------------------------
        calls: list[int] = []
        loop_updater = make_updater(root, base=base)
        loop_updater.update = lambda: calls.append(1)  # type: ignore[method-assign]
        stop = FakeStop([False, True])
        loop_updater.run_forever(stop, 3600)
        check("loop runs at start and after the interval", len(calls) == 2 and stop.timeouts == [3600.0, 3600.0], str(stop.timeouts))

        crashing = make_updater(root)

        def boom() -> None:
            calls.append(1)
            raise RuntimeError("boom")

        crashing.update = boom  # type: ignore[method-assign]
        stop = FakeStop([True])
        crashing.run_forever(stop, 3600)
        check("loop survives a crashing iteration", True)


def real_scenarios() -> None:
    """Real PyPI/GitHub round-trip with the actual release asset."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        target = root / "bin" / "yt-dlp"
        write_binary(target, "2020.01.01")
        latest = fetch_latest_version()
        check("real PyPI version available", bool(latest), str(latest))
        if not latest:
            return
        result = make_updater(root).update()
        installed = run_version(target)
        check("real update installed a binary", result.updated and result.method == "binary", str(result.as_dict()))
        check("real binary reports the PyPI version", installed.replace("0", "") == (latest or "").replace("0", ""), f"binary={installed} pypi={latest}")
        check("real binary is executable", bool(target.stat().st_mode & 0o111))
        check("real sha256 matches the installed file", result.sha256 == hashlib.sha256(target.read_bytes()).hexdigest(), str(result.sha256))
        check("no staged file after a real update", not target.with_name(".yt-dlp.part").exists())
        again = make_updater(root).update()
        check("second check is up to date", not again.updated and not again.error, str(again.as_dict()))


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Mirror)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    real_urls = (updater_module.RELEASE_URL, updater_module.SUMS_URL)
    point_module_at(base)
    try:
        local_scenarios(Mirror, base)  # type: ignore[arg-type]
        # The local mirror must not leak into the real PyPI/GitHub round-trip.
        updater_module.RELEASE_URL, updater_module.SUMS_URL = real_urls
        if "--real" in sys.argv:
            real_scenarios()
    finally:
        server.shutdown()
        server.server_close()
    failed = [name for name, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    if failed:
        print("FAILED: " + ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
