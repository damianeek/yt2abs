"""Phase 3 regression E2E: mark -> ABS -> retention with SponsorBlock + ntfy.

The test runs the real FastAPI app, the real worker thread, the real import
pipeline and the real retention job.  Only the outside world is replaced:

* a fake ``yt-dlp`` executable records every argv and writes a dummy ``.m4a``
  (so the real ``build_audio_args``/``download`` path is exercised, including
  ``--sponsorblock-*`` flags chosen per channel),
* a stub Audiobookshelf client indexes the library directory on the real
  filesystem (scan/find/edit/delete semantics of ABS 2.37.1),
* a local HTTP sink receives ntfy publishes.

Scenarios
---------
A. features enabled (channel policies + ntfy topic):
   1. ``POST /api/jobs`` -> worker imports the file -> ABS episode edited,
   2. sponsorblock ``remove``/``mark`` reach yt-dlp, ``off`` stays clean,
   3. a failing job lands in ``failed`` and publishes one ntfy ``failed``,
   4. retention deletes the ``retention_days: 0`` episode and publishes one
      ntfy ``deleted``; a ``null`` channel policy keeps its episode,
   5. a second retention pass publishes nothing (dedupe).
B. features disabled (defaults ``sponsorblock: off``, empty ``NTFY_TOPIC``):
   the same mark -> ABS -> retention flow still works, yt-dlp gets no
   ``--sponsorblock-*`` flag and the sink receives zero requests.

Run from the repository root (no network, no yt-dlp, no ABS needed):

    .venv/bin/python tests/e2e_phase3.py
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from yt2abs.abs_client import AbsNotFound
from yt2abs.app import create_app
from yt2abs.config import parse_config
from yt2abs.db import Database
from yt2abs.notify import DiscordNotifier
from yt2abs.ntfy import NtfyNotifier
from yt2abs.pipeline import ImportPipeline

CH_REMOVE = "UCremove00000000000000000"
CH_MARK = "UCmark000000000000000000"
CH_KEEP = "UCkeep000000000000000000"

VIDEO_REMOVE = "rem00000001"
VIDEO_MARK = "mrk00000001"
VIDEO_KEEP = "kep00000001"
VIDEO_BAD = "bad00000001"
VIDEO_FAILDL = "dld00000001"

TOPIC = "yt2abs-faza3-e2e"
FAILURES: list[str] = []

FAKE_YTDLP = '''#!/usr/bin/env python3
"""Fake yt-dlp: records argv, emits metadata and writes a dummy audio file."""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

LOG = os.environ["FAKE_YTDLP_LOG"]
VIDEOS = json.loads(os.environ["FAKE_YTDLP_VIDEOS"])
args = sys.argv[1:]
with open(LOG, "a", encoding="utf-8") as handle:
    handle.write(json.dumps({"argv": args}) + "\\n")

url = next((arg for arg in reversed(args) if not arg.startswith("-")), "")
match = re.search(r"(?:v=|youtu\\.be/|shorts/|embed/)([A-Za-z0-9_-]{6,})", url)
video_id = match.group(1) if match else ""
video = VIDEOS.get(video_id)

if "--dump-single-json" in args:
    if video is None:
        print(f"ERROR: [youtube] {video_id}: Video unavailable", file=sys.stderr)
        raise SystemExit(1)
    print(json.dumps({
        "id": video_id,
        "channel_id": video["channel_id"],
        "channel": video["channel"],
        "channel_url": f"https://www.youtube.com/channel/{video['channel_id']}",
        "title": video["title"],
        "description": "Opis testowy E2E",
        "upload_date": "20261019",
        "duration": 600,
        "chapters": [],
        "thumbnails": [],
    }))
    raise SystemExit(0)

if "--simulate" in args:
    if video is None:
        raise SystemExit(1)
    print(f"{video['channel_id']}|{video_id}")
    raise SystemExit(0)

if video is None:
    print(f"ERROR: [youtube] {video_id}: Video unavailable", file=sys.stderr)
    raise SystemExit(1)

if video.get("fail_download"):
    print(f"ERROR: [youtube] {video_id}: unable to download video data: HTTP 403", file=sys.stderr)
    raise SystemExit(1)

template = args[args.index("--output") + 1]
name = template
name = name.replace("%(upload_date>%Y-%m-%d)s", "2026-10-19")
name = name.replace("%(title).200B", video["title"])
name = name.replace("%(title)s", video["title"])
name = name.replace("%(id)s", video_id)
name = name.replace("%(ext)s", "m4a")
if "%(" in name:
    print(f"ERROR: unsupported output template {template!r}", file=sys.stderr)
    raise SystemExit(1)
path = Path(name)
path.parent.mkdir(parents=True, exist_ok=True)
path.write_bytes(b"fake-m4a-audio")
print(path)
'''


# --------------------------------------------------------------------- helpers
def check(condition: bool, label: str, detail: str = "") -> bool:
    ok = bool(condition)
    print(("PASS " if ok else "FAIL ") + label + (f" | {detail}" if detail else ""))
    if not ok:
        FAILURES.append(label)
    return ok


class NtfySink(BaseHTTPRequestHandler):
    requests: list[dict[str, Any]] = []

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw.decode("utf-8"))
        except ValueError:
            payload = {"_raw": raw.decode("utf-8", errors="replace")}
        self.__class__.requests.append({"path": self.path, "payload": payload})
        body = b'{"id":"msg"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass


class StubAbs:
    """Audiobookshelf double backed by the real library directory."""

    def __init__(self, library_path: Path) -> None:
        self.library_path = Path(library_path)
        self.scans: list[str] = []
        self.item_scans: list[str] = []
        self.updates: list[dict[str, Any]] = []
        self.podcasts: list[dict[str, Any]] = []
        self.deleted: list[tuple[str, str, bool]] = []
        self.progress: dict[str, Any] = {}

    # -- state ---------------------------------------------------------
    def _items(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        if not self.library_path.exists():
            return items
        for folder in sorted(path for path in self.library_path.iterdir() if path.is_dir()):
            episodes = []
            for number, audio in enumerate(sorted(folder.glob("*.m4a")), start=1):
                episodes.append(
                    {
                        "id": f"ep-{folder.name}-{number}",
                        "title": audio.stem,
                        "pubDate": None,
                        "description": "",
                        "audioFile": {"metadata": {"filename": audio.name, "path": str(audio)}},
                    }
                )
            items.append(
                {
                    "id": f"item-{folder.name}",
                    "path": str(folder),
                    "metadata": {"title": folder.name},
                    "media": {"episodes": episodes},
                }
            )
        return items

    def _find(self, item_id: str) -> dict[str, Any] | None:
        return next((item for item in self._items() if item["id"] == item_id), None)

    # -- ABS API used by the pipeline ----------------------------------
    def get_library(self, library_id: str) -> dict[str, Any]:
        """Cheap preflight used by the pipeline before downloading."""
        return {"id": library_id, "name": "Library", "mediaType": "podcast"}

    def scan_library(self, library_id: str) -> None:
        self.scans.append(library_id)

    def scan_item(self, item_id: str) -> None:
        self.item_scans.append(item_id)

    def list_library_items(self, library_id: str) -> list[dict[str, Any]]:
        return self._items()

    def find_item_by_path(
        self, library_id: str, path: str, *, items: list[dict[str, Any]] | None = None
    ) -> dict[str, Any] | None:
        candidates = items if items is not None else self._items()
        for item in candidates:
            if Path(str(item.get("path"))).resolve() == Path(path).resolve():
                return item
        return None

    def get_item(self, item_id: str) -> dict[str, Any]:
        item = self._find(item_id)
        if item is None:
            raise AbsNotFound(f"item {item_id} not found")
        return item

    def find_episode_by_filename(
        self, item_id: str, filename: str, *, item: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        payload = item if item is not None else self.get_item(item_id)
        for episode in payload.get("media", {}).get("episodes", []):
            metadata = (episode.get("audioFile") or {}).get("metadata") or {}
            if metadata.get("filename") == filename:
                return episode
        return None

    @staticmethod
    def find_episode_by_id(item: dict[str, Any], episode_id: str) -> dict[str, Any] | None:
        for episode in item.get("media", {}).get("episodes", []):
            if episode.get("id") == episode_id:
                return episode
        return None

    def update_episode(
        self,
        item_id: str,
        episode_id: str,
        *,
        title: str | None = None,
        description: str | None = None,
        pub_date: str | None = None,
        published_at_ms: int | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        self.updates.append(
            {
                "item_id": item_id,
                "episode_id": episode_id,
                "title": title,
                "description": description,
                "pub_date": pub_date,
                "published_at_ms": published_at_ms,
                **extra,
            }
        )
        return {"id": episode_id}

    def edit_podcast(self, item_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.podcasts.append({"item_id": item_id, "payload": payload})
        return payload

    def get_media_progress(self, item_id: str, episode_id: str) -> Any:
        return self.progress.get(episode_id)

    def delete_episode(self, item_id: str, episode_id: str, *, hard: bool = True) -> None:
        item = self._find(item_id)
        if item is None:
            raise AbsNotFound(f"item {item_id} not found")
        episode = self.find_episode_by_id(item, episode_id)
        if episode is None:
            raise AbsNotFound(f"episode {episode_id} not found")
        path = Path(str(((episode.get("audioFile") or {}).get("metadata") or {}).get("path") or ""))
        if path.is_file():
            path.unlink()
        self.deleted.append((item_id, episode_id, hard))


class FakeUpdater:
    """No-op yt-dlp updater (the E2E must not touch PyPI)."""

    current_version = "fake-2026.10.19"
    last_result = None

    def run_forever(self, stop_event: threading.Event, interval_seconds: float) -> None:
        stop_event.wait(interval_seconds)

    def version_of(self) -> str:
        return self.current_version


class ServerThread:
    """Run the ASGI app on a free 127.0.0.1 port."""

    def __init__(self, app: Any) -> None:
        import uvicorn

        config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self) -> str:
        self.thread.start()
        deadline = time.monotonic() + 30
        while not self.server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("uvicorn did not start")
            time.sleep(0.02)
        sockets = self.server.servers[0].sockets
        return f"http://127.0.0.1:{sockets[0].getsockname()[1]}"

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=30)


def http(method: str, url: str, body: Any = None, timeout: float = 60.0) -> tuple[int, Any]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status), json.loads(response.read().decode("utf-8") or "null")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            return int(exc.code), json.loads(raw or "null")
        except json.JSONDecodeError:
            return int(exc.code), raw


def wait_for(base: str, video_id: str, status: str, timeout: float = 90.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        code, last = http("GET", f"{base}/api/jobs/{video_id}")
        if code == 200 and last.get("status") == status:
            return last
        if code == 200 and last.get("status") == "failed" and status != "failed":
            raise RuntimeError(f"{video_id} failed unexpectedly: {last.get('error')}")
        time.sleep(0.1)
    raise TimeoutError(f"{video_id} did not reach {status} within {timeout}s: {last}")


def wait_for_requests(count: int, timeout: float = 15.0) -> bool:
    """Wait until the ntfy sink has at least ``count`` requests.

    The worker marks a job ``failed`` *before* the notification hook runs, so a
    status poll can win the race against the HTTP publish; retention publishes
    synchronously but uses the same helper for symmetry.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if len(NtfySink.requests) >= count:
            return True
        time.sleep(0.02)
    return False


