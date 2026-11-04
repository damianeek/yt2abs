#!/usr/bin/env python3
"""E2E regression for the pipeline edge cases found in review.

Runs inside the runtime image against a real Audiobookshelf and a real
YouTube download, and covers four findings:

1. ``sponsorblock: mute`` + ``--embed-thumbnail``: the published m4a keeps its
   ``attached_pic`` cover and the sponsor window is silent.
2. ABS gets both ``publishedAt`` (epoch ms from yt-dlp's ``timestamp``) and an
   RFC-822 ``pubDate``.
3. An ABS failure *after* the file was published is retried without a second
   download: one file, one episode, ``tmp_dir`` clean.
4. ``/add`` and ``/jobs/{id}`` are read-only; a retry is an explicit POST.

The script needs:
  ABS_API_KEY            (required, never printed)
  ABS_URL                default http://192.168.1.10:13380
  ABS_LIBRARY_ID         default cb857d8b-00da-4f74-a03e-45af3b2ff4f1
  YT2ABS_E2E_LIBRARY     default /youtube-podcasts (container view)
  YT2ABS_E2E_DATA        default /data
  YT2ABS_E2E_TMP         default /tmp-downloads
  YT2ABS_E2E_VIDEO       default dQw4w9WgXcQ
  YT2ABS_E2E_FOLDER      default "Review 1a"

Usage:
    ABS_API_KEY=... python3 tests/e2e_review1a.py
Exit code 0 when every check passes.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from email.utils import format_datetime
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from yt2abs.abs_client import AbsClient, AbsError  # noqa: E402,F401
from yt2abs.app import create_app  # noqa: E402
from yt2abs.config import Config, parse_config  # noqa: E402
from yt2abs.db import Database  # noqa: E402
from yt2abs.downloader import download_for_channel  # noqa: E402
from yt2abs.metadata import fetch_metadata  # noqa: E402
from yt2abs.pipeline import ImportPipeline  # noqa: E402
from yt2abs.worker import Worker  # noqa: E402

VIDEO = os.environ.get("YT2ABS_E2E_VIDEO", "dQw4w9WgXcQ")
CHANNEL_ID = "UCuAXFkgsw1L7xaCfnd5JJOw"
FOLDER = os.environ.get("YT2ABS_E2E_FOLDER", "Review 1a")
RETRY_FOLDER = FOLDER + " retry"
ABS_URL = os.environ.get("ABS_URL", "http://192.168.1.10:13380").rstrip("/")
ABS_KEY = os.environ.get("ABS_API_KEY", "")
ABS_LIBRARY_ID = os.environ.get("ABS_LIBRARY_ID", "cb857d8b-00da-4f74-a03e-45af3b2ff4f1")
LIBRARY = Path(os.environ.get("YT2ABS_E2E_LIBRARY", "/youtube-podcasts"))
DATA = Path(os.environ.get("YT2ABS_E2E_DATA", "/data"))
TMP = Path(os.environ.get("YT2ABS_E2E_TMP", "/tmp-downloads"))
PORT = int(os.environ.get("YT2ABS_E2E_PORT", "8099"))
# A window that is certainly inside the video and easy to measure.
SEGMENT = (30.0, 40.0)

failures: list[str] = []
checks = 0


def check(condition: bool, message: str) -> None:
    global checks
    checks += 1
    print(("PASS  " if condition else "FAIL  ") + message, flush=True)
    if not condition:
        failures.append(message)


def ffprobe_streams(path: Path) -> list[dict]:
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=index,codec_type,codec_name:stream_disposition=attached_pic",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return json.loads(out).get("streams", [])


def silencedetect(path: Path) -> list[tuple[float, float]]:
    result = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-i",
            str(path),
            "-map",
            "0:a:0",
            "-af",
            "silencedetect=noise=-50dB:d=1",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    spans: list[tuple[float, float]] = []
    start: float | None = None
    for line in result.stderr.splitlines():
        match = re.search(r"silence_start:\s*(-?[\d.]+)", line)
        if match:
            start = float(match.group(1))
            continue
        match = re.search(r"silence_end:\s*(-?[\d.]+)", line)
        if match and start is not None:
            spans.append((start, float(match.group(1))))
            start = None
    return spans


class SponsorBlockStub(BaseHTTPRequestHandler):
    """Serves one sponsor segment for every video hash."""

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        payload = [
            {
                "videoID": VIDEO,
                "category": "sponsor",
                "actionType": "skip",
                "segment": [SEGMENT[0], SEGMENT[1]],
                "UUID": "review1a-stub",
            }
        ]
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args, **kwargs) -> None:  # keep the output readable
        pass


def video_files(directory: Path, video_id: str) -> list[Path]:
    """Files named after ``[<video_id>]`` (glob would treat it as a class)."""
    if not directory.is_dir():
        return []
    marker = f"[{video_id}]"
    return sorted(path for path in directory.iterdir() if path.is_file() and marker in path.name)


def reset_media(directory: Path, video_id: str) -> int:
    """Remove this video's media so a rerun exercises a real download."""
    removed = 0
    for path in video_files(directory, video_id):
        try:
            path.unlink()
            removed += 1
        except OSError as exc:  # pragma: no cover - permissions on the share
            print(f"warning: could not remove {path}: {exc}")
    return removed


