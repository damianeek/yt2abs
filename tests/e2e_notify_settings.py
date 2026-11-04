"""Live E2E: notification events configured in the WebUI drive real Discord delivery.

Unlike ``tests/e2e_discord.py`` this check does not download anything: the
worker runs for real (SQLite queue, retries, Discord webhook) but the pipeline
is a stub that can either fail or "import" instantly. That makes the run fast
and deterministic while still proving the part this card is about:

* ``PUT /api/notifications`` changes which operations are delivered,
* the worker honours that selection immediately (no restart),
* the selection is stored in SQLite and survives an application restart,
* every delivered message is read back **independently from Discord**
  (``GET /api/webhooks/{id}/{token}/messages/{message_id}``) - the notifier
  posts through a loopback proxy that adds ``?wait=true`` and captures the
  message id returned by Discord.

Requirements
------------
* ``DISCORD_WEBHOOK_URL`` in the environment (secret - never printed)

Usage
-----
    DISCORD_WEBHOOK_URL=... .venv/bin/python tests/e2e_notify_settings.py

Exit codes: 0 = all checks passed, 1 = a check failed, 2 = missing environment.
Evidence (config, log, message ids) stays in ``.e2e/notify-settings-<UTC>/``.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast

from fastapi.testclient import TestClient

from yt2abs.app import NOTIFY_EVENTS_SETTING, create_app
from yt2abs.config import parse_config
from yt2abs.db import Database
from yt2abs.notify import DiscordNotifier

REQUIRED_TITLES = {
    "failed": "❌ Import failed",
    "imported": "✅ Imported to Audiobookshelf",
}
JOB_TIMEOUT = float(os.environ.get("YT2ABS_E2E_TIMEOUT", "120"))


class Checks:
    def __init__(self) -> None:
        self.results: list[dict[str, Any]] = []

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.results.append({"check": name, "ok": bool(ok), "detail": detail})
        print(f"[e2e] {'PASS' if ok else 'FAIL'} {name}" + (f" - {detail}" if detail else ""))
        return ok

    @property
    def failed(self) -> int:
        return sum(1 for item in self.results if not item["ok"])


class StubPipeline:
    """Pipeline stand-in: fails or succeeds depending on ``mode``."""

    def __init__(self) -> None:
        self.mode = "fail"

    def process(self, job, report):
        report("downloading")
        if self.mode == "fail":
            raise RuntimeError("E2E: symulowany blad pipeline'u")
        video_id = str(job["video_id"])
        return {
            "title": "E2E: powiadomienia z WebUI",
            "channel_title": "yt2abs e2e",
            "file_path": f"/tmp/e2e/{video_id}.m4a",
        }


def webhook_base(url: str) -> str:
    return url.split("?", 1)[0].rstrip("/")


def read_message(webhook_url: str, message_id: str) -> dict[str, Any]:
    """Fetch one message straight from Discord (independent read-back)."""
    request = urllib.request.Request(
        f"{webhook_base(webhook_url)}/messages/{message_id}",
        headers={"User-Agent": "yt2abs-e2e/0.1"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310 - Discord API
        return json.loads(response.read() or b"{}")


def message_title(message: dict[str, Any]) -> str:
    embeds = message.get("embeds") or []
    if embeds:
        return str(embeds[0].get("title") or "")
    return str(message.get("content") or "")


class CaptureProxy:
    """Forwards webhook POSTs to Discord with ``?wait=true`` and records ids."""

    def __init__(self, target: str) -> None:
        self.target = webhook_base(target)
        self.records: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                request = urllib.request.Request(
                    proxy.target + "?wait=true",
                    data=raw,
                    headers={
                        "Content-Type": "application/json",
                        "User-Agent": "yt2abs-e2e/0.1",
                    },
                    method="POST",
                )
                status = 502
                body = b"{}"
                try:
                    with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310
                        status = int(response.status)
                        body = response.read() or b"{}"
                except urllib.error.HTTPError as exc:
                    status = int(exc.code)
                    body = exc.read() or b"{}"
                try:
                    returned = json.loads(body)
                except ValueError:
                    returned = {}
                with proxy._lock:
                    proxy.records.append(
                        {
                            "id": str(returned.get("id") or ""),
                            "channel_id": str(returned.get("channel_id") or ""),
                            "payload": json.loads(raw or b"{}"),
                            "forward_status": status,
                        }
                    )
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: Any) -> None:  # keep the e2e output clean
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(
            target=self.server.serve_forever, name="discord-capture", daemon=True
        )
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}/webhook"

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self.records)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def wait_until(predicate, *, timeout: float = JOB_TIMEOUT, interval: float = 0.3) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def wait_for_job(client: TestClient, video_id: str, expected: str) -> dict[str, Any]:
    last: dict[str, Any] = {}

    def done() -> bool:
        nonlocal last
        response = client.get(f"/api/jobs/{video_id}")
        if response.status_code != 200:
            return False
        last = response.json()
        return last.get("status") == expected

    if not wait_until(done):
        raise TimeoutError(f"{video_id}: status != {expected} (ostatni: {last.get('status')})")
    return last


def build_config(root: Path):
    return parse_config(
        {
            "defaults": {"retention_days": 14},
            "abs": {
                "url": "http://abs.invalid:13378",
                "api_key": "e2e",
                "library_id": "e2e-lib",
                "library_path": str(root / "library"),
            },
            "paths": {
                "data_dir": str(root / "data"),
                "tmp_dir": str(root / "tmp"),
                "library_path": str(root / "library"),
            },
            "worker": {
                "max_attempts": 1,
                "retry_backoff_seconds": [1],
                "poll_interval": 0.2,
            },
            "channels": {},
        }
    )


def main() -> int:
    webhook_url = (os.environ.get("DISCORD_WEBHOOK_URL") or "").strip()
    if not webhook_url:
        print("BLAD: brak DISCORD_WEBHOOK_URL w srodowisku.", file=sys.stderr)
        return 2

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    work = Path(".e2e") / f"notify-settings-{stamp}"
    work.mkdir(parents=True, exist_ok=True)
    checks = Checks()
    pipeline = StubPipeline()
    proxy = CaptureProxy(webhook_url)
    print(f"[e2e] start {stamp} (katalog dowodowy: {work})")

    notifier = DiscordNotifier.from_env(
        {**os.environ, "DISCORD_WEBHOOK_URL": proxy.url}
    )
    checks.check("notifier_points_at_capture_proxy", notifier.enabled, "webhook przez proxy 127.0.0.1")

    tmp = tempfile.TemporaryDirectory(prefix="yt2abs-e2e-")
    root = Path(tmp.name)
    config = build_config(root)
    db_path = root / "data" / "notify.sqlite3"
    app = create_app(
        config,
        db=Database(db_path),
        worker_enabled=True,
        pipeline=cast(Any, pipeline),
        notifier=notifier,
    )
    delivered: list[dict[str, Any]] = []

    with TestClient(app) as client:
        state = client.get("/api/notifications").json()
        checks.check(
            "state_starts_from_defaults",
            state["source"] == "default"
            and state["enabled"]
            and [e["name"] for e in state["events"] if e["enabled"]]
            == ["imported", "failed"],
            f"source={state['source']} enabled={state['enabled']}",
        )

        # ---------------------------------------------------------- failed only
        saved = client.put("/api/notifications", json={"events": ["failed"]})
        checks.check(
            "save_failed_only",
            saved.status_code == 200
            and [e["name"] for e in saved.json()["events"] if e["enabled"]] == ["failed"],
            f"HTTP {saved.status_code}",
        )
        pipeline.mode = "fail"
        failing = client.post("/api/jobs", json={"url": "https://youtu.be/e2e00000001"})
        checks.check("enqueue_failing_job", failing.status_code == 202, failing.text[:120])
        wait_for_job(client, "e2e00000001", "failed")
        wait_until(lambda: len(proxy.snapshot()) >= 1, timeout=30)
        time.sleep(0.5)
        batch = proxy.snapshot()
        delivered.extend(batch)
        checks.check(
            "failed_event_delivered_once",
            len(batch) == 1,
            f"{len(batch)} POST-ow do webhooka",
        )
        if batch:
            readback = read_message(webhook_url, batch[0]["id"])
            checks.check(
                "failed_message_read_back_from_discord",
                message_title(readback) == REQUIRED_TITLES["failed"]
                and str(readback.get("id")) == batch[0]["id"],
                f"id={batch[0]['id']} title={message_title(readback)!r}",
            )
            checks.check(
                "failed_message_has_no_retry",
                "Import retry" not in message_title(readback),
                "wybrane tylko failed - brak wiadomosci retry",
            )
            batch[0]["readback_title"] = message_title(readback)

        # ------------------------------------------------------------- no events
        empty = client.put("/api/notifications", json={"events": []})
        checks.check("save_empty_selection", empty.status_code == 200, f"HTTP {empty.status_code}")
        before = len(proxy.snapshot())
        failing2 = client.post("/api/jobs", json={"url": "https://youtu.be/e2e00000002"})
        checks.check("enqueue_failing_job_2", failing2.status_code == 202, failing2.text[:120])
        wait_for_job(client, "e2e00000002", "failed")
        time.sleep(2)
        checks.check(
            "no_delivery_when_nothing_selected",
            len(proxy.snapshot()) == before,
            f"{len(proxy.snapshot()) - before} nowych POST-ow",
        )

        # ------------------------------------------------------- imported only
        selected = client.put("/api/notifications", json={"events": ["imported"]})
        checks.check(
            "save_imported_only",
            selected.status_code == 200
            and [e["name"] for e in selected.json()["events"] if e["enabled"]] == ["imported"],
            f"HTTP {selected.status_code}",
        )
        pipeline.mode = "success"
        imported = client.post("/api/jobs", json={"url": "https://youtu.be/e2e00000003"})
        checks.check("enqueue_importing_job", imported.status_code == 202, imported.text[:120])
        wait_for_job(client, "e2e00000003", "imported")
        wait_until(lambda: len(proxy.snapshot()) >= before + 1, timeout=30)
        time.sleep(0.5)
        batch = proxy.snapshot()[before:]
        delivered.extend(batch)
        checks.check(
            "imported_event_delivered_once",
            len(batch) == 1,
            f"{len(batch)} POST-ow do webhooka",
        )
        if batch:
            readback = read_message(webhook_url, batch[0]["id"])
            checks.check(
                "imported_message_read_back_from_discord",
                message_title(readback) == REQUIRED_TITLES["imported"]
                and str(readback.get("id")) == batch[0]["id"],
                f"id={batch[0]['id']} title={message_title(readback)!r}",
            )
            checks.check(
                "failed_event_now_suppressed",
                "nie powiódł" not in message_title(readback),
                "wybrane tylko imported - brak wiadomosci failed",
            )
            batch[0]["readback_title"] = message_title(readback)

        page = client.get("/settings")
        checks.check(
            "settings_page_shows_saved_selection",
            page.status_code == 200
            and 'name="events" value="imported" checked' in page.text
            and 'name="events" value="failed">' in page.text,
            f"HTTP {page.status_code}",
        )

    # ------------------------------------------------------- restart: persistence
    persisted = Database(db_path).get_setting(NOTIFY_EVENTS_SETTING)
    checks.check("setting_stored_in_sqlite", persisted == "imported", f"wartosc={persisted!r}")

    fresh_notifier = DiscordNotifier.from_env(
        {**os.environ, "DISCORD_WEBHOOK_URL": proxy.url}
    )
    restarted = create_app(
        config,
        db=Database(db_path),
        worker_enabled=False,
        notifier=fresh_notifier,
    )
    with TestClient(restarted) as client:
        state = client.get("/api/notifications").json()
    enabled_names = [entry["name"] for entry in state["events"] if entry["enabled"]]
    checks.check(
        "selection_survives_restart",
        state["source"] == "webui"
        and enabled_names == ["imported"]
        and fresh_notifier.events == frozenset({"imported"}),
        f"source={state['source']} enabled={enabled_names}",
    )

    report = {
        "started_at": stamp,
        "channel_messages": [
            {
                "id": item["id"],
                "channel_id": item.get("channel_id"),
                "title": item.get("readback_title"),
                "forward_status": item.get("forward_status"),
            }
            for item in delivered
        ],
        "checks": checks.results,
        "failed": checks.failed,
        "total": len(checks.results),
    }
    (work / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        f"[e2e] {len(checks.results) - checks.failed}/{len(checks.results)} PASS, "
        f"raport: {work / 'report.json'}"
    )
    proxy.close()
    tmp.cleanup()
    return 0 if checks.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