def read_ytdlp_log(path: Path) -> list[list[str]]:
    if not path.exists():
        return []
    calls = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            calls.append(json.loads(line)["argv"])
    return calls


def download_calls(calls: list[list[str]], video_id: str) -> list[list[str]]:
    return [
        argv
        for argv in calls
        if "--output" in argv and any(video_id in arg for arg in argv)
    ]


def first_download_flags(calls: list[list[str]], video_id: str) -> list[str] | None:
    matches = download_calls(calls, video_id)
    return sb_flags(matches[0]) if matches else None



def sb_flags(argv: list[str]) -> list[str]:
    flags = []
    for index, arg in enumerate(argv):
        if arg in ("--sponsorblock-remove", "--sponsorblock-mark"):
            flags.append(f"{arg} {argv[index + 1] if index + 1 < len(argv) else ''}".strip())
    return flags


def build_config(
    root: Path,
    *,
    ytdlp: Path,
    channels: dict[str, Any],
    defaults: dict[str, Any] | None = None,
) -> Any:
    data: dict[str, Any] = {
        "defaults": {
            "retention_days": 0,
            "protect_unfinished": False,
            "sponsorblock": False,
            **(defaults or {}),
        },
        "abs": {
            "url": "http://abs.stub:13378",
            "api_key": "stub-key",
            "library_id": "lib-faza3",
            "library_path": str(root / "library"),
            "scan_timeout": 10,
            "scan_interval": 0.02,
        },
        "paths": {
            "data_dir": str(root / "data"),
            "tmp_dir": str(root / "tmp"),
            "library_path": str(root / "library"),
        },
        "worker": {"max_attempts": 1, "retry_backoff_seconds": [1], "poll_interval": 0.05},
        "retention": {"interval_minutes": 60, "run_on_startup": False},
        "ytdlp": {"update": False, "command": str(ytdlp)},
        "channels": channels,
    }
    return parse_config(data)


