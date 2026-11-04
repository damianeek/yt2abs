"""E2E helper that runs *inside* the yt2abs container (host script copies it in).

Used by ``tests/e2e_retention_restart.py`` to prove that a config change plus a
container restart really re-evaluates the retention policy for the **same row**
whose ``added_at`` is already past the 7-day deadline:

* ``prepare``       – import the video and backdate/channel it to the test channel
* ``verify-keep``   – the row must still be ``imported`` with its ABS episode/file
* ``verify-seven``  – the row must be ``deleted`` and its ABS episode/file gone

No secrets are printed: the API token and ABS key stay in the environment.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

BASE = os.environ.get("YT2ABS_BASE_URL", "http://127.0.0.1:8080").rstrip("/")
DB = os.environ.get("YT2ABS_DB", "/data/yt2abs.sqlite3")
ABS = os.environ.get("ABS_URL", "").rstrip("/")
ABS_KEY = os.environ.get("ABS_API_KEY", "")
VIDEO_ID = os.environ.get("YT2ABS_E2E_VIDEO", "dQw4w9WgXcQ")
TARGET_CHANNEL = os.environ.get("YT2ABS_E2E_TARGET_CHANNEL", "UCseven00000000000000000")
DAYS_AGO = float(os.environ.get("YT2ABS_E2E_DAYS_AGO", "10"))
TIMEOUT = float(os.environ.get("YT2ABS_E2E_TIMEOUT", "600"))


def http(method: str, url: str, body: Any = None) -> tuple[int, Any]:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return int(response.status), json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            return int(exc.code), json.loads(raw) if raw else None
        except json.JSONDecodeError:
            return int(exc.code), raw


def api(method: str, path: str, body: Any = None) -> tuple[int, Any]:
    return http(method, f"{BASE}{path}", body)


def abs_get(path: str) -> tuple[int, Any]:
    request = urllib.request.Request(
        f"{ABS}{path}", headers={"Authorization": f"Bearer {ABS_KEY}", "Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return int(response.status), json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        return int(exc.code), None


def row() -> dict[str, Any]:
    connection = sqlite3.connect(DB, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        result = connection.execute(
            "SELECT * FROM videos WHERE video_id = ?", (VIDEO_ID,)
        ).fetchone()
    finally:
        connection.close()
    return dict(result) if result else {}


def episode_ids(item_id: str) -> list[str]:
    code, payload = abs_get(f"/api/items/{item_id}?expanded=1")
    if code != 200 or not isinstance(payload, dict):
        return []
    episodes = ((payload.get("media") or {}).get("episodes") or [])
    return [str(episode.get("id") or "") for episode in episodes if isinstance(episode, dict)]


def wait_imported() -> dict[str, Any]:
    deadline = time.monotonic() + TIMEOUT
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        code, job = api("GET", f"/api/jobs/{VIDEO_ID}")
        if code == 200 and isinstance(job, dict):
            last = job
            if job.get("status") == "imported":
                return job
            if job.get("status") == "failed":
                raise SystemExit(f"job failed: {job.get('error')}")
        time.sleep(2)
    raise SystemExit(f"job did not reach imported: {last}")


def prepare() -> dict[str, Any]:
    code, payload = api(
        "POST", "/api/jobs", {"url": f"https://www.youtube.com/watch?v={VIDEO_ID}"}
    )
    if code not in (200, 202):
        raise SystemExit(f"POST /api/jobs -> HTTP {code}: {payload}")
    job = wait_imported()
    added_at = (datetime.now(timezone.utc) - timedelta(days=DAYS_AGO)).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")
    connection = sqlite3.connect(DB, timeout=30)
    try:
        connection.execute(
            "UPDATE videos SET channel_id = ?, added_at = ? WHERE video_id = ?",
            (TARGET_CHANNEL, added_at, VIDEO_ID),
        )
        connection.commit()
    finally:
        connection.close()
    current = row()
    return {
        "phase": "prepare",
        "status": current.get("status"),
        "channel_id": current.get("channel_id"),
        "added_at": current.get("added_at"),
        "file_path": current.get("file_path"),
        "item_id": current.get("abs_item_id"),
        "episode_id": current.get("abs_episode_id"),
    }


def verify(expected: str) -> dict[str, Any]:
    current = row()
    item_id = str(current.get("abs_item_id") or "")
    episode_id = str(current.get("abs_episode_id") or "")
    episodes = episode_ids(item_id) if item_id else []
    file_exists = bool(current.get("file_path")) and Path(str(current["file_path"])).exists()
    payload = {
        "phase": f"verify-{expected}",
        "status": current.get("status"),
        "episode_count": len(episodes),
        "episode_present": episode_id in episodes,
        "file_exists": file_exists,
        "item_id": item_id,
    }
    if expected == "keep":
        ok = current.get("status") == "imported" and episode_id in episodes and file_exists
    else:
        # The item keeps episodes from older E2E runs; only *this* episode must
        # be gone, together with its file.
        ok = current.get("status") == "deleted" and episode_id not in episodes and not file_exists
    payload["ok"] = ok
    return payload


def main(argv: list[str]) -> int:
    phase = (argv[1] if len(argv) > 1 else "prepare").strip()
    if phase == "prepare":
        result = prepare()
    elif phase == "verify-keep":
        result = verify("keep")
    elif phase == "verify-seven":
        result = verify("seven")
    else:
        print(f"BLAD: nieznana faza {phase!r}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
