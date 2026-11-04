"""Offline E2E: unlisted playlist -> queue -> ABS -> retention.

The real FastAPI app, worker, import pipeline and retention job run against a
fake ``yt-dlp`` executable and an Audiobookshelf double backed by the real
filesystem - no network and no yt-dlp installation are needed.

Scenarios
---------
A. playlist enabled (2 importable videos + 1 failing one):
   1. the first poll at startup queues every unknown id with source=playlist,
   2. a second poll (``poll_once``, the same call the thread makes) queues
      nothing: queued/imported/failed ids are never touched again,
   3. a manual ``POST /api/jobs`` still works and stays source=api,
   4. removing a video from the playlist changes nothing in ABS/retention,
   5. retention still deletes the ``retention_days: 0`` episode and the next
      poll does not resurrect it (status stays ``deleted``),
   6. every poll used ``yt-dlp --flat-playlist`` exactly once.
B. ``playlist.enabled: false`` - zero yt-dlp listing calls, manual flow works.
C. ``YT2ABS_PLAYLIST_ENABLED=0`` - the env kill-switch wins over a configured
   and enabled playlist: again zero listing calls.

Run from the repository root:

    .venv/bin/python tests/e2e_playlist.py
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, cast

try:  # running as a module (python -m tests.e2e_playlist) or from the tests dir
    from tests.e2e_phase3 import FakeUpdater, ServerThread, StubAbs, http
except ImportError:  # running as a script: python tests/e2e_playlist.py
    from e2e_phase3 import FakeUpdater, ServerThread, StubAbs, http  # type: ignore[no-redef]

from yt2abs.app import create_app
from yt2abs.config import parse_config
from yt2abs.db import Database
from yt2abs.notify import DiscordNotifier
from yt2abs.ntfy import NtfyNotifier
from yt2abs.pipeline import ImportPipeline

PLAYLIST_URL = "https://www.youtube.com/playlist?list=PLplaylistE2E0123456789"

CH_ZERO = "UCzero000000000000000000"
CH_WEEK = "UCweek000000000000000000"

VID_ZERO = "plz00000001"   # retention_days: 0 -> deleted by retention
VID_WEEK = "plw00000001"   # retention_days: 7 -> stays imported
VID_FAIL = "plf00000001"   # download fails -> failed, never requeued
VID_MANUAL = "man00000001"  # marked by hand, not in the playlist

FAILURES: list[str] = []

FAKE_YTDLP = r'''#!/usr/bin/env python3
"""Fake yt-dlp: lists a playlist, emits metadata and writes a dummy audio file."""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

LOG = os.environ["FAKE_YTDLP_LOG"]
VIDEOS = json.loads(os.environ.get("FAKE_YTDLP_VIDEOS") or "{}")
PLAYLIST_FILE = os.environ.get("FAKE_PLAYLIST_FILE") or ""
args = sys.argv[1:]
with open(LOG, "a", encoding="utf-8") as handle:
    print(json.dumps({"argv": args}), file=handle)

if "--flat-playlist" in args:
    if os.environ.get("FAKE_LISTING_FAIL") == "1":
        print(
            "ERROR: [youtube:tab] PLplaylistE2E0123456789: "
            "The playlist does not exist.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    ids = []
    if PLAYLIST_FILE and Path(PLAYLIST_FILE).exists():
        ids = [line.strip() for line in Path(PLAYLIST_FILE).read_text().splitlines() if line.strip()]
    for video_id in ids:
        print(video_id)
    # yt-dlp prints NA for an entry it cannot resolve; the poller must drop it.
    print("NA")
    raise SystemExit(0)

url = next((arg for arg in reversed(args) if not arg.startswith("-")), "")
match = re.search(r"(?:v=|youtu\.be/|shorts/|embed/)([A-Za-z0-9_-]{6,})", url)
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
        "description": "Opis testowy E2E playlisty",
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


def read_log(path: Path) -> list[list[str]]:
    if not path.exists():
        return []
    return [json.loads(line)["argv"] for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def listing_calls(calls: list[list[str]]) -> list[list[str]]:
    return [argv for argv in calls if "--flat-playlist" in argv]


def download_calls(calls: list[list[str]], video_id: str) -> list[list[str]]:
    return [argv for argv in calls if "--output" in argv and any(video_id in arg for arg in argv)]


def wait_for(base: str, video_id: str, status: str, timeout: float = 60.0) -> dict[str, Any]:
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


def wait_for_file(path: Path, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.05)
    return False


def write_playlist(path: Path, ids: list[str]) -> None:
    path.write_text("".join(f"{video_id}\n" for video_id in ids), encoding="utf-8")


def build_config(
    root: Path,
    *,
    ytdlp: Path,
    playlist: dict[str, Any],
    channels: dict[str, Any] | None = None,
) -> Any:
    return parse_config(
        {
            "defaults": {"retention_days": 7, "protect_unfinished": False, "sponsorblock": False},
            "abs": {
                "url": "http://abs.stub:13378",
                "api_key": "stub-key",
                "library_id": "lib-playlist",
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
            "playlist": playlist,
            "channels": channels or {},
        }
    )


def start_app(config: Any) -> tuple[Any, Any, str]:
    db = Database(config.paths.database_path)
    stub = StubAbs(Path(config.library_path))
    pipeline = ImportPipeline(config, db, cast(Any, stub), avatar_fetcher=lambda *a, **k: None)
    app = create_app(
        config,
        db=db,
        pipeline=pipeline,
        worker_enabled=True,
        notifier=DiscordNotifier(""),
        ntfy_notifier=NtfyNotifier("", ""),
        updater=cast(Any, FakeUpdater()),
    )
    server = ServerThread(app)
    base = server.start()
    return app, server, base


def count_jobs(base: str) -> dict[str, int]:
    code, payload = http("GET", f"{base}/api/jobs", timeout=30)
    if code != 200:
        return {}
    return dict(payload.get("counts") or {})


# ------------------------------------------------------------------ scenarios
def scenario_enabled(root: Path, ytdlp: Path, playlist_file: Path, log_path: Path) -> None:
    print("\n=== A. playlista włączona (2 filmy + 1 błąd) ===")
    os.environ["FAKE_YTDLP_LOG"] = str(log_path)
    config = build_config(
        root,
        ytdlp=ytdlp,
        playlist={"enabled": True, "url": PLAYLIST_URL, "interval_minutes": 5},
        channels={
            CH_ZERO: {"name": "Playlista zero", "retention_days": 0},
            CH_WEEK: {"name": "Playlista tydzień", "retention_days": 7},
        },
    )
    app, server, base = start_app(config)
    try:
        code, health = http("GET", f"{base}/health")
        check(code == 200, "GET /health odpowiada", f"HTTP {code}")
        check("playlist" not in health, "/health nie ujawnia stanu playlisty", str(health))
        code, system = http("GET", f"{base}/api/system")
        playlist_state = system.get("playlist") or {}
        check(code == 200, "GET /api/system odpowiada", f"HTTP {code}")
        check(playlist_state.get("enabled") is True, "/api/system: poller działa", str(playlist_state))
        check(playlist_state.get("configured") is True, "/api/system: playlista skonfigurowana")
        check(playlist_state.get("interval_minutes") == 5, "/api/system: interwał 5 min")
        check(PLAYLIST_URL not in json.dumps(system), "/api/system nie ujawnia URL playlisty")

        # 1) first poll at startup queues every id and the worker imports them
        zero = wait_for(base, VID_ZERO, "imported")
        week = wait_for(base, VID_WEEK, "imported")
        failed = wait_for(base, VID_FAIL, "failed")
        check(zero.get("source") == "playlist", "film 1 zakolejkowany ze źródłem playlist", str(zero.get("source")))
        check(week.get("source") == "playlist", "film 2 zakolejkowany ze źródłem playlist", str(week.get("source")))
        check(failed.get("source") == "playlist", "błędny film też ze źródłem playlist")
        check(bool(zero.get("abs_episode_id") and week.get("abs_episode_id")),
              "oba filmy mają odcinki w ABS")
        zero_file = Path(str(zero.get("file_path") or ""))
        week_file = Path(str(week.get("file_path") or ""))
        check(zero_file.is_file() and week_file.is_file(), "pliki audio w bibliotece")

        # 2) a manual mark still works and dedupes against the poller
        code, manual = http("POST", f"{base}/api/jobs", {"url": f"https://youtu.be/{VID_MANUAL}"})
        check(code == 202, "ręczne oznaczenie nowego filmu działa", f"HTTP {code}")
        manual_job = wait_for(base, VID_MANUAL, "imported")
        check(manual_job.get("source") == "api", "ręczne oznaczenie ma źródło api",
              str(manual_job.get("source")))
        code, duplicate = http("POST", f"{base}/api/jobs", {"url": f"https://youtu.be/{VID_WEEK}"})
        check(code == 200 and duplicate.get("created") is False,
              "ręczne oznaczenie filmu z playlisty nie dubluje", f"HTTP {code} {duplicate.get('status')}")

        counts = count_jobs(base)
        check(counts.get("imported") == 3 and counts.get("failed") == 1,
              "stan kolejki: 3 imported + 1 failed", str(counts))

        # 3) a second poll (same call the thread makes) changes nothing
        poller = getattr(app.state, "playlist_poller", None)
        check(poller is not None and getattr(app.state, "playlist_thread") is not None,
              "aplikacja uruchomiła poller w tle")
        assert poller is not None
        second = poller.poll_once()
        check(second.seen == 3 and second.queued == [] and second.skipped == 3,
              "drugi tick nie dubluje (seen=3, queued=0, skipped=3)", str(second.as_dict()))
        check(count_jobs(base) == counts, "drugi tick nie zmienił stanu kolejki")
        failed_after = http("GET", f"{base}/api/jobs/{VID_FAIL}")[1]
        check(failed_after.get("status") == "failed", "failed nie jest re-kolejkowany")
        calls = read_log(log_path)
        check(len(download_calls(calls, VID_WEEK)) == 1,
              "importowany film nie jest pobierany drugi raz",
              str(len(download_calls(calls, VID_WEEK))))

        # 4) removing a video from the playlist is a no-op for ABS/retention
        write_playlist(playlist_file, [VID_ZERO, VID_FAIL])
        removed = poller.poll_once()
        check(removed.seen == 2 and removed.queued == [],
              "usunięcie z playlisty nie kolejkuje niczego", str(removed.as_dict()))
        week_after = http("GET", f"{base}/api/jobs/{VID_WEEK}")[1]
        check(week_after.get("status") == "imported" and week_file.is_file(),
              "usunięcie z playlisty nie usuwa odcinka z ABS")

        # 5) retention still deletes the retention_days: 0 episode
        # ``?wait=true`` is the synchronous variant meant for E2E/debugging;
        # without it the endpoint returns 202 and the pass runs in a thread.
        code, report = http("POST", f"{base}/api/retention/run?wait=true", timeout=120)
        deleted_ids = [item.get("video_id") for item in (report or {}).get("deleted", [])]
        check(code == 200, "ręczny przebieg retencji", f"HTTP {code}")
        check(deleted_ids == [VID_ZERO], "retencja usunęła tylko politykę 0 dni", str(deleted_ids))
        check(not zero_file.exists(), "plik usunięty z dysku", str(zero_file))
        check(http("GET", f"{base}/api/jobs/{VID_ZERO}")[1].get("status") == "deleted",
              "status deleted w bazie")
        check(http("GET", f"{base}/api/jobs/{VID_WEEK}")[1].get("status") == "imported",
              "film z polityką 7 dni zachowany")

        # 6) the deleted video is still in the playlist -> must not come back
        write_playlist(playlist_file, [VID_ZERO, VID_WEEK, VID_FAIL])
        after_delete = poller.poll_once()
        check(after_delete.seen == 3 and after_delete.queued == [],
              "deleted nie jest re-kolejkowany przez poller", str(after_delete.as_dict()))
        check(http("GET", f"{base}/api/jobs/{VID_ZERO}")[1].get("status") == "deleted",
              "status deleted pozostaje po kolejnym ticku")
        check(count_jobs(base).get("queued", 0) == 0, "kolejka pusta po wszystkich tickach")

        # 7) every poll listed exactly once (startup + 3 manual polls)
        listings = listing_calls(read_log(log_path))
        check(len(listings) == 4, "4 wywołania --flat-playlist (start + 3 ticki)", str(len(listings)))
        check(all(argv[-1] == PLAYLIST_URL for argv in listings),
              "każde listowanie dostało URL playlisty")
        check(all("--flat-playlist" in argv and "--print" in argv for argv in listings),
              "listowanie bez pobierania (--flat-playlist)")
    finally:
        server.stop()


def scenario_disabled(root: Path, ytdlp: Path, playlist_file: Path, log_path: Path) -> None:
    print("\n=== B. playlista wyłączona w configu ===")
    os.environ["FAKE_YTDLP_LOG"] = str(log_path)
    write_playlist(playlist_file, [VID_ZERO, VID_WEEK])
    config = build_config(
        root,
        ytdlp=ytdlp,
        playlist={"enabled": False, "url": PLAYLIST_URL},
        channels={CH_ZERO: {"name": "Playlista zero", "retention_days": 7}},
    )
    app, server, base = start_app(config)
    try:
        time.sleep(0.5)
        calls = read_log(log_path)
        check(listing_calls(calls) == [], "zerowe listowanie przy playlist.enabled=false",
              str(len(listing_calls(calls))))
        check(getattr(app.state, "playlist_thread", None) is None, "brak wątku pollera")
        check(count_jobs(base).get("queued", 0) == 0, "kolejka pusta (poller nic nie dodał)")

        code, _ = http("POST", f"{base}/api/jobs", {"url": f"https://youtu.be/{VID_MANUAL}"})
        check(code == 202, "ręczne oznaczenie nadal działa", f"HTTP {code}")
        wait_for(base, VID_MANUAL, "imported")
        check(len(listing_calls(read_log(log_path))) == 0,
              "oznaczenie ręczne nie uruchomiło listowania")
    finally:
        server.stop()


def scenario_env_switch(root: Path, ytdlp: Path, playlist_file: Path, log_path: Path) -> None:
    print("\n=== C. kill-switch YT2ABS_PLAYLIST_ENABLED=0 ===")
    os.environ["FAKE_YTDLP_LOG"] = str(log_path)
    write_playlist(playlist_file, [VID_ZERO, VID_WEEK])
    config = build_config(
        root,
        ytdlp=ytdlp,
        playlist={"enabled": True, "url": PLAYLIST_URL, "interval_minutes": 5},
    )
    os.environ["YT2ABS_PLAYLIST_ENABLED"] = "0"
    app, server, base = start_app(config)
    try:
        time.sleep(0.5)
        check(listing_calls(read_log(log_path)) == [], "kill-switch blokuje listowanie")
        check(getattr(app.state, "playlist_thread", None) is None, "kill-switch nie startuje wątku")
        playlist_state = http("GET", f"{base}/api/system")[1].get("playlist") or {}
        check(playlist_state.get("enabled") is False and playlist_state.get("configured") is True,
              "/api/system: wyłączone przez env, mimo skonfigurowanego URL", str(playlist_state))
    finally:
        server.stop()
        os.environ.pop("YT2ABS_PLAYLIST_ENABLED", None)


def scenario_listing_error(root: Path, ytdlp: Path, playlist_file: Path, log_path: Path) -> None:
    print("\n=== D. błąd listowania: tylko warning, bez wycieku, samonaprawa ===")
    os.environ["FAKE_YTDLP_LOG"] = str(log_path)
    os.environ["FAKE_LISTING_FAIL"] = "1"
    write_playlist(playlist_file, [VID_ZERO, VID_WEEK])
    config = build_config(
        root,
        ytdlp=ytdlp,
        playlist={"enabled": True, "url": PLAYLIST_URL, "interval_minutes": 5},
    )
    app, server, base = start_app(config)
    try:
        health: dict[str, Any] = {}
        system: dict[str, Any] = {}
        report = None
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            _, system = http("GET", f"{base}/api/system")
            report = (system.get("playlist") or {}).get("last_report")
            if report is not None:
                break
            time.sleep(0.05)
        check(report is not None, "pierwszy tick zapisał raport błędu", str(report))
        check(bool(report) and isinstance(report.get("error"), str) and bool(report.get("error")),
              "/api/system: błąd listowania jako tekst", str(report))
        check(PLAYLIST_URL not in json.dumps(system), "/api/system: brak URL przy błędzie")
        check("PLplaylistE2E" not in json.dumps(system), "/api/system: brak ID playlisty przy błędzie")
        _, health = http("GET", f"{base}/health")
        check("playlist" not in health, "/health: brak stanu playlisty przy błędzie", str(health))

        error_text = ((system.get("playlist") or {}).get("last_report") or {}).get("error")
        check("PLplaylistE2E" not in json.dumps(system),
              "/api/system: brak ID/URL nawet w szczegółach", str(error_text))
        check(bool(error_text) and "<playlist>" in str(error_text),
              "/api/system: ID zredagowane", str(error_text))

        thread = getattr(app.state, "playlist_thread", None)
        check(thread is not None and thread.is_alive(), "wątek przeżył błąd listowania")
        check(count_jobs(base).get("queued", 0) == 0, "błąd nie zakolejkował śmieci")

        # next tick after the failure recovers without a restart
        os.environ["FAKE_LISTING_FAIL"] = "0"
        result = app.state.playlist_poller.poll_once()
        check(result.error is None and result.seen == 2,
              "kolejny tick po błędzie listuje normalnie", str(result.as_dict()))
        wait_for(base, VID_ZERO, "imported")
        wait_for(base, VID_WEEK, "imported")
        check(count_jobs(base).get("imported") == 2,
              "po samonaprawie oba filmy trafiły do ABS", str(count_jobs(base)))
    finally:
        server.stop()
        os.environ.pop("FAKE_LISTING_FAIL", None)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="yt2abs-playlist-") as tmp:
        root = Path(tmp)
        fake = root / "fake-yt-dlp"
        fake.write_text(FAKE_YTDLP, encoding="utf-8")
        fake.chmod(0o755)

        videos = {
            VID_ZERO: {"channel_id": CH_ZERO, "channel": "Playlista zero", "title": "Film zero"},
            VID_WEEK: {"channel_id": CH_WEEK, "channel": "Playlista tydzień", "title": "Film tydzień"},
            VID_FAIL: {
                "channel_id": CH_WEEK,
                "channel": "Playlista tydzień",
                "title": "Film z błędem",
                "fail_download": True,
            },
            VID_MANUAL: {"channel_id": CH_WEEK, "channel": "Playlista tydzień", "title": "Film ręczny"},
        }
        os.environ["FAKE_YTDLP_VIDEOS"] = json.dumps(videos)
        os.environ["FAKE_PLAYLIST_FILE"] = str(root / "playlist.txt")
        for name in ("ABS_API_KEY", "NTFY_TOPIC", "NTFY_URL", "DISCORD_WEBHOOK_URL",
                     "YT2ABS_PLAYLIST_ENABLED"):
            os.environ.pop(name, None)

        write_playlist(root / "playlist.txt", [VID_ZERO, VID_WEEK, VID_FAIL])
        scenario_enabled(root / "enabled", fake, root / "playlist.txt", root / "enabled.log")
        scenario_disabled(root / "disabled", fake, root / "playlist.txt", root / "disabled.log")
        scenario_env_switch(root / "envswitch", fake, root / "playlist.txt", root / "envswitch.log")
        scenario_listing_error(
            root / "listing-error", fake, root / "playlist.txt", root / "listing-error.log"
        )

    if FAILURES:
        print(f"\n{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
