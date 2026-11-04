"""E2E for the WebUI/API contract (queue, channels, retention, delete).

Run from any host that can reach yt2abs and its Audiobookshelf instance:

    YT2ABS_BASE_URL=http://127.0.0.1:8082 \
    ABS_API_KEY=... python tests/e2e_webui.py prepare

``prepare`` imports a known short video, changes its channel policy to "keep",
runs retention and leaves one genuinely failed job for the browser UI.  The
rest of the flow (UI add -> imported, restart persistence, UI delete) is driven
by the browser test in the task report; ``cleanup`` removes test jobs.

No third-party dependencies.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any

BASE = os.environ.get("YT2ABS_BASE_URL", "http://127.0.0.1:8082").rstrip("/")
ABS = os.environ.get("ABS_URL", "http://192.168.1.10:13380").rstrip("/")
ABS_KEY = os.environ.get("ABS_API_KEY", "")
VIDEO_ID = os.environ.get("YT2ABS_E2E_VIDEO", "dQw4w9WgXcQ")
BAD_VIDEO = os.environ.get("YT2ABS_E2E_BAD_VIDEO", "aaaaaaaaaaa")
CHANNEL_ID = os.environ.get("YT2ABS_E2E_CHANNEL", "UCuAXFkgsw1L7xaCfnd5JJOw")
TIMEOUT = float(os.environ.get("YT2ABS_E2E_TIMEOUT", "600"))

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, condition: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(condition), detail))
    print(("PASS " if condition else "FAIL ") + name + (f" | {detail}" if detail else ""))
    return bool(condition)


def http(
    method: str,
    url: str,
    *,
    bearer: str | None = None,
    body: Any = None,
    timeout: float = 120.0,
) -> tuple[int, Any]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            text = raw.decode("utf-8", errors="replace")
            return int(response.status), _json_or_text(text)
    except urllib.error.HTTPError as exc:
        return int(exc.code), _json_or_text(exc.read().decode("utf-8", errors="replace"))


def _json_or_text(text: str) -> Any:
    try:
        return json.loads(text) if text else None
    except json.JSONDecodeError:
        return text


def api(method: str, path: str, **kwargs: Any) -> tuple[int, Any]:
    return http(method, f"{BASE}{path}", **kwargs)


def abs_api(method: str, path: str) -> tuple[int, Any]:
    if not ABS_KEY:
        return 0, None
    return http(method, f"{ABS}{path}", bearer=ABS_KEY, timeout=120.0)


def job(video_id: str) -> dict[str, Any]:
    code, payload = api("GET", f"/api/jobs/{video_id}")
    return payload if code == 200 and isinstance(payload, dict) else {}


def wait_for(video_id: str, status: str, *, timeout: float = TIMEOUT) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = job(video_id)
        if last.get("status") == status:
            return last
        if last.get("status") == "failed" and status != "failed":
            raise RuntimeError(f"{video_id} failed: {last.get('error')}")
        time.sleep(2)
    raise TimeoutError(f"{video_id} did not reach {status}: {last}")


def episode_count(item_id: str) -> int:
    code, payload = abs_api("GET", f"/api/items/{item_id}?expanded=1")
    if code != 200 or not isinstance(payload, dict):
        raise RuntimeError(f"ABS item {item_id}: HTTP {code}")
    return len(((payload.get("media") or {}).get("episodes") or []))


def prepare() -> int:

    code, index = http("GET", f"{BASE}/")
    check("WebUI shell is public", code == 200 and "Queue" in str(index), f"HTTP {code}")
    code, script = http("GET", f"{BASE}/static/app.js")
    check("WebUI JS is served", code == 200 and "renderJobs" in str(script), f"HTTP {code}")
    code, _ = http("GET", f"{BASE}/api/jobs")
    check("API needs the bearer token", code == 401, f"HTTP {code}")

    code, payload = api(
        "POST", "/api/jobs", body={"url": f"https://youtu.be/{VIDEO_ID}"}
    )
    check("add video queues a job", code in (200, 202), f"HTTP {code}")
    imported = wait_for(VIDEO_ID, "imported")
    item_id = str(imported.get("abs_item_id") or "")
    episode_id = str(imported.get("abs_episode_id") or "")
    check("job imported with ABS ids", bool(item_id and episode_id))
    check("ABS has the episode", episode_count(item_id) == 1 if item_id else False)
    retention = imported.get("retention") or {}
    check(
        "deadline comes from the channel policy",
        retention.get("days") == 7 and retention.get("source") == "channel",
        str(retention),
    )

    code, channel = api("PUT", f"/api/channels/{CHANNEL_ID}", body={"retention_days": None})
    check("channel set to keep forever", code == 200 and channel.get("retention_days") is None,
          f"HTTP {code}")
    refreshed = job(VIDEO_ID)
    check(
        "job deadline disappears after channel change",
        (refreshed.get("retention") or {}).get("keep") is True,
        str(refreshed.get("retention")),
    )

    code, report = api("POST", "/api/retention/run", timeout=300)
    check("retention run succeeds", code == 200 and isinstance(report, dict), f"HTTP {code}")
    check("keep protects the imported episode", job(VIDEO_ID).get("status") == "imported")

    code, status = api("GET", "/api/abs/status")
    check(
        "ABS status reports the version",
        code == 200 and bool((status or {}).get("version")),
        f"HTTP {code}: {status}",
    )

    code, payload = api(
        "POST", "/api/jobs", body={"url": f"https://youtu.be/{BAD_VIDEO}"}
    )
    check("failed-job fixture queued", code in (200, 202), f"HTTP {code}")
    failed = wait_for(BAD_VIDEO, "failed", timeout=240)
    check("failed job has an error", bool(failed.get("error")), str(failed.get("error"))[:200])

    print("\nstate: imported=%s failed=%s" % (VIDEO_ID, BAD_VIDEO))
    return 0 if all(ok for _, ok, _ in RESULTS) else 1


def cleanup() -> int:
    for video_id in (BAD_VIDEO,):
        code, _ = api("DELETE", f"/api/jobs/{video_id}")
        check(f"cleanup {video_id}", code in (200, 404), f"HTTP {code}")
    if "--with-imported" in sys.argv:
        code, _ = api("DELETE", f"/api/jobs/{VIDEO_ID}")
        check(f"cleanup {VIDEO_ID}", code in (200, 404), f"HTTP {code}")
    return 0 if all(ok for _, ok, _ in RESULTS) else 1


def main() -> int:
    action = sys.argv[1] if len(sys.argv) > 1 else "prepare"
    if action == "prepare":
        return prepare()
    if action == "cleanup":
        return cleanup()
    print(f"unknown action {action!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
