"""End-to-end test for ntfy error notifications (local HTTP sink, no network).

Scenarios:
1. no NTFY_TOPIC (also the compose default) -> disabled, zero HTTP requests,
2. NTFY_URL with a topic path -> rejected, zero HTTP requests,
3. 301/302 -> refused, the token never reaches the redirect target,
4. 200 HTML without an ntfy ack -> not delivered,
5. forced failed -> one POST with channel title, video id and reason (<60 s),
6. retention bulk delete -> one summary POST for N deletions,
7. repeated events -> dedupe, no duplicate messages.

Run from the repository root:

    .venv/bin/python tests/e2e_ntfy.py
"""
from __future__ import annotations

import json
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping

from yt2abs.config import parse_config
from yt2abs.db import Database
from yt2abs.notify import FanoutNotifier
from yt2abs.ntfy import NtfyNotifier
from yt2abs.retention import RetentionJob
from yt2abs.worker import Worker

VIDEO_FAILED = "dQw4w9WgXcQ"
NOW = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
TOPIC = "yt2abs-e2e"
TOKEN = "tk_e2e_secret"
MAX_LATENCY = 60.0
FAILURES: list[str] = []


class SinkHandler(BaseHTTPRequestHandler):
    """Tiny ntfy stand-in: only ``POST /`` is accepted."""

    mode = "ok"
    redirect_to = ""
    requests: list[dict[str, Any]] = []

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw.decode("utf-8"))
        except ValueError:
            payload = {"_raw": raw.decode("utf-8", errors="replace")}
        self.__class__.requests.append(
            {
                "path": self.path,
                "payload": payload,
                "content_type": self.headers.get("Content-Type"),
                "authorization": self.headers.get("Authorization"),
                "at": time.time(),
            }
        )
        if self.path != "/":
            # A topic in the URL publishes raw JSON as text: never acknowledge.
            self._respond(404, "text/plain; charset=utf-8", b"not a publish endpoint")
            return
        if self.mode == "redirect":
            self.send_response(302)
            self.send_header("Location", self.redirect_to)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.mode == "html":
            self._respond(200, "text/html; charset=utf-8", b"<html>ntfy web</html>")
            return
        ack = {
            "id": f"msg-{len(self.requests)}",
            "event": "message",
            "topic": payload.get("topic"),
        }
        self._respond(200, "application/json", json.dumps(ack).encode())

    def _respond(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        pass


def start_sink(
    mode: str = "ok", redirect_to: str = ""
) -> tuple[ThreadingHTTPServer, str, type[SinkHandler]]:
    handler = type(
        "SinkHandlerInstance",
        (SinkHandler,),
        {"mode": mode, "redirect_to": redirect_to, "requests": []},
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}", handler


class StubAbs:
    """Minimal Audiobookshelf client for the retention path."""

    def __init__(self) -> None:
        self.deleted: list[tuple[str, str, bool]] = []
        self.items = {
            "item-1": {
                "id": "item-1",
                "path": "/youtube-podcasts/Channel",
                "media": {
                    "episodes": [
                        {
                            "id": f"ep-{index}",
                            "title": f"Episode ep-{index}",
                            "audioFile": {
                                "metadata": {
                                    "filename": f"bulk{index}.m4a",
                                    "path": f"/youtube-podcasts/Channel/bulk{index}.m4a",
                                }
                            },
                        }
                        for index in range(1, 4)
                    ]
                },
            }
        }

    def list_library_items(self, library_id: str) -> list[dict[str, Any]]:
        return [json.loads(json.dumps(item)) for item in self.items.values()]

    def get_item(self, item_id: str) -> dict[str, Any]:
        return json.loads(json.dumps(self.items[item_id]))

    @staticmethod
    def find_episode_by_id(item: dict[str, Any], episode_id: str) -> dict[str, Any] | None:
        for episode in item.get("media", {}).get("episodes", []):
            if episode.get("id") == episode_id:
                return episode
        return None

    def get_media_progress(self, item_id: str, episode_id: str):
        return None

    def delete_episode(self, item_id: str, episode_id: str, *, hard: bool = True) -> None:
        episodes = self.items[item_id]["media"]["episodes"]
        self.items[item_id]["media"]["episodes"] = [
            episode for episode in episodes if episode.get("id") != episode_id
        ]
        self.deleted.append((item_id, episode_id, hard))


def check(condition: bool, label: str, detail: str = "") -> None:
    if condition:
        print(f"PASS {label}")
        return
    FAILURES.append(label)
    print(f"FAIL {label}{(': ' + detail) if detail else ''}")


def make_config(data_dir: Path) -> Any:
    return parse_config(
        {
            "defaults": {"retention_days": 0, "protect_unfinished": False},
            "abs": {
                "url": "http://abs.local:13378",
                "api_key": "key",
                "library_id": "lib-1",
                "library_path": "/youtube-podcasts",
            },
            "paths": {
                "data_dir": str(data_dir),
                "tmp_dir": str(data_dir / "tmp"),
                "library_path": "/youtube-podcasts",
            },
            "channels": {},
        }
    )


def main() -> int:
    servers: list[ThreadingHTTPServer] = []
    tmp = tempfile.TemporaryDirectory()
    try:
        # 1) No topic (compose default) -> no HTTP calls at all.
        ok_server, base, ok_sink = start_sink()
        servers.append(ok_server)
        print(f"Sink: {base}")
        disabled = NtfyNotifier.from_env({"NTFY_URL": base, "NTFY_TOPIC": ""})
        job = {"video_id": VIDEO_FAILED, "title": "x", "channel_title": "Kanał"}
        disabled.job_failed(job, "boom", 3)
        disabled.job_deleted(job, "retencja")
        check(len(ok_sink.requests) == 0, "brak configu = zero żądań")

        # 2) NTFY_URL with a topic path is rejected before any request.
        path_notifier = NtfyNotifier(f"{base}/{TOPIC}", TOPIC, min_interval=0)
        check(not path_notifier.configured, "NTFY_URL ze ścieżką odrzucony")
        check(
            "path" in (path_notifier.config_error or "")
            and "NTFY_TOPIC" in (path_notifier.config_error or ""),
            "komunikat wskazuje NTFY_TOPIC",
        )
        path_notifier.send("hello")
        check(len(ok_sink.requests) == 0, "odrzucony URL = zero żądań")

        # 3) Redirects are refused; the token must not reach another host.
        steal_server, steal_base, steal_sink = start_sink()
        servers.append(steal_server)
        redirect_server, redirect_base, redirect_sink = start_sink(
            mode="redirect", redirect_to=f"{steal_base}/"
        )
        servers.append(redirect_server)
        redirecter = NtfyNotifier(
            redirect_base, TOPIC, token=TOKEN, min_interval=0, retries=0
        )
        result = redirecter.send("hello")
        check(not result.delivered, "302 nie jest liczony jako dostarczony")
        check("redirect" in (result.error or ""), "błąd mówi o przekierowaniu")
        check(
            len(redirect_sink.requests) == 1
            and redirect_sink.requests[0]["authorization"] == f"Bearer {TOKEN}",
            "pierwszy POST miał token",
        )
        check(len(steal_sink.requests) == 0, "token nie wyciekł na inny host")
        check(TOKEN not in (result.error or ""), "błąd nie zawiera tokenu")

        # 4) A 2xx HTML page without the ntfy ack is not delivered.
        html_server, html_base, html_sink = start_sink(mode="html")
        servers.append(html_server)
        html_result = NtfyNotifier(
            html_base, TOPIC, min_interval=0, retries=0
        ).send("hello")
        check(not html_result.delivered, "200 HTML to nie dostarczenie")
        check(
            "acknowledgement" in (html_result.error or ""),
            "błąd mówi o braku potwierdzenia ntfy",
        )
        check(len(html_sink.requests) == 1, "HTML sink dostał jedno żądanie")

        # 5) Forced permanent failure -> exactly one POST.
        db = Database(Path(tmp.name) / "e2e-ntfy.sqlite3")
        db.initialize()
        notifier = NtfyNotifier(base, TOPIC, min_interval=0, timeout=5)
        db.create_or_requeue(VIDEO_FAILED)
        db.update_video(
            VIDEO_FAILED,
            title="Film E2E",
            channel_title="Kanał E2E",
        )

        def failing_process(_job: Mapping[str, Any], report) -> Any:
            report("download")
            raise RuntimeError("wymuszony błąd E2E")

        worker = Worker(
            db,
            failing_process,
            max_attempts=1,
            retry_backoff=(1,),
            poll_interval=0.01,
            wait=lambda seconds: None,
            notifier=FanoutNotifier([notifier]),
        )
        started = time.perf_counter()
        worker.run_once()
        latency = time.perf_counter() - started
        row = db.get_video(VIDEO_FAILED)
        assert row is not None
        check(row["status"] == "failed", "wymuszony failed zapisany w bazie")
        check(len(ok_sink.requests) == 1, "failed wysłany dokładnie raz")
        check(latency < MAX_LATENCY, f"failed dotarł w {latency:.2f}s (<60s)")
        if ok_sink.requests:
            first = ok_sink.requests[0]
            payload = first["payload"]
            check(first["path"] == "/", "POST trafił na root serwera (JSON publish)")
            check(payload.get("topic") == TOPIC, "payload zawiera topic")
            check(first["content_type"] == "application/json", "Content-Type JSON")
            check("Kanał E2E" in payload.get("title", ""), "tytuł zawiera kanał")
            check(VIDEO_FAILED in payload.get("message", ""), "treść zawiera video id")
            check(
                "wymuszony błąd E2E" in payload.get("message", ""),
                "treść zawiera powód",
            )
        check(
            [item["payload"].get("title") for item in ok_sink.requests]
            == ["❌ yt2abs: import failed — Kanał E2E"],
            "tytuł wiadomości failed",
        )

        # 6) Bulk retention delete -> one summary POST, not one per episode.
        old = (
            (NOW - timedelta(days=2))
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )
        for index in range(1, 4):
            video_id = f"bulk{index}"
            db.create_or_requeue(video_id)
            db.update_video(
                video_id,
                status="imported",
                channel_id="UC_e2e",
                channel_title="Kanał retencji",
                title=f"Odcinek {index}",
                added_at=old,
                file_path=f"/youtube-podcasts/Channel/{video_id}.m4a",
                abs_item_id="item-1",
                abs_episode_id=f"ep-{index}",
            )
        retention = RetentionJob(
            make_config(Path(tmp.name) / "retention"),
            db,
            StubAbs(),  # type: ignore[arg-type]
            now=lambda: NOW,
            notifier=notifier,
        )
        before = len(ok_sink.requests)
        started = time.perf_counter()
        report = retention.run_once()
        latency = time.perf_counter() - started
        check(len(report.deleted) == 3, "retencja usunęła trzy odcinki")
        check(
            len(ok_sink.requests) == before + 1,
            "hurtowe usunięcie = jedna wiadomość zbiorcza",
        )
        check(latency < MAX_LATENCY, f"deleted dotarł w {latency:.2f}s (<60s)")
        batch = ok_sink.requests[-1]["payload"]
        check("3 odcinki" in batch.get("title", ""), "tytuł zawiera liczbę odcinków")
        check(batch.get("message", "").count("•") == 3, "treść wymienia trzy odcinki")
        check("Deleted: 3 episodes" in batch.get("message", ""), "body contains the summary")
        check("Kanał retencji" in batch.get("message", ""), "treść zawiera kanał")
        check(
            "/youtube-podcasts/Channel" not in batch.get("message", ""),
            "treść nie zawiera pełnych ścieżek plików",
        )

        # 7) Repeated notifications for the same event are suppressed.
        count = len(ok_sink.requests)
        notifier.job_failed(row, "powtórka", 1)
        retention.run_once()
        check(
            len(ok_sink.requests) == count,
            "powtórzone zdarzenia nie spamują (dedupe / brak nowych usunięć)",
        )

        # 8) No success notifications, even when Discord-like hooks are absent.
        check(
            not hasattr(notifier, "job_imported") and not hasattr(notifier, "job_retry"),
            "brak powiadomień o sukcesach/retry",
        )
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        tmp.cleanup()

    if FAILURES:
        print(f"\n{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
