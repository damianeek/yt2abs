"""End-to-end smoke test against a running yt2abs instance.

Usage:
    YT2ABS_BASE_URL=http://127.0.0.1:8099 python tests/e2e_service.py [video_url]

Checks: POST creates a job, a repeated POST returns the existing one, then polls
until the job is imported (or failed) and prints the final JSON. Exit code 1 on
failure or timeout. No third-party dependencies.
"""
from __future__ import annotations

import json
import os
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

BASE_URL = os.environ.get("YT2ABS_BASE_URL", "http://127.0.0.1:8099").rstrip("/")
DEFAULT_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
TIMEOUT_SECONDS = int(os.environ.get("YT2ABS_E2E_TIMEOUT", "600"))
POLL_SECONDS = float(os.environ.get("YT2ABS_E2E_POLL", "3"))


def call(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    request = Request(
        BASE_URL + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urlopen(request, timeout=30) as response:  # noqa: S310 - local service
            return int(response.status), json.loads(response.read() or b"{}")
    except HTTPError as exc:
        payload = exc.read()
        return int(exc.code), json.loads(payload or b"{}")


def main() -> int:
    video_url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    print(f"POST /api/jobs {video_url}")
    status, created = call("POST", "/api/jobs", {"url": video_url})
    print(f"  -> HTTP {status}: {json.dumps(created, ensure_ascii=False)}")
    if status not in (200, 202):
        return 1
    video_id = created["video_id"]

    status, repeated = call("POST", "/api/jobs", {"url": video_url})
    print(f"repeat POST -> HTTP {status}: status={repeated.get('status')}")
    if status != 200 or repeated.get("video_id") != video_id:
        return 1

    deadline = time.monotonic() + TIMEOUT_SECONDS
    job: dict = {}
    while time.monotonic() < deadline:
        _, job = call("GET", f"/api/jobs/{video_id}")
        print(
            f"  status={job.get('status')} stage={job.get('stage')} "
            f"attempts={job.get('attempts')} error={job.get('error')}"
        )
        if job.get("status") in ("imported", "failed"):
            break
        time.sleep(POLL_SECONDS)

    print(json.dumps(job, ensure_ascii=False, indent=2))
    return 0 if job.get("status") == "imported" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (URLError, OSError) as exc:
        print(f"cannot reach {BASE_URL}: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
