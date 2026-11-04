"""End-to-end check: yt2abs job events -> Discord webhook -> Discord read-back.

The production Audiobookshelf is deliberately **not** touched: the test starts a
local stub that exposes the same HTTP endpoints yt2abs uses (library scan, item
list, item details, episode and podcast metadata PATCH). Everything else is the
real thing: SQLite queue, worker, yt-dlp download, ``yt2abs.notify`` module and
the real Discord webhook.

A loopback capture proxy sits between the notifier and discord.com. It forwards
every request to the real webhook with ``?wait=true``, records the payload and
the message id returned by Discord, and hands the response back to the notifier.
Each recorded message is then fetched **directly from Discord** through
``GET /api/webhooks/{id}/{token}/messages/{message_id}`` - that read-back is the
independent proof that the message really landed in the channel.

Requirements
------------
* ``DISCORD_WEBHOOK_URL`` in the environment (secret - never printed)
* network access to YouTube and discord.com
* ``yt-dlp`` and ``ffmpeg`` in ``PATH``

Usage
-----
    DISCORD_WEBHOOK_URL=... .venv/bin/python tests/e2e_discord.py
    DISCORD_WEBHOOK_URL=... .venv/bin/python tests/e2e_discord.py \
        --report /tmp/yt2abs-discord-e2e.json

Exit codes: 0 = all checks passed, 1 = a check failed, 2 = missing environment
or unreachable service. The work directory with config, logs and ``report.json``
stays on disk as evidence (under ``.e2e/``, git-ignored).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
VALID_VIDEO = "dQw4w9WgXcQ"
INVALID_VIDEO = "AAAAAAAAAAA"
JOB_TIMEOUT = float(os.environ.get("YT2ABS_E2E_TIMEOUT", "900"))
EVENT_TITLES = {
    "imported": "✅ Imported to Audiobookshelf",
    "retry": "⚠️ Import retry",
    "failed": "❌ Import failed",
    "cli": "yt2abs: test powiadomien",
}
AUDIO_SUFFIXES = {".m4a", ".mp3", ".webm", ".opus", ".ogg", ".mka", ".aac", ".flac"}


def log(message: str) -> None:
    print(f"[e2e] {message}", flush=True)


def http_json(url: str, *, method: str = "GET", body: dict | None = None,
              timeout: float = 20.0) -> tuple[int, Any]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - local/e2e
            raw = response.read()
            return int(response.status), json.loads(raw or b"{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            payload = json.loads(raw or b"{}")
        except ValueError:
            payload = {"raw": raw.decode("utf-8", errors="replace")[:300]}
        return int(exc.code), payload


# --------------------------------------------------------------------------- ABS stub
class AbsStub:
    """Minimal Audiobookshelf look-alike backed by a directory tree."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._items: dict[str, dict[str, Any]] = {}
        self.lock = threading.Lock()

    @staticmethod
    def _item_id(name: str) -> str:
        return "item-" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:10]

    @staticmethod
    def _episode_id(name: str) -> str:
        return "ep-" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:10]

    def scan(self) -> None:
        items: dict[str, dict[str, Any]] = {}
        if self.root.exists():
            for folder in sorted(self.root.iterdir()):
                if not folder.is_dir():
                    continue
                episodes = []
                for media in sorted(
                    path
                    for path in folder.iterdir()
                    if path.is_file() and path.suffix.lower() in AUDIO_SUFFIXES
                ):
                    episodes.append(
                        {
                            "id": self._episode_id(media.name),
                            "audioFile": {
                                "metadata": {"filename": media.name, "path": str(media)}
                            },
                        }
                    )
                item_id = self._item_id(folder.name)
                items[item_id] = {
                    "id": item_id,
                    "path": str(folder),
                    "media": {
                        "metadata": {"title": folder.name},
                        "episodes": episodes,
                    },
                }
        with self.lock:
            self._items = items

    def items(self) -> list[dict[str, Any]]:
        with self.lock:
            return [dict(item) for item in self._items.values()]

    def item(self, item_id: str) -> dict[str, Any] | None:
        with self.lock:
            return self._items.get(item_id)