def clean_podcast(client: AbsClient, folder: str) -> int:
    """Hard-delete every episode of the test podcast so reruns stay exact."""
    path = str(LIBRARY / folder)
    try:
        item = client.find_item_by_path(ABS_LIBRARY_ID, path)
        if not item:
            return 0
        full = client.get_item(str(item["id"]))
        removed = 0
        for episode in (full.get("media") or {}).get("episodes") or []:
            try:
                client.delete_episode(str(item["id"]), str(episode["id"]), hard=True)
                removed += 1
            except AbsError as exc:  # pragma: no cover - ABS already gone
                print(f"warning: could not delete episode {episode.get('id')}: {exc}")
        return removed
    except AbsError as exc:  # pragma: no cover - ABS hiccup
        print(f"warning: could not clean podcast {folder}: {exc}")
        return 0


def queue_job(db: Database, video_id: str, source: str) -> dict | None:
    """Make sure ``video_id`` is claimable, whatever the previous run left."""
    row = db.get_video(video_id)
    if row is not None and row["status"] != "queued":
        db.update_video(video_id, status="failed", error="e2e restart")
    db.create_or_requeue(video_id, source=source)
    return db.get_video(video_id)


def build_config(folder: str, *, scan_timeout: float = 20.0, api: str) -> Config:
    return parse_config(
        {
            "defaults": {"retention_days": 14, "sponsorblock_api": api},
            "abs": {
                "url": ABS_URL,
                "api_key": ABS_KEY,
                "library_id": ABS_LIBRARY_ID,
                "library_path": str(LIBRARY),
                "scan_timeout": scan_timeout,
                "scan_interval": 0.5,
            },
            "paths": {
                "data_dir": str(DATA),
                "tmp_dir": str(TMP),
                "library_path": str(LIBRARY),
            },
            "channels": {
                CHANNEL_ID: {"name": folder, "sponsorblock": "mute:sponsor"},
            },
            "worker": {"max_attempts": 2, "retry_backoff_seconds": [1], "poll_interval": 0.1},
        }
    )


def http_get(url: str, timeout: float = 10.0) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return int(response.status or 0), response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read().decode("utf-8", "replace")


