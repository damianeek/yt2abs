"""E2E: retention, protect_unfinished, reconcile and yt-dlp update.

Run inside the yt2abs container (or any host that sees the same SQLite file and
library) against a live service and a real Audiobookshelf instance:

    ABS_API_KEY=... python tests/e2e_retention.py

Environment:
  YT2ABS_BASE_URL          default http://127.0.0.1:8080
  YT2ABS_DB                default /data/yt2abs.sqlite3
  YT2ABS_LIBRARY_PATH      default /youtube-podcasts
  ABS_URL                  default http://192.168.1.10:13380
  ABS_API_KEY              Audiobookshelf API key (required)
  YT2ABS_E2E_VIDEO         default dQw4w9WgXcQ (short, public)
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

BASE = os.environ.get("YT2ABS_BASE_URL", "http://127.0.0.1:8080").rstrip("/")
DB = os.environ.get("YT2ABS_DB", "/data/yt2abs.sqlite3")
LIBRARY = Path(os.environ.get("YT2ABS_LIBRARY_PATH", "/youtube-podcasts"))
ABS = os.environ.get("ABS_URL", "http://192.168.1.10:13380").rstrip("/")
ABS_KEY = os.environ.get("ABS_API_KEY", "")
VIDEO_ID = os.environ.get("YT2ABS_E2E_VIDEO", "dQw4w9WgXcQ")
CHANNEL_ID = os.environ.get("YT2ABS_E2E_CHANNEL", "UCuAXFkgsw1L7xaCfnd5JJOw")
# Channel ids used by config.yaml of the E2E instance (valid ``UC`` + 22 chars,
# so ``yt2abs check`` does not warn about them).
KEEP_CHANNEL = os.environ.get("YT2ABS_E2E_KEEP_CHANNEL", "UCkeep000000000000000000")
SEVEN_CHANNEL = os.environ.get("YT2ABS_E2E_SEVEN_CHANNEL", "UCseven00000000000000000")
ONE_CHANNEL = os.environ.get("YT2ABS_E2E_ONE_CHANNEL", "UCone0000000000000000000")
TIMEOUT = float(os.environ.get("YT2ABS_E2E_TIMEOUT", "900"))

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, condition: bool, detail: str = "") -> bool:
    if len(detail) > 400:
        detail = detail[:400] + "..."
    RESULTS.append((name, bool(condition), detail))
    print(("PASS " if condition else "FAIL ") + name + (f" | {detail}" if detail else ""))
    return bool(condition)


def http(
    method: str,
    url: str,
    *,
    token: bool = True,
    cookie: str = "",
    body: Any = None,
    timeout: float = 120.0,
) -> tuple[int, Any]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            text = raw.decode("utf-8", errors="replace")
            try:
                return int(response.status), json.loads(text) if text else None
            except json.JSONDecodeError:
                return int(response.status), text
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        try:
            return int(exc.code), json.loads(text) if text else None
        except json.JSONDecodeError:
            return int(exc.code), text


def api(method: str, path: str, **kwargs: Any) -> tuple[int, Any]:
    return http(method, f"{BASE}{path}", **kwargs)


def wait_for_job(status: str = "imported", *, timeout: float = TIMEOUT) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        code, job = api("GET", f"/api/jobs/{VIDEO_ID}")
        if code == 200 and isinstance(job, dict):
            last = job
            if job.get("status") == status:
                return job
            if job.get("status") == "failed":
                raise RuntimeError(f"job failed: {job.get('error')}")
        time.sleep(2)
    raise TimeoutError(f"job did not reach {status}: {last}")


def run_retention() -> dict[str, Any]:
    """Run one pass synchronously (``?wait=true``) and return its report."""
    code, report = api("POST", "/api/retention/run?wait=true", timeout=300)
    if code != 200 or not isinstance(report, dict):
        raise RuntimeError(f"retention run failed: HTTP {code}: {report}")
    return report


def episode_ids(item_id: str) -> list[str]:
    """Episode ids of one podcast item (the shared E2E library keeps old ones)."""
    code, payload = _abs_request("GET", f"/api/items/{item_id}?expanded=1")
    if code != 200 or not isinstance(payload, dict):
        raise RuntimeError(f"ABS item {item_id}: HTTP {code}")
    episodes = ((payload.get("media") or {}).get("episodes") or [])
    return [str(episode.get("id") or "") for episode in episodes if isinstance(episode, dict)]


def episode_count(item_id: str) -> int:
    return len(episode_ids(item_id))


def item_exists(item_id: str) -> bool:
    code, _ = _abs_request("GET", f"/api/items/{item_id}")
    return code == 200


def file_exists(job: dict[str, Any]) -> bool:
    path = job.get("file_path") or ""
    return bool(path) and Path(path).exists()


def set_progress(item_id: str, episode_id: str, *, current: float, duration: float) -> bool:
    code, _ = _abs_request(
        "PATCH",
        f"/api/me/progress/{item_id}/{episode_id}",
        {
            "currentTime": current,
            "duration": duration,
            "progress": current / duration,
            "isFinished": False,
        },
    )
    return code in (200, 204)


def delete_episode_abs(item_id: str, episode_id: str) -> bool:
    code, _ = _abs_request(
        "DELETE", f"/api/podcasts/{item_id}/episode/{episode_id}?hard=1"
    )
    return code in (200, 204)


def main() -> int:
    if not ABS_KEY:
        print("BLAD: ustaw ABS_API_KEY", file=sys.stderr)
        return 2
    if not Path(DB).exists():
        print(f"BLAD: brak bazy {DB}", file=sys.stderr)
        return 2

    code, health = http("GET", f"{BASE}/health")
    check(
        "public /health is minimal",
        code == 200 and isinstance(health, dict) and sorted(health) == ["alive", "status"],
        str(health),
    )

    code, system = api("GET", "/api/system")
    check("system 200", code == 200 and isinstance(system, dict))
    check("no auth section (LAN mode)", "auth" not in (system or {}), str(sorted(system or {})))
    check(
        "yt-dlp version reported",
        bool((system or {}).get("ytdlp", {}).get("version")),
        str((system or {}).get("ytdlp", {}).get("version")),
    )

    code, listing = api("GET", "/api/jobs")
    check("api/jobs answers without credentials", code == 200, f"HTTP {code}")

    code, payload = api("POST", "/api/jobs", body={"url": "https://vimeo.com/123"})
    check("400 for non-YouTube URL", code == 400, f"HTTP {code}")

    code, page = api("GET", "/bookmarklet")
    check("bookmarklet page", code == 200 and "/add?url=" in str(page))

    # The bookmarklet does a top-level navigation with no credentials.
    code, page = http("GET", f"{BASE}/add?url=https%3A%2F%2Fyoutu.be%2F{VIDEO_ID}")
    check("/add queues a job", code == 200, f"HTTP {code}")
    code, _ = http("GET", f"{BASE}/add?url=https%3A%2F%2Fvimeo.com%2F1")
    check("/add rejects a non-YouTube URL", code == 400, f"HTTP {code}")
    job = wait_for_job("imported")
    item_id = str(job.get("abs_item_id") or "")
    episode_id = str(job.get("abs_episode_id") or "")
    check("imported has ABS ids", bool(item_id and episode_id))
    check("file exists after import", file_exists(job), str(job.get("file_path")))
    check("ABS has the new episode", episode_id in episode_ids(item_id))

    # --- retention 0: delete through the ABS API -------------------------
    before = db_row()
    check("channel policy 0 is used", before.get("channel_id") == CHANNEL_ID,
          str(before.get("channel_id")))
    report = run_retention()
    deleted = [entry["video_id"] for entry in report.get("deleted", [])]
    check("retention deleted the episode", VIDEO_ID in deleted, str(report))
    row = db_row()
    check("status=deleted", row.get("status") == "deleted", str(row.get("status")))
    check("deleted_at set", bool(row.get("deleted_at")))
    check("file removed from disk", not file_exists(job), str(job.get("file_path")))
    check("episode removed from ABS", episode_id not in episode_ids(item_id))
    check("empty podcast remains", item_exists(item_id))

    # --- re-marking a deleted video downloads it again --------------------
    code, payload = add_job()
    check("re-mark requeues", code == 200 and bool(payload.get("requeued")), str(payload))
    check("stale ABS ids cleared", payload.get("abs_item_id") is None)
    job = wait_for_job("imported")
    item_id = str(job.get("abs_item_id") or item_id)
    episode_id = str(job.get("abs_episode_id") or episode_id)
    check("re-imported into ABS", episode_id in episode_ids(item_id))
    check("file exists after re-import", file_exists(job))

    # --- keep policy (null) protects an old episode -----------------------
    db_execute(
        "UPDATE videos SET channel_id = ?, added_at = ? WHERE video_id = ?",
        (KEEP_CHANNEL, iso_days_ago(60), VIDEO_ID),
    )
    report = run_retention()
    row = db_row()
    check("keep channel does not delete", row.get("status") == "imported", str(row.get("status")))
    check("keep channel still in ABS", episode_id in episode_ids(item_id))

    # --- changing 7 days -> keep needs a config change and a restart ------
    # Covered by tests/e2e_retention_restart.py (host side): it rewrites
    # config.yaml, restarts the container and checks the same row twice.
    # (The old in-place channel_id swap is gone: it never proved a config reload.)


    # --- protect_unfinished defers, then deletes after 2*N ----------------
    db_execute(
        "UPDATE videos SET channel_id = ?, added_at = ? WHERE video_id = ?",
        (ONE_CHANNEL, iso_days_ago(1.5), VIDEO_ID),
    )
    check("ABS progress written", set_progress(item_id, episode_id, current=30.0, duration=213.0))
    report = run_retention()
    deferred = [entry["video_id"] for entry in report.get("deferred", [])]
    check("unfinished episode deferred", VIDEO_ID in deferred, str(report))
    check("unfinished episode not deleted", episode_id in episode_ids(item_id))
    check("unfinished status still imported", db_row().get("status") == "imported")

    db_execute(
        "UPDATE videos SET added_at = ? WHERE video_id = ?",
        (iso_days_ago(3.0), VIDEO_ID),
    )
    report = run_retention()
    check("deleted after 2*N despite unfinished", db_row().get("status") == "deleted")
    check("ABS episode gone after 2*N", episode_id not in episode_ids(item_id))
    check("file gone after 2*N", not file_exists(job))

    # --- reconcile an episode deleted manually in ABS ---------------------
    code, payload = add_job()
    check("third import queued", code == 200)
    job = wait_for_job("imported")
    item_id = str(job.get("abs_item_id") or item_id)
    episode_id = str(job.get("abs_episode_id") or episode_id)
    check("third import visible in ABS", episode_id in episode_ids(item_id))
    # The E2E ABS library is shared and keeps episodes from older runs with the
    # same file name. Remove them, so "deleted in ABS -> reconciled" is decided
    # by the stored episode id and not by a duplicate file name.
    for other in episode_ids(item_id):
        if other != episode_id:
            delete_episode_abs(item_id, other)
    check("stale duplicate episodes removed", episode_ids(item_id) == [episode_id])
    check("manual ABS delete works", delete_episode_abs(item_id, episode_id))
    report = run_retention()
    reconciled = [entry["video_id"] for entry in report.get("reconciled", [])]
    check("manual delete reconciled", VIDEO_ID in reconciled, str(report))
    check("reconciled status=deleted", db_row().get("status") == "deleted")
    check("reconcile did not delete podcast", item_exists(item_id))

    # --- update endpoint and status --------------------------------------
    code, status = api("GET", "/api/retention")
    check("retention status endpoint", code == 200 and status.get("last_report") is not None)
    code, system = api("GET", "/api/system")
    check("system endpoint", code == 200 and bool(system.get("ytdlp", {}).get("version")))
    code, update = api("POST", "/api/ytdlp/update", timeout=600)
    check("ytdlp update endpoint", code == 200 and update.get("error") is None, str(update))

    # --- preview (dry run) and the background manual run ------------------
    code, _ = add_job()
    check("preview setup import queued", code == 200)
    job = wait_for_job("imported")
    item_id = str(job.get("abs_item_id") or item_id)
    episode_id = str(job.get("abs_episode_id") or episode_id)
    db_execute(
        "UPDATE videos SET channel_id = ?, added_at = ? WHERE video_id = ?",
        (CHANNEL_ID, iso_days_ago(3), VIDEO_ID),
    )
    code, preview = api("GET", "/api/retention/preview")
    check("retention preview 200", code == 200 and isinstance(preview, dict), f"HTTP {code}")
    check("preview is dry_run", bool((preview or {}).get("dry_run")))
    planned = [entry.get("video_id") for entry in (preview or {}).get("planned", [])]
    check("preview plans the deletion", VIDEO_ID in planned, str(planned))
    check("preview did not delete", episode_id in episode_ids(item_id))
    check("preview kept status", db_row().get("status") == "imported")

    code, before = api("GET", "/api/retention")
    started_before = (before.get("last_report") or {}).get("started_at") if code == 200 else None
    code, started = api("POST", "/api/retention/run")
    check("manual run returns 202", code == 202, f"HTTP {code}: {started}")
    deadline = time.monotonic() + 120
    status: dict[str, Any] = {}
    while time.monotonic() < deadline:
        code, status = api("GET", "/api/retention")
        current_started = (status.get("last_report") or {}).get("started_at")
        if code == 200 and not status.get("running") and current_started != started_before:
            break
        time.sleep(1)
    check(
        "manual run finishes in background",
        code == 200
        and not status.get("running")
        and (status.get("last_report") or {}).get("started_at") != started_before,
        str((status.get("last_report") or {}).get("started_at")),
    )
    check("background run deleted the previewed row", db_row().get("status") == "deleted")

    failed = [name for name, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    if failed:
        print("FAILED: " + ", ".join(failed), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
