"""E2E: a ``config.yaml`` change plus a container restart re-evaluates retention.

Runs on the Docker host (next to the yt2abs container) and proves the exact
acceptance case from the review (finding #9): a row whose ``added_at`` is already
past the 7-day deadline

1. survives when the channel policy becomes ``null`` (keep) and the container is
   restarted,
2. is deleted when the same row's channel policy becomes ``7`` and the container
   is restarted again.

The phase helper (``e2e_restart_phase.py``) is copied into the container and
executed there, so it sees the same SQLite file, library and ABS.

Environment:
  YT2ABS_CONTAINER        container name (required)
  YT2ABS_CONFIG_HOST      host path of the bind-mounted config.yaml (required)
  YT2ABS_BASE_URL         default http://127.0.0.1:8080 (published port of the container)
  ABS_URL                 Audiobookshelf URL seen from the container
  ABS_LIBRARY_ID          library id
  YT2ABS_E2E_VIDEO        default dQw4w9WgXcQ
  YT2ABS_E2E_TARGET_CHANNEL  default UCseven00000000000000000
  YT2ABS_RESTART_TIMEOUT  default 180 seconds

Usage:
  YT2ABS_CONTAINER=yt2abs-e2e-ret1b \\
  YT2ABS_CONFIG_HOST=/mnt/user/appdata/yt2abs-e2e-ret1b/app/config.yaml \\
  YT2ABS_BASE_URL=http://127.0.0.1:13385 \\
  ABS_URL=http://192.168.1.10:13380 ABS_LIBRARY_ID=... \\
  python3 tests/e2e_retention_restart.py
"""
from __future__ import annotations

import http.client as http_client
import json
import os
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

CONTAINER = os.environ.get("YT2ABS_CONTAINER", "")
CONFIG_HOST = Path(os.environ.get("YT2ABS_CONFIG_HOST", ""))
BASE = os.environ.get("YT2ABS_BASE_URL", "http://127.0.0.1:8080").rstrip("/")
ABS_URL = os.environ.get("ABS_URL", "").rstrip("/")
LIBRARY_ID = os.environ.get("ABS_LIBRARY_ID", "")
VIDEO_ID = os.environ.get("YT2ABS_E2E_VIDEO", "dQw4w9WgXcQ")
TARGET_CHANNEL = os.environ.get("YT2ABS_E2E_TARGET_CHANNEL", "UCseven00000000000000000")
KEEP_CHANNEL = os.environ.get("YT2ABS_E2E_KEEP_CHANNEL", "UCkeep000000000000000000")
ONE_CHANNEL = os.environ.get("YT2ABS_E2E_ONE_CHANNEL", "UCone0000000000000000000")
MAIN_CHANNEL = os.environ.get("YT2ABS_E2E_CHANNEL", "UCuAXFkgsw1L7xaCfnd5JJOw")
RESTART_TIMEOUT = float(os.environ.get("YT2ABS_RESTART_TIMEOUT", "180"))
PHASE_PATH = Path(__file__).with_name("e2e_restart_phase.py")
#: Optional ``user@host`` when the Docker daemon lives on another machine (the
#: Unraid host has no python3, so the test runs here and drives docker over SSH).
SSH_HOST = os.environ.get("YT2ABS_SSH_HOST", "").strip()
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
SSH_KEY = os.environ.get("YT2ABS_SSH_KEY", str(Path.home() / ".ssh" / "hermes_agent"))
REMOTE_PHASE = "/tmp/e2e_restart_phase.py"

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, condition: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(condition), detail))
    print(("PASS " if condition else "FAIL ") + name + (f" | {detail}" if detail else ""))
    return bool(condition)