class _AbsHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _reply(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: Any) -> None:
        self._reply(status, json.dumps(payload).encode("utf-8"), "application/json")

    def _text(self, status: int, text: str) -> None:
        self._reply(status, text.encode("utf-8"), "text/plain; charset=utf-8")

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        path = urllib.parse.urlsplit(self.path).path
        if re.fullmatch(r"/api/libraries/[^/]+/scan", path):
            self.server.abs_stub.scan()  # type: ignore[attr-defined]
            return self._text(200, "OK")
        if re.fullmatch(r"/api/items/[^/]+/scan", path):
            return self._text(200, "OK")
        self._read_body()
        return self._json(404, {"error": "not found"})

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        path = urllib.parse.urlsplit(self.path).path
        if re.fullmatch(r"/api/libraries/[^/]+/items", path):
            return self._json(200, {"results": self.server.abs_stub.items()})  # type: ignore[attr-defined]
        match = re.fullmatch(r"/api/items/([^/]+)", path)
        if match:
            item = self.server.abs_stub.item(urllib.parse.unquote(match.group(1)))  # type: ignore[attr-defined]
            if item is None:
                return self._json(404, {"error": "not found"})
            return self._json(200, item)
        return self._json(404, {"error": "not found"})

    def do_PATCH(self) -> None:  # noqa: N802 - http.server API
        self._read_body()
        path = urllib.parse.urlsplit(self.path).path
        if re.fullmatch(r"/api/podcasts/[^/]+/episode/[^/]+", path):
            return self._json(200, {})
        if re.fullmatch(r"/api/items/[^/]+/media", path):
            return self._json(200, {})
        return self._json(404, {"error": "not found"})

    def log_message(self, format: str, *args: Any) -> None:  # silence access log
        return


class _AbsServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class AbsStubServer:
    def __init__(self, root: Path) -> None:
        self.stub = AbsStub(root)
        self.httpd = _AbsServer(("127.0.0.1", 0), _AbsHandler)
        self.httpd.abs_stub = self.stub  # type: ignore[attr-defined]
        self.port = int(self.httpd.server_address[1])
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="abs-stub", daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


# --------------------------------------------------------------------------- webhook proxy
@dataclass
class Captured:
    payload: dict[str, Any]
    upstream_status: int
    message_id: str | None
    channel_id: str | None
    timestamp: str | None


@dataclass
class CaptureProxy:
    target: str
    records: list[Captured] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
    httpd: ThreadingHTTPServer | None = None
    thread: threading.Thread | None = None

    def _wait_url(self) -> str:
        parts = urllib.parse.urlsplit(self.target)
        query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        if not any(key == "wait" for key, _ in query):
            query.append(("wait", "true"))
        return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))

    def forward(self, body: bytes) -> tuple[int, bytes]:
        payload: dict[str, Any] = {}
        try:
            parsed = json.loads(body.decode("utf-8"))
            if isinstance(parsed, dict):
                payload = parsed
        except ValueError:
            pass

        request = urllib.request.Request(
            self._wait_url(),
            data=body,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "yt2abs-e2e-proxy/0.1",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=25) as response:  # noqa: S310 - discord.com
                status = int(response.status)
                raw = response.read()
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            raw = exc.read() or b""
        except (urllib.error.URLError, TimeoutError) as exc:
            raise ConnectionError(f"cannot reach Discord: {exc}") from exc

        message: dict[str, Any] = {}
        if raw:
            try:
                decoded = json.loads(raw)
                if isinstance(decoded, dict):
                    message = decoded
            except ValueError:
                message = {}

        with self.lock:
            self.records.append(
                Captured(
                    payload=payload,
                    upstream_status=status,
                    message_id=message.get("id"),
                    channel_id=message.get("channel_id"),
                    timestamp=message.get("timestamp"),
                )
            )
        return status, raw

    def start(self) -> None:
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:  # noqa: N802 - http.server API
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                try:
                    status, raw = proxy.forward(body)
                except ConnectionError as exc:
                    log(f"proxy error: {exc}")
                    status, raw = 502, json.dumps({"error": str(exc)}).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, format: str, *args: Any) -> None:
                return

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="webhook-proxy", daemon=True)

    @property
    def url(self) -> str:
        assert self.httpd is not None
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/webhook"

    def snapshot(self) -> list[Captured]:
        with self.lock:
            return list(self.records)

    def stop(self) -> None:
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()

    def start_thread(self) -> None:
        assert self.thread is not None
        self.thread.start()