def scenario_enabled(root: Path, ytdlp: Path, sink_url: str, log_path: Path) -> None:
    print("\n=== A. funkcje włączone (polityki kanałów + ntfy) ===")
    os.environ["FAKE_YTDLP_LOG"] = str(log_path)
    config = build_config(
        root,
        ytdlp=ytdlp,
        channels={
            CH_REMOVE: {"name": "Kanał remove", "retention_days": 0, "sponsorblock": "remove"},
            CH_MARK: {"name": "Kanał mark", "retention_days": None, "sponsorblock": "mark:intro"},
            CH_KEEP: {"name": "Kanał keep", "retention_days": None, "sponsorblock": "off"},
        },
    )
    db = Database(config.paths.database_path)
    stub = StubAbs(Path(config.library_path))
    pipeline = ImportPipeline(config, db, stub, avatar_fetcher=lambda *a, **k: None)
    ntfy = NtfyNotifier(sink_url, TOPIC, min_interval=0, timeout=5)
    app = create_app(
        config,
        db=db,
        pipeline=pipeline,
        worker_enabled=True,
        notifier=DiscordNotifier(""),
        ntfy_notifier=ntfy,
        updater=FakeUpdater(),
    )
    server = ServerThread(app)
    base = server.start()
    try:
        code, health = http("GET", f"{base}/health")
        check(code == 200, "GET /health odpowiada", f"HTTP {code}")
        check(
            (health.get("ntfy") or {}).get("enabled") is True
            and {"failed", "deleted"} <= set((health.get("ntfy") or {}).get("events") or []),
            "/health: ntfy włączone dla failed/deleted",
            str(health.get("ntfy")),
        )
        check(
            TOPIC not in json.dumps(health),
            "/health nie ujawnia tematu ntfy",
        )

        # 1) mark -> worker -> ABS
        code, payload = http("POST", f"{base}/api/jobs", {"url": f"https://youtu.be/{VIDEO_REMOVE}"})
        check(code == 202, "oznaczenie filmu kolejkuje zadanie", f"HTTP {code} {payload}")
        imported = wait_for(base, VIDEO_REMOVE, "imported")
        check(bool(imported.get("abs_item_id") and imported.get("abs_episode_id")),
              "zadanie zaimportowane z id ABS", str(imported.get("abs_item_id")))
        check(stub.scans == ["lib-faza3"], "pipeline wywołał scan biblioteki ABS")
        check(len(stub.updates) == 1 and stub.updates[0]["item_id"] == imported["abs_item_id"],
              "odcinek zaktualizowany w ABS (tytuł/opis/data)")
        update = stub.updates[0] if stub.updates else {}
        check(bool(update.get("title")) and "youtube.com/watch" in str(update.get("description")),
              "opis odcinka zawiera link do źródła", str(update.get("description"))[:80])
        check(bool(update.get("pub_date", "")), "pubDate przekazany do ABS", str(update.get("pub_date")))
        check(stub.podcasts and stub.podcasts[0]["payload"].get("author") == "Kanał remove",
              "metadane podcastu ustawione (autor = kanał)")
        file_path = Path(str(imported.get("file_path") or ""))
        check(file_path.is_file(), "plik audio w bibliotece", str(file_path))
        check(file_path.parent.name == "Kanał remove", "folder kanału z config.name", file_path.parent.name)

        calls = read_ytdlp_log(log_path)
        remove_args = download_calls(calls, VIDEO_REMOVE)
        check(len(remove_args) == 1, "yt-dlp wywołany raz na film", str(len(remove_args)))
        check(first_download_flags(calls, VIDEO_REMOVE) == ["--sponsorblock-remove sponsor"],
              "kanał remove -> --sponsorblock-remove sponsor",
              str(first_download_flags(calls, VIDEO_REMOVE)))
        check(bool(remove_args) and "--embed-metadata" in remove_args[0]
              and "--embed-thumbnail" in remove_args[0],
              "embed metadanych/okładki/rozdziałów nadal działa")

        # 2) channel with mark + keep-forever policy
        code, _ = http("POST", f"{base}/api/jobs", {"url": f"https://youtu.be/{VIDEO_MARK}"})
        check(code == 202, "drugie oznaczenie zakolejkowane", f"HTTP {code}")
        mark_job = wait_for(base, VIDEO_MARK, "imported")
        calls = read_ytdlp_log(log_path)
        check(first_download_flags(calls, VIDEO_MARK) == ["--sponsorblock-mark intro"],
              "kanał mark:intro -> --sponsorblock-mark intro",
              str(first_download_flags(calls, VIDEO_MARK)))
        check(mark_job.get("retention", {}).get("keep") is True,
              "polityka null = zachowaj na stałe widoczna w API", str(mark_job.get("retention")))

        # 3) explicit off must not add sponsorblock flags
        code, _ = http("POST", f"{base}/api/jobs", {"url": f"https://youtu.be/{VIDEO_KEEP}"})
        check(code == 202, "trzecie oznaczenie zakolejkowane", f"HTTP {code}")
        keep_job = wait_for(base, VIDEO_KEEP, "imported")
        calls = read_ytdlp_log(log_path)
        check(first_download_flags(calls, VIDEO_KEEP) == [], "kanał off -> brak flag SponsorBlock",
              str(first_download_flags(calls, VIDEO_KEEP)))
        code, duplicate = http("POST", f"{base}/api/jobs", {"url": f"https://youtu.be/{VIDEO_KEEP}"})
        check(code == 200 and duplicate.get("created") is False,
              "ponowne oznaczenie nie tworzy duplikatu", f"HTTP {code}")

        # 4) failure -> ntfy failed
        before_failed = len(NtfySink.requests)
        code, _ = http("POST", f"{base}/api/jobs", {"url": f"https://youtu.be/{VIDEO_BAD}"})
        check(code in (200, 202), "błędny film zakolejkowany", f"HTTP {code}")
        failed = wait_for(base, VIDEO_BAD, "failed")
        check(bool(failed.get("error")), "błąd zapisany w zadaniu", str(failed.get("error"))[:80])
        check(wait_for_requests(before_failed + 1), "ntfy: powiadomienie failed dotarło")
        failed_posts = NtfySink.requests[before_failed:]
        check(len(failed_posts) == 1, "ntfy: dokładnie jedno powiadomienie failed",
              str(len(failed_posts)))
        if failed_posts:
            body = failed_posts[0]["payload"]
            check(body.get("topic") == TOPIC, "ntfy: topic w payloadzie")
            check("Kanał" in body.get("title", "") or "failed" in body.get("title", ""),
                  "ntfy failed: tytuł opisuje błąd", str(body.get("title")))
            check(VIDEO_BAD in body.get("message", ""), "ntfy failed: video_id w treści")

        # 4b) failure after metadata -> the channel title is known
        code, _ = http("POST", f"{base}/api/jobs", {"url": f"https://youtu.be/{VIDEO_FAILDL}"})
        check(code in (200, 202), "film z błędem pobierania zakolejkowany", f"HTTP {code}")
        wait_for(base, VIDEO_FAILDL, "failed")
        check(wait_for_requests(before_failed + 2), "ntfy: powiadomienie failed po metadanych dotarło")
        dl_posts = NtfySink.requests[before_failed + 1:]
        check(len(dl_posts) == 1, "ntfy: failed po metadanych wysłany raz", str(len(dl_posts)))
        if dl_posts:
            check("Kanał remove" in str(dl_posts[0]["payload"].get("title")),
                  "ntfy failed: tytuł zawiera kanał", str(dl_posts[0]["payload"].get("title")))

        # 5) retention deletes policy=0, keeps null, publishes one deleted
        before_deleted = len(NtfySink.requests)
        code, report = http("POST", f"{base}/api/retention/run", timeout=120)
        check(code == 200, "ręczny przebieg retencji", f"HTTP {code}")
        deleted_ids = [item.get("video_id") for item in (report or {}).get("deleted", [])]
        check(deleted_ids == [VIDEO_REMOVE], "retencja usunęła tylko politykę 0 dni", str(deleted_ids))
        check(len(stub.deleted) == 1 and stub.deleted[0][2] is True,
              "ABS hard delete wywołany", str(stub.deleted))
        check(not file_path.exists(), "plik usunięty z dysku", str(file_path))
        check(http("GET", f"{base}/api/jobs/{VIDEO_REMOVE}")[1].get("status") == "deleted",
              "status deleted w bazie")
        check(http("GET", f"{base}/api/jobs/{VIDEO_KEEP}")[1].get("status") == "imported",
              "kanał keep zachowany")
        check(wait_for_requests(before_deleted + 1), "ntfy: powiadomienie deleted dotarło")
        deleted_posts = NtfySink.requests[before_deleted:]
        check(len(deleted_posts) == 1, "ntfy: dokładnie jedno powiadomienie deleted",
              str(len(deleted_posts)))
        if deleted_posts:
            body = deleted_posts[0]["payload"]
            check(VIDEO_REMOVE in body.get("message", ""), "ntfy deleted: video_id w treści")
            check("retention" in body.get("message", ""), "ntfy deleted: retention reason")

        # 6) second pass: nothing new (dedupe + idempotent retention)
        before_second = len(NtfySink.requests)
        http("POST", f"{base}/api/retention/run", timeout=120)
        check(len(NtfySink.requests) == before_second,
              "powtórna retencja nie wysyła duplikatów", str(len(NtfySink.requests) - before_second))

        # 7) success/retry never reach ntfy
        titles = [str(item["payload"].get("title", "")) for item in NtfySink.requests]
        check(len(titles) == 3, "ntfy: dwie wiadomości failed + jedna deleted", str(len(titles)))
        check(
            all(title.startswith(("❌", "🗑")) for title in titles),
            "ntfy dostaje wyłącznie failed/deleted",
            str(titles),
        )
    finally:
        server.stop()