def main() -> int:
    import urllib.error  # noqa: F401 - used by http_get

    if not ABS_KEY:
        print("ABS_API_KEY is required", file=sys.stderr)
        return 2
    print(f"ABS {ABS_URL} library {ABS_LIBRARY_ID} · video {VIDEO} · folder {FOLDER}")

    stub = ThreadingHTTPServer(("127.0.0.1", 0), SponsorBlockStub)
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    api = f"http://127.0.0.1:{stub.server_address[1]}"
    print(f"SponsorBlock stub on {api} (segment {SEGMENT[0]}-{SEGMENT[1]}s)")

    db = Database(DATA / "review1a.sqlite3")
    db.initialize()

    # Metadata first: the expected publishedAt comes from yt-dlp itself.
    config = build_config(FOLDER, api=api)
    metadata = fetch_metadata(
        f"https://www.youtube.com/watch?v={VIDEO}",
        extra=config.defaults.ytdlp_args,
        ytdlp=config.ytdlp_command,
    )
    check(metadata.video_id == VIDEO, "metadata.video_id matches")
    timestamp = metadata.published_timestamp
    check(bool(timestamp), f"yt-dlp reported upload timestamp ({timestamp})")

    # ---------------------------------------------------------------- phase 1
    print("\n== phase 1: mute + embedded cover through the full pipeline ==")
    abs_client = AbsClient(ABS_URL, ABS_KEY, timeout=60)
    pipeline = ImportPipeline(config, db, abs_client)
    removed = clean_podcast(abs_client, FOLDER)
    stale = reset_media(LIBRARY / FOLDER, VIDEO) + reset_media(TMP, VIDEO)
    print(f"cleaned {removed} ABS episode(s) and {stale} file(s) before the run")
    job = queue_job(db, VIDEO, "e2e-review1a")
    assert job is not None
    worker = Worker(db, pipeline.process, max_attempts=1, retry_backoff=(1,), poll_interval=0.1)
    check(worker.run_once(), "worker claimed and processed the job")
    row = db.get_video(VIDEO)
    check(
        bool(row) and row["status"] == "imported",
        f"job status is imported (got {row['status'] if row else None})",
    )
    assert row is not None and row["file_path"]
    media = Path(row["file_path"])
    check(media.exists() and media.stat().st_size > 0, f"audio published: {media.name}")
    check((media.parent / "cover.jpg").exists(), "cover.jpg written next to the episode")

    streams = ffprobe_streams(media)
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    covers = [
        s
        for s in streams
        if s.get("codec_type") == "video"
        and (s.get("disposition") or {}).get("attached_pic") == 1
    ]
    check(bool(audio) and audio[0].get("codec_name") == "aac", "audio stream is aac")
    check(bool(covers), "embedded cover survives the mute step (attached_pic)")

    spans = silencedetect(media)
    window = [
        (start, end)
        for start, end in spans
        if abs(start - SEGMENT[0]) < 1.5 and abs(end - SEGMENT[1]) < 1.5
    ]
    check(bool(window), f"sponsor window {SEGMENT[0]}-{SEGMENT[1]}s is silent (found {spans})")

    # ---------------------------------------------------------------- phase 2
    print("\n== phase 2: ABS episode dates ==")
    item = abs_client.get_item(str(row["abs_item_id"]), expanded=True)
    episode = abs_client.find_episode_by_filename(
        str(row["abs_item_id"]), media.name, item=item
    )
    check(episode is not None, "ABS lists the episode by filename")
    if episode is not None:
        episode = abs_client.get_episode(str(row["abs_item_id"]), str(episode["id"]))
        expected_ms = int(timestamp) * 1000 if timestamp is not None else None
        expected_pub = (
            format_datetime(datetime.fromtimestamp(int(timestamp), tz=timezone.utc))
            if timestamp is not None
            else None
        )
        check(
            expected_ms is not None and int(episode.get("publishedAt") or 0) == expected_ms,
            f"publishedAt == {expected_ms} (ABS: {episode.get('publishedAt')})",
        )
        check(
            str(episode.get("pubDate") or "") == expected_pub,
            f"pubDate is RFC-822 (ABS: {episode.get('pubDate')!r})",
        )
        check(
            str(episode.get("title") or "") == metadata.title,
            "episode title matches the video",
        )

    # ---------------------------------------------------------------- phase 3
    print("\n== phase 3: retry after publish does not download again ==")
    retry_db = Database(DATA / "review1a-retry.sqlite3")
    retry_db.initialize()
    retry_config = build_config(RETRY_FOLDER, scan_timeout=5, api=api)
    retry_abs = AbsClient(ABS_URL, ABS_KEY, timeout=60)
    downloads = {"count": 0}

    def counting_downloader(*args, **kwargs):
        downloads["count"] += 1
        return download_for_channel(*args, **kwargs)

    class BrokenEpisodeLookup(AbsClient):
        def find_episode_by_filename(self, item_id, filename, *, item=None):
            return None

    removed = clean_podcast(retry_abs, RETRY_FOLDER)
    stale = reset_media(LIBRARY / RETRY_FOLDER, VIDEO) + reset_media(TMP, VIDEO)
    print(f"cleaned {removed} ABS episode(s) and {stale} file(s) so the first attempt must download")
    retry_job = queue_job(retry_db, VIDEO, "e2e-review1a-retry")
    assert retry_job is not None
    failing = ImportPipeline(
        retry_config,
        retry_db,
        BrokenEpisodeLookup(ABS_URL, ABS_KEY, timeout=60),
        downloader=counting_downloader,
    )
    failing_worker = Worker(retry_db, failing.process, max_attempts=1, retry_backoff=(1,))
    check(failing_worker.run_once(), "worker ran the failing attempt")
    row = retry_db.get_video(VIDEO)
    check(
        bool(row) and row["status"] == "failed",
        f"ABS import failure marks the job failed (got {row['status'] if row else None})",
    )
    check(
        bool(row) and "ABS nie znalazł odcinka" in str(row.get("error") or ""),
        f"failure reason recorded: {str(row.get('error') if row else '')[:80]}…",
    )

    published = video_files(LIBRARY / RETRY_FOLDER, VIDEO)
    check(len(published) == 1, f"exactly one published file for the video ({len(published)})")
    check(downloads["count"] == 1, "first attempt downloaded once")
    leftovers = [path.name for path in video_files(TMP, VIDEO)]
    check(not leftovers, f"tmp_dir cleaned after the failure ({leftovers})")
    check(bool(row) and bool(row.get("file_path")), "file_path recorded in the database")

    action = retry_db.create_or_requeue(VIDEO)
    check(action[1] == "requeued", "failed job can be requeued")
    retrying = ImportPipeline(
        retry_config, retry_db, retry_abs, downloader=counting_downloader
    )
    retry_worker = Worker(retry_db, retrying.process, max_attempts=1, retry_backoff=(1,))
    check(retry_worker.run_once(), "worker retried the job")
    check(downloads["count"] == 1, "retry reused the published file (no second download)")
    row = retry_db.get_video(VIDEO)
    check(
        bool(row) and row["status"] == "imported",
        f"retried job is imported (got {row['status'] if row else None})",
    )
    assert row is not None and row["file_path"]
    check(Path(row["file_path"]) == published[0], "retry imported the same file")
    leftovers = [path.name for path in video_files(TMP, VIDEO)]
    check(not leftovers, f"tmp_dir stays clean after the retry ({leftovers})")

    item2 = retry_abs.get_item(str(row["abs_item_id"]), expanded=True)
    episodes = [
        episode
        for episode in ((item2.get("media") or {}).get("episodes") or [])
        if (episode.get("audioFile") or {}).get("metadata", {}).get("filename") == published[0].name
    ]
    check(len(episodes) == 1, f"ABS has exactly one episode for the file ({len(episodes)})")

    # ---------------------------------------------------------------- phase 4
    print("\n== phase 4: /add and /jobs/{id} are read-only ==")
    os.environ["YT2ABS_PLAYLIST_ENABLED"] = "0"
    retry_db.update_video(VIDEO, status="failed", error="ui check", attempts=2)
    app = create_app(retry_config, db=retry_db, worker_enabled=False)
    import uvicorn

    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)
    check(server.started, "read-only app started on 127.0.0.1")

    base = f"http://127.0.0.1:{PORT}"
    try:
        for attempt in (1, 2):
            status, body = http_get(f"{base}/add?url=https://youtu.be/{VIDEO}")
            check(status == 200, f"/add #{attempt} answers 200 (got {status})")
            check("osobna akcja" in body, f"/add #{attempt} explains that retry is explicit")
        row = retry_db.get_video(VIDEO)
        check(
            bool(row) and row["status"] == "failed" and row["attempts"] == 2,
            "opening /add twice did not requeue the job",
        )

        status, body = http_get(f"{base}/jobs/{VIDEO}")
        check(status == 200, f"/jobs/{{id}} answers 200 (got {status})")
        check("Read-only view" in body, "/jobs/{id} is a read-only view")
        row = retry_db.get_video(VIDEO)
        check(
            bool(row) and row["status"] == "failed",
            "opening /jobs/{id} did not requeue the job",
        )

        request = urllib.request.Request(
            f"{base}/api/jobs/{VIDEO}/retry", method="POST", data=b""
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode())
        check(payload.get("status") == "queued", "explicit POST retry queues the job")
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        stub.shutdown()

    print(f"\n{checks - len(failures)}/{checks} checks passed")
    if failures:
        print("FAILED:")
        for failure in failures:
            print(" -", failure)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