# --------------------------------------------------------------------------- app helpers
def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_healthy(base_url: str, timeout: float = 40.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            status, payload = http_json(base_url + "/health", timeout=3)
            if status == 200:
                return payload
        except (urllib.error.URLError, OSError, ValueError) as exc:
            last = exc
        time.sleep(0.25)
    raise RuntimeError(f"service did not become healthy: {last}")


def post_job(base_url: str, video_url: str) -> dict[str, Any]:
    status, payload = http_json(base_url + "/api/jobs", method="POST", body={"url": video_url})
    if status not in (200, 202):
        raise RuntimeError(f"POST /api/jobs -> HTTP {status}: {payload}")
    return payload


def wait_job(base_url: str, video_id: str, expected: set[str], timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    seen: dict[str, Any] = {}
    while time.monotonic() < deadline:
        status, job = http_json(f"{base_url}/api/jobs/{video_id}", timeout=5)
        if status == 200:
            seen = job
            log(
                f"job {video_id}: status={job.get('status')} stage={job.get('stage')} "
                f"attempts={job.get('attempts')} error={(job.get('error') or '-')[:120]}"
            )
            if job.get("status") in expected:
                return job
        time.sleep(1.0)
    raise TimeoutError(f"job {video_id} did not reach {sorted(expected)}; last={seen}")


def fetch_message(target: str, message_id: str) -> dict[str, Any]:
    base = target.split("?", 1)[0].rstrip("/")
    url = f"{base}/messages/{urllib.parse.quote(message_id)}"
    request = urllib.request.Request(url, headers={"User-Agent": "yt2abs-e2e/0.1"})
    with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310 - discord.com
        return json.loads(response.read() or b"{}")


def payload_event(payload: dict[str, Any]) -> str | None:
    embeds = payload.get("embeds") or []
    title = ""
    if embeds and isinstance(embeds[0], dict):
        title = str(embeds[0].get("title") or "")
    for event, expected in EVENT_TITLES.items():
        if title == expected:
            return event
    return None


def build_config(path: Path, workdir: Path, abs_url: str) -> None:
    library = workdir / "library"
    library.mkdir(parents=True, exist_ok=True)
    (workdir / "data").mkdir(exist_ok=True)
    (workdir / "tmp").mkdir(exist_ok=True)
    config = {
        "defaults": {
            "retention_days": None,
            "protect_unfinished": True,
            "sponsorblock": "off",
            # Datacenter IPs get HTTP 403 from the default YouTube clients;
            # the embedded client still serves the audio stream.
            "ytdlp_args": ["--extractor-args", "youtube:player_client=web_embedded"],
        },
        "abs": {
            "url": abs_url,
            "api_key": "e2e-api-key",
            "library_id": "e2e-library",
            "library_path": str(library),
            "timeout": 15,
            "scan_timeout": 30,
            "scan_interval": 0.2,
        },
        "paths": {
            "data_dir": str(workdir / "data"),
            "tmp_dir": str(workdir / "tmp"),
        },
        "worker": {"max_attempts": 2, "retry_backoff_seconds": [2], "poll_interval": 0.2},
        "channels": {},
    }
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


class AppServer:
    def __init__(self, config_path: Path, port: int) -> None:
        from yt2abs.app import create_app
        from yt2abs.config import load_config

        self.port = port
        self.base_url = f"http://127.0.0.1:{port}"
        config = load_config(config_path)
        self.app = create_app(config=config, worker_enabled=True)
        self.server = None
        self.thread = None

    def start(self) -> None:
        import uvicorn

        uvicorn_config = uvicorn.Config(
            self.app,
            host="127.0.0.1",
            port=self.port,
            log_level="warning",
            access_log=False,
        )
        self.server = uvicorn.Server(uvicorn_config)
        self.thread = threading.Thread(target=self.server.run, name="uvicorn", daemon=True)
        self.thread.start()
        health = wait_healthy(self.base_url)
        log(f"/health: {json.dumps(health, ensure_ascii=False)}")

    def stop(self) -> None:
        if self.server is not None:
            self.server.should_exit = True
        if self.thread is not None:
            self.thread.join(timeout=15)


# --------------------------------------------------------------------------- main
def main() -> int:
    parser = argparse.ArgumentParser(description="yt2abs -> Discord E2E notification test")
    parser.add_argument("--report", type=Path, default=None, help="where to write the JSON report")
    parser.add_argument("--workdir", type=Path, default=None, help="evidence directory (.e2e/...)")
    parser.add_argument("--skip-cli", action="store_true", help="do not run the notify-test CLI step")
    args = parser.parse_args()

    webhook = (os.environ.get("DISCORD_WEBHOOK_URL") or "").strip()
    if not webhook or not webhook.startswith(("http://", "https://")):
        log("BLAD: ustaw DISCORD_WEBHOOK_URL (sekret) w srodowisku.")
        return 2

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    workdir = args.workdir or (REPO_ROOT / ".e2e" / f"discord-{stamp}")
    workdir.mkdir(parents=True, exist_ok=True)
    report_path = args.report or (workdir / "report.json")
    report_path.parent.mkdir(parents=True, exist_ok=True)

    # Make sure no stale ABS_* override from the caller leaks into the test config.
    for key in (
        "ABS_URL",
        "ABS_API_KEY",
        "ABS_LIBRARY_ID",
        "ABS_LIBRARY_PATH",
        "YT2ABS_LIBRARY_DIR",
        "YT2ABS_DATA_DIR",
        "YT2ABS_TMP_DIR",
    ):
        os.environ.pop(key, None)

    abs_stub = AbsStubServer(workdir / "library")
    proxy = CaptureProxy(webhook)
    abs_stub.start()
    proxy.start()
    proxy.start_thread()
    log(f"ABS stub: {abs_stub.url} (library={workdir / 'library'})")
    log(f"capture proxy: {proxy.url} -> discord.com (wait=true)")

    # The notifier reads everything from the environment, exactly like in production.
    os.environ["DISCORD_WEBHOOK_URL"] = proxy.url
    os.environ["DISCORD_NOTIFY_EVENTS"] = "all"
    os.environ["DISCORD_NOTIFY_TIMEOUT"] = "20"
    os.environ.pop("DISCORD_NOTIFY_DISABLED", None)
    os.environ.pop("NOTIFY_DISABLED", None)

    config_path = workdir / "config.yaml"
    build_config(config_path, workdir, abs_stub.url)

    checks: dict[str, bool] = {}
    report: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "workdir": str(workdir),
        "config": str(config_path),
        "notifications": [],
        "jobs": {},
        "checks": checks,
    }
    exit_code = 0
    app: AppServer | None = None
    try:
        app = AppServer(config_path, free_port())
        if not args.skip_cli:
            log("CLI: python -m yt2abs.cli notify-test")
            result = subprocess.run(
                [sys.executable, "-m", "yt2abs.cli", "notify-test", "--message",
                 "yt2abs E2E: test powiadomien Discord z tests/e2e_discord.py."],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            report["cli"] = {"exit": result.returncode, "stdout": result.stdout.strip(),
                             "stderr": result.stderr.strip()}
            log(f"CLI exit={result.returncode}: {result.stdout.strip() or result.stderr.strip()}")
            checks["cli_notify_test"] = result.returncode == 0

        app.start()
        checks["health_notifications_enabled"] = bool(
            wait_healthy(app.base_url).get("notifications", {}).get("enabled")
        )

        log(f"POST /api/jobs {VALID_VIDEO} (oczekiwany: imported)")
        valid_job = post_job(app.base_url, f"https://www.youtube.com/watch?v={VALID_VIDEO}")
        log(f"POST /api/jobs {INVALID_VIDEO} (oczekiwany: retry -> failed)")
        invalid_job = post_job(app.base_url, f"https://www.youtube.com/watch?v={INVALID_VIDEO}")

        report["jobs"]["valid"] = wait_job(app.base_url, valid_job["video_id"], {"imported", "failed"}, JOB_TIMEOUT)
        report["jobs"]["invalid"] = wait_job(app.base_url, invalid_job["video_id"], {"failed"}, JOB_TIMEOUT)
        checks["valid_job_imported"] = report["jobs"]["valid"].get("status") == "imported"
        checks["invalid_job_failed"] = report["jobs"]["invalid"].get("status") == "failed"

        # Notifications are sent synchronously by the worker before the state is polled,
        # but give the proxy a moment to flush its records.
        time.sleep(1.0)
        all_records = proxy.snapshot()
        records = [record for record in all_records if 200 <= record.upstream_status < 300]
        report["attempts_total"] = len(all_records)
        if len(records) != len(all_records):
            log(f"uwaga: {len(all_records) - len(records)} nieudana/-e proba/-y wysylki (retry Discorda)")

        events: list[str] = []
        for record in records:
            event = payload_event(record.payload) or "unknown"
            events.append(event)
            embeds = record.payload.get("embeds") or [{}]
            entry = {
                "event": event,
                "upstream_status": record.upstream_status,
                "message_id": record.message_id,
                "channel_id": record.channel_id,
                "timestamp": record.timestamp,
                "embed_title": (embeds[0] or {}).get("title"),
                "description": (embeds[0] or {}).get("description"),
            }
            if record.message_id:
                try:
                    remote = fetch_message(webhook, record.message_id)
                    entry["read_back"] = {
                        "id": remote.get("id"),
                        "channel_id": remote.get("channel_id"),
                        "timestamp": remote.get("timestamp"),
                        "content": remote.get("content"),
                        "embed_title": ((remote.get("embeds") or [{}])[0] or {}).get("title"),
                    }
                    entry["verified"] = (
                        remote.get("id") == record.message_id
                        and remote.get("channel_id") == record.channel_id
                        and ((remote.get("embeds") or [{}])[0] or {}).get("title")
                        == (embeds[0] or {}).get("title")
                    )
                except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as exc:
                    entry["verified"] = False
                    entry["read_back_error"] = f"{type(exc).__name__}: {exc}"
            else:
                entry["verified"] = False
            report["notifications"].append(entry)

        log(f"captured {len(records)} notification(s): {', '.join(events) or '-'}")
        for event in EVENT_TITLES:
            if event == "cli" and args.skip_cli:
                continue
            checks[f"delivered_{event}"] = event in events
        checks["all_messages_verified"] = bool(report["notifications"]) and all(
            item.get("verified") for item in report["notifications"]
        )
        channel_ids = {item.get("channel_id") for item in report["notifications"]}
        checks["single_channel"] = len(channel_ids) == 1 and None not in channel_ids
        checks["no_read_back_errors"] = all(
            "read_back_error" not in item for item in report["notifications"]
        )
        report["channel_id"] = report["notifications"][0].get("channel_id") if report["notifications"] else None
    except Exception as exc:  # noqa: BLE001 - report every failure instead of a traceback only
        exit_code = 1
        report["error"] = f"{type(exc).__name__}: {exc}"
        log(f"BLAD: {report['error']}")
    finally:
        if app is not None:
            app.stop()
        proxy.stop()
        abs_stub.stop()
        checks_pass = exit_code == 0 and bool(checks) and all(checks.values())
        report["passed"] = checks_pass
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        log(f"raport: {report_path}")
        for name, ok in checks.items():
            log(f"  {'PASS' if ok else 'FAIL'}  {name}")
        if not checks_pass:
            exit_code = 1

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