def scenario_disabled(root: Path, ytdlp: Path, sink_url: str, log_path: Path) -> None:
    print("\n=== B. funkcje wyłączone (sponsorblock off + pusty NTFY_TOPIC) ===")
    os.environ["FAKE_YTDLP_LOG"] = str(log_path)
    config = build_config(
        root,
        ytdlp=ytdlp,
        channels={CH_REMOVE: {"name": "Kanał domyślny", "retention_days": 0}},
    )
    db = Database(config.paths.database_path)
    stub = StubAbs(Path(config.library_path))
    pipeline = ImportPipeline(config, db, stub, avatar_fetcher=lambda *a, **k: None)
    ntfy = NtfyNotifier(sink_url, "", min_interval=0)
    app = create_app(
        config,
        db=db,
        pipeline=pipeline,
        worker_enabled=True,
        notifier=DiscordNotifier(""),
        ntfy_notifier=ntfy,
        updater=FakeUpdater(),
    )
    server = ServerThread(app)
    base = server.start()
    try:
        before = len(NtfySink.requests)
        code, health = http("GET", f"{base}/health")
        check(code == 200 and (health.get("ntfy") or {}).get("enabled") is False,
              "/health: ntfy wyłączone bez topicu", str(health.get("ntfy")))

        code, _ = http("POST", f"{base}/api/jobs", {"url": f"https://youtu.be/{VIDEO_REMOVE}"})
        check(code == 202, "oznaczenie działa bez dodatków", f"HTTP {code}")
        imported = wait_for(base, VIDEO_REMOVE, "imported")
        calls = read_ytdlp_log(log_path)
        check(first_download_flags(calls, VIDEO_REMOVE) == [],
              "domyślne off -> brak flag SponsorBlock",
              str(first_download_flags(calls, VIDEO_REMOVE)))

        code, report = http("POST", f"{base}/api/retention/run", timeout=120)
        check(code == 200 and len((report or {}).get("deleted", [])) == 1,
              "retencja nadal usuwa wg polityki 0 dni", str((report or {}).get("deleted")))
        check(http("GET", f"{base}/api/jobs/{VIDEO_REMOVE}")[1].get("status") == "deleted",
              "status deleted bez ntfy")
        check(len(NtfySink.requests) == before,
              "brak topicu -> zero żądań HTTP do ntfy",
              str(len(NtfySink.requests) - before))
        check(Path(str(imported.get("file_path") or "")).exists() is False,
              "plik usunięty także przy wyłączonym ntfy")
    finally:
        server.stop()


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 0), NtfySink)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    sink_url = f"http://127.0.0.1:{server.server_address[1]}"
    requests_log = NtfySink.requests

    with tempfile.TemporaryDirectory(prefix="yt2abs-faza3-") as tmp:
        root = Path(tmp)
        videos = {
            VIDEO_REMOVE: {"channel_id": CH_REMOVE, "channel": "Kanał remove", "title": "Film remove"},
            VIDEO_MARK: {"channel_id": CH_MARK, "channel": "Kanał mark", "title": "Film mark"},
            VIDEO_KEEP: {"channel_id": CH_KEEP, "channel": "Kanał keep", "title": "Film keep"},
            VIDEO_FAILDL: {
                "channel_id": CH_REMOVE,
                "channel": "Kanał remove",
                "title": "Film z błędem pobierania",
                "fail_download": True,
            },
        }
        os.environ["FAKE_YTDLP_LOG"] = str(root / "ytdlp.log")
        os.environ["FAKE_YTDLP_VIDEOS"] = json.dumps(videos)
        for name in ("ABS_API_KEY", "NTFY_TOPIC", "NTFY_URL", "DISCORD_WEBHOOK_URL"):
            os.environ.pop(name, None)

        fake = root / "fake-yt-dlp"
        fake.write_text(FAKE_YTDLP, encoding="utf-8")
        fake.chmod(0o755)

        try:
            scenario_enabled(root / "enabled", fake, sink_url, root / "enabled.log")
            print(f"\nntfy sink: {len(requests_log)} żądań w scenariuszu A")
            scenario_disabled(root / "disabled", fake, sink_url, root / "disabled.log")
        finally:
            server.shutdown()
            server.server_close()

    if FAILURES:
        print(f"\n{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