def ssh(*args: str, input_text: str | None = None, timeout: float = 120.0):
    command = ["ssh", *SSH_OPTS]
    if Path(SSH_KEY).exists():
        command += ["-i", SSH_KEY]
    return subprocess.run(
        [*command, SSH_HOST, *args],
        input=input_text,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def docker(*args: str, timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
    if SSH_HOST:
        command = " ".join(shlex.quote(part) for part in ("docker", *args))
        return ssh(command, timeout=timeout)
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout
    )


def read_config() -> str:
    if SSH_HOST:
        return ssh(f"cat {shlex.quote(str(CONFIG_HOST))}").stdout
    return CONFIG_HOST.read_text(encoding="utf-8") if CONFIG_HOST.exists() else ""


def write_config_text(text: str) -> None:
    if SSH_HOST:
        result = ssh(
            f"cat > {shlex.quote(str(CONFIG_HOST))}", input_text=text, timeout=60
        )
        if result.returncode != 0:
            raise SystemExit(f"nie można zapisać configu: {result.stderr}")
    else:
        CONFIG_HOST.write_text(text, encoding="utf-8")


def remove_backup(path: Path) -> None:
    if SSH_HOST:
        ssh(f"rm -f {shlex.quote(str(path))}")
    else:
        path.unlink(missing_ok=True)


def upload_phase() -> str:
    """Make the phase helper available on the Docker host; returns its path there."""
    text = PHASE_PATH.read_text(encoding="utf-8")
    if SSH_HOST:
        result = ssh(f"cat > {shlex.quote(REMOTE_PHASE)}", input_text=text, timeout=60)
        if result.returncode != 0:
            raise SystemExit(f"nie można wgrać {PHASE_PATH}: {result.stderr}")
        return REMOTE_PHASE
    return str(PHASE_PATH)


def http(method: str, path: str, body: Any = None, timeout: float = 60.0) -> tuple[int, Any]:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(f"{BASE}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return int(response.status), json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            return int(exc.code), json.loads(raw) if raw else None
        except json.JSONDecodeError:
            return int(exc.code), raw


def config_text(seven_days: int | None) -> str:
    seven = "null" if seven_days is None else str(seven_days)
    return f"""# generated by tests/e2e_retention_restart.py
defaults:
  retention_days: 14
  protect_unfinished: false

abs:
  url: {ABS_URL}
  library_id: {LIBRARY_ID}
  library_path: /youtube-podcasts

api:
  token: ""
  rate_limit_per_minute: 600
  public_rate_limit_per_minute: 600

retention:
  interval_minutes: 60
  run_on_startup: true

paths:
  data_dir: /data
  tmp_dir: /tmp-downloads

channels:
  {MAIN_CHANNEL}: {{retention_days: 0}}
  {KEEP_CHANNEL}: {{retention_days: null}}
  {TARGET_CHANNEL}: {{retention_days: {seven}}}
  {ONE_CHANNEL}: {{retention_days: 1, protect_unfinished: true}}
"""


def write_config(seven_days: int | None) -> None:
    write_config_text(config_text(seven_days))


def retention_state() -> dict[str, Any]:
    try:
        code, payload = http("GET", "/api/retention")
    except (urllib.error.URLError, OSError, http_client.HTTPException):
        return {}
    return payload if code == 200 and isinstance(payload, dict) else {}


def wait_health(timeout: float = RESTART_TIMEOUT) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            code, payload = http("GET", "/health", timeout=5)
        except (urllib.error.URLError, OSError, http_client.HTTPException):
            # The container/port is still restarting.
            code, payload = 0, None
        if code == 200 and isinstance(payload, dict):
            return True
        time.sleep(2)
    return False


def wait_startup_pass(before: str | None, timeout: float = RESTART_TIMEOUT) -> dict[str, Any]:
    """Wait until the retention thread has finished its startup pass."""
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        state = retention_state()
        last = state.get("last_report") or {}
        finished = last.get("finished_at") if isinstance(last, dict) else None
        if finished and finished != before and not state.get("running"):
            return state
        time.sleep(2)
    return last if isinstance(last, dict) else {}


def restart(started_at: str | None) -> dict[str, Any]:
    result = docker("restart", CONTAINER, timeout=RESTART_TIMEOUT + 60)
    check(f"docker restart {CONTAINER}", result.returncode == 0, result.stderr.strip()[:200])
    check("container healthy after restart", wait_health())
    state = wait_startup_pass(started_at)
    check(
        "startup retention pass finished",
        bool(state.get("last_report") and state["last_report"].get("finished_at")),
        str(state.get("last_report", {}).get("finished_at")),
    )
    return state


def run_phase(phase: str) -> tuple[int, dict[str, Any]]:
    result = docker(
        "exec",
        "-e",
        "YT2ABS_BASE_URL=http://127.0.0.1:8080",
        "-e",
        f"YT2ABS_E2E_VIDEO={VIDEO_ID}",
        "-e",
        f"YT2ABS_E2E_TARGET_CHANNEL={TARGET_CHANNEL}",
        CONTAINER,
        "python3",
        "/tmp/e2e_restart_phase.py",
        phase,
        timeout=300,
    )
    payload: dict[str, Any] = {}
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
    if result.returncode != 0:
        print(result.stdout[-800:], file=sys.stderr)
        print(result.stderr[-800:], file=sys.stderr)
    return result.returncode, payload


def main() -> int:
    if not CONTAINER or not CONFIG_HOST.name:
        print(
            "BLAD: ustaw YT2ABS_CONTAINER i YT2ABS_CONFIG_HOST",
            file=sys.stderr,
        )
        return 2
    if not ABS_URL or not LIBRARY_ID:
        print("BLAD: ustaw ABS_URL i ABS_LIBRARY_ID", file=sys.stderr)
        return 2
    if not PHASE_PATH.exists():
        print(f"BLAD: brak {PHASE_PATH}", file=sys.stderr)
        return 2

    original = read_config()
    backup = CONFIG_HOST.with_suffix(".yaml.e2e-backup")
    phase_host_path = upload_phase()
    copied = docker("cp", phase_host_path, f"{CONTAINER}:/tmp/e2e_restart_phase.py")
    if copied.returncode != 0:
        print(f"BLAD: docker cp: {copied.stderr}", file=sys.stderr)
        return 2

    try:
        # Phase A: row past the 7-day deadline, config says keep, then restart.
        before = (retention_state().get("last_report") or {}).get("finished_at")
        write_config(None)
        code, prepared = run_phase("prepare")
        check("prepare row (10 days old, UCseven)", code == 0, str(prepared))

        before = (retention_state().get("last_report") or {}).get("finished_at")
        write_config(None)
        restart(before)
        code, keep = run_phase("verify-keep")
        check("keep policy survives restart", code == 0 and keep.get("ok") is True, str(keep))

        # Phase B: same row, config now says 7 days, another restart.
        before = (retention_state().get("last_report") or {}).get("finished_at")
        write_config(7)
        restart(before)
        code, seven = run_phase("verify-seven")
        check("7-day policy deletes after restart", code == 0 and seven.get("ok") is True, str(seven))
    finally:
        if original:
            write_config_text(original)
        else:
            write_config(7)
        remove_backup(backup)

    failed = [name for name, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    if failed:
        print("FAILED: " + ", ".join(failed), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
