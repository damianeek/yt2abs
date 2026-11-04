"""E2E: open LAN surface, rate limiting and proxy trust on a real uvicorn server.

Unlike ``tests/test_security.py`` (in-process ``TestClient``), this script boots
the application exactly like production does - ``python -m yt2abs`` with
uvicorn's access log enabled - so it can prove the acceptance criteria that
matter operationally now that the token is gone:

* every page and ``/api/*`` endpoint answers without credentials: ``/``,
  ``/bookmarklet``, ``/add`` and ``/api/system``; the old ``?token=`` bootstrap
  is just an ignored query parameter and nothing sets a cookie;
* ``/api/session`` no longer exists (``404``);
* ``/health`` stays minimal, ``/api/system`` keeps the diagnostics;
* the rate limit still applies: ``/api/*`` and ``/add`` have separate buckets
  and ``/health`` is exempt;
* a forged ``X-Forwarded-For`` from an untrusted peer does not create a new
  bucket, while a trusted proxy's right-most hop does - including a *repeated*
  header (HAProxy-style), where only the proxy-appended line counts;
* behind the reverse proxy a ``/add/`` slash redirect stays on https.

Usage::

    python tests/e2e_security.py
"""
from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
PYTHON = sys.executable
VIDEO = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, condition: bool, detail: str = "") -> bool:
    if len(detail) > 300:
        detail = detail[:300] + "..."
    RESULTS.append((name, bool(condition), detail))
    print(("PASS " if condition else "FAIL ") + name + (f" | {detail}" if detail else ""))
    return bool(condition)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: Any = None,
    timeout: float = 30.0,
    follow: bool = True,
) -> tuple[int, dict[str, str], str, str]:
    """Return ``(status, headers, body, final_url)``; never raises on 4xx/5xx."""

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, hdrs, newurl):  # noqa: ANN001
            return None

    data = json.dumps(body).encode("utf-8") if body is not None else None
    request_headers = {"Accept": "application/json, text/html"}
    request_headers.update(headers or {})
    if data is not None:
        request_headers["Content-Type"] = "application/json"
    opener = urllib.request.build_opener() if follow else urllib.request.build_opener(NoRedirect)
    prepared = urllib.request.Request(url, data=data, headers=request_headers, method=method)
    try:
        with opener.open(prepared, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return (
                int(response.status),
                {key.lower(): value for key, value in response.headers.items()},
                raw,
                response.geturl() or url,
            )
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        return (
            int(exc.code),
            {key.lower(): value for key, value in exc.headers.items()},
            raw,
            exc.geturl() or url,
        )


class Server:
    """A real ``python -m yt2abs`` process with its own config and log file."""

    def __init__(self, api: dict[str, Any]) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.access_log = self.root / "access.log"
        config = {
            "defaults": {"retention_days": 14},
            "abs": {
                "url": "http://127.0.0.1:9",
                "api_key": "unused",
                "library_id": "lib",
                "library_path": "/library",
            },
            "paths": {
                "data_dir": str(self.root / "data"),
                "tmp_dir": str(self.root / "tmp"),
                "library_path": str(self.root / "library"),
            },
            "api": {"public_url": "https://yt2abs.example.com", **api},
            "channels": {},
        }
        self.config_path = self.root / "config.yaml"
        self.config_path.write_text(json.dumps(config), encoding="utf-8")
        self.port = free_port()
        env = {
            **os.environ,
            "YT2ABS_CONFIG": str(self.config_path),
            "YT2ABS_HOST": "127.0.0.1",
            "YT2ABS_PORT": str(self.port),
            "YT2ABS_WORKER_ENABLED": "0",
            "YT2ABS_LOG_LEVEL": "info",
            "PYTHONPATH": str(ROOT),
        }
        self.log = self.access_log.open("wb")
        self.process = subprocess.Popen(
            [PYTHON, "-m", "yt2abs"],
            cwd=str(ROOT),
            env=env,
            stdout=self.log,
            stderr=subprocess.STDOUT,
        )
        self.base = f"http://127.0.0.1:{self.port}"

    def wait_ready(self, timeout: float = 40.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                return False
            try:
                status, _, _, _ = request("GET", f"{self.base}/health", timeout=2)
                if status in (200, 503):
                    return True
            except OSError:
                pass
            time.sleep(0.2)
        return False

    def logs(self) -> str:
        self.log.flush()
        return self.access_log.read_text(encoding="utf-8", errors="replace")

    def close(self) -> None:
        self.process.terminate()
        try:
            self.process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        self.log.close()
        self.tmp.cleanup()


def open_surface_checks(server: Server) -> None:
    """Nothing asks for a token; the LAN boundary is the access control."""
    base = server.base
    status, headers, body, _ = request("GET", f"{base}/health")
    payload = json.loads(body) if body.strip().startswith("{") else {}
    check("health answers 200", status == 200, f"HTTP {status}")
    check(
        "health stays minimal (status + alive only)",
        sorted(payload) == ["alive", "status"],
        str(sorted(payload)),
    )

    status, _, body, _ = request("GET", f"{base}/api/system")
    system = json.loads(body) if body.strip().startswith("{") else {}
    check("api/system answers without credentials", status == 200, f"HTTP {status}")
    check("api/system keeps the diagnostics", status == 200 and "ytdlp" in system, f"HTTP {status}")
    check(
        "api/system reports no auth section",
        isinstance(system.get("api"), dict) and "auth" not in system,
        str(sorted(system)),
    )

    status, _, _, _ = request("GET", f"{base}/api/jobs")
    check("api/jobs answers without credentials", status == 200, f"HTTP {status}")

    status, _, body, _ = request("GET", f"{base}/")
    check("the WebUI shell loads", status == 200 and "/static/app.js" in body, f"HTTP {status}")

    status, _, body, _ = request("GET", f"{base}/bookmarklet")
    check("the bookmarklet page loads", status == 200 and "/add?url=" in body, f"HTTP {status}")

    status, _, body, _ = request("GET", f"{base}/add?url={urllib.parse.quote(VIDEO, safe='')}")
    check("/add queues a job", status == 200 and "Added" in body, f"HTTP {status}")

    status, _, _, _ = request("GET", f"{base}/add?url=https%3A%2F%2Fvimeo.com%2F1")
    check("/add rejects a non-YouTube URL", status == 400, f"HTTP {status}")

    status, headers, _, _ = request(
        "GET", f"{base}/?token=leftover-secret", follow=False
    )
    check("a legacy ?token= is ignored (200, no redirect)", status == 200, f"HTTP {status}")
    check("a legacy ?token= sets no cookie", "set-cookie" not in headers, str(headers.get("set-cookie", "")))

    status, _, _, _ = request("POST", f"{base}/api/session", body={"token": "x"})
    check("/api/session is gone", status == 404, f"HTTP {status}")


def rate_limit_checks(server: Server) -> None:
    """``/api/*`` and ``/add`` have separate buckets; ``/health`` is exempt."""
    base = server.base
    statuses = [request("GET", f"{base}/api/jobs")[0] for _ in range(4)]
    check(
        "api limit: two requests pass, the rest are 429",
        statuses == [200, 200, 429, 429],
        str(statuses),
    )

    add_statuses = [
        request("GET", f"{base}/add?url={urllib.parse.quote(VIDEO, safe='')}")[0]
        for _ in range(3)
    ]
    check("/add limit: requests pass, the last is 429", add_statuses == [200, 200, 429], str(add_statuses))

    health = [request("GET", f"{base}/health")[0] for _ in range(3)]
    check("/health is never rate limited", health == [200, 200, 200], str(health))


def spoof_checks(server: Server) -> None:
    """A forged X-Forwarded-For from an untrusted peer must not reset the bucket."""
    base = server.base
    statuses = []
    for index in range(3):
        status, _, _, _ = request(
            "GET",
            f"{base}/api/jobs",
            headers={"X-Forwarded-For": f"203.0.113.{index + 1}"},
        )
        statuses.append(status)
    check(
        "untrusted X-Forwarded-For cannot bypass the rate limit",
        statuses == [200, 429, 429],
        str(statuses),
    )


def trusted_proxy_checks(server: Server) -> None:
    """With the peer in trusted_proxies the right-most hop picks the bucket."""
    base = server.base
    first, _, _, _ = request(
        "GET",
        f"{base}/api/jobs",
        headers={"X-Forwarded-For": "203.0.113.1, 198.51.100.7"},
    )
    second, _, _, _ = request(
        "GET",
        f"{base}/api/jobs",
        headers={"X-Forwarded-For": "203.0.113.2, 198.51.100.7"},
    )
    other, _, _, _ = request(
        "GET",
        f"{base}/api/jobs",
        headers={"X-Forwarded-For": "192.0.2.9"},
    )
    check(
        "trusted proxy: spoofed left-most hop keeps one bucket",
        (first, second) == (200, 429),
        f"{first}, {second}",
    )
    check(
        "trusted proxy: another real client has its own bucket",
        other == 200,
        str(other),
    )


def raw_get_with_forwarded_lines(server: Server, path: str, lines: list[str]) -> int:
    """GET over a raw socket with *repeated* ``X-Forwarded-For`` header lines."""
    connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=15)
    try:
        connection.putrequest("GET", path)
        connection.putheader("Accept", "application/json")
        for value in lines:
            connection.putheader("X-Forwarded-For", value)
        connection.endheaders()
        response = connection.getresponse()
        response.read()
        return int(response.status)
    finally:
        connection.close()


def repeated_xff_checks(server: Server) -> None:
    """Review #5: HAProxy appends its own line, so the last line's right-most entry wins.

    ``urllib`` merges duplicate headers, so this check speaks raw HTTP - the
    only way to prove the real ASGI stack keeps both lines and reads the second
    one.  A limit of one request per bucket makes the bucket choice visible in
    the status codes.
    """
    path = "/api/jobs"
    first = raw_get_with_forwarded_lines(server, path, ["203.0.113.1", "198.51.100.99"])
    second = raw_get_with_forwarded_lines(server, path, ["203.0.113.77", "198.51.100.99"])
    other = raw_get_with_forwarded_lines(server, path, ["203.0.113.2", "198.51.100.98"])
    check(
        "duplicated X-Forwarded-For: a forged first line keeps one bucket",
        (first, second) == (200, 429),
        f"{first}, {second}",
    )
    check(
        "duplicated X-Forwarded-For: the proxy-appended line picks the bucket",
        other == 200,
        str(other),
    )


def proxy_redirect_check(server: Server) -> None:
    """Behind NPM a ``/add/`` slash redirect must stay on https."""
    status, headers, _, _ = request(
        "GET",
        f"{server.base}/add/",
        headers={"Host": "yt2abs.example.com"},
        follow=False,
    )
    check(
        "the slash redirect behind the proxy stays https",
        status == 307 and headers.get("location", "").startswith("https://yt2abs.example.com/add"),
        f"HTTP {status} {headers.get('location', '')}",
    )


def single_config_load_check(server: Server) -> None:
    """``python -m yt2abs`` must parse the config once (no duplicated warnings)."""
    logs = server.logs()
    occurrences = logs.count("covers 16777216 addresses")
    check(
        "the config is parsed once, so startup warnings are not duplicated",
        occurrences == 1,
        f"{occurrences} warning(s)",
    )


def dockerfile_cmd() -> list[str]:
    """The ``CMD`` of the runtime image, parsed the way Docker does (exec form)."""
    for line in (ROOT / "Dockerfile").read_text(encoding="utf-8").splitlines():
        if line.startswith("CMD "):
            try:
                value = json.loads(line[4:].strip())
            except json.JSONDecodeError:
                return []
            return value if isinstance(value, list) else []
    return []


def deployment_invariant_check() -> None:
    argv = dockerfile_cmd()
    check(
        "Dockerfile CMD starts the app through python -m yt2abs",
        argv == ["python", "-m", "yt2abs"],
        str(argv),
    )


def main() -> int:
    servers: list[Server] = []
    try:
        server = Server({"rate_limit_per_minute": 100, "public_rate_limit_per_minute": 100})
        servers.append(server)
        if not server.wait_ready():
            print("BLAD: serwer nie wystartowal", file=sys.stderr)
            print(server.logs()[-2000:], file=sys.stderr)
            return 2
        open_surface_checks(server)
        proxy_redirect_check(server)

        limited = Server({"rate_limit_per_minute": 2, "public_rate_limit_per_minute": 2})
        servers.append(limited)
        if not limited.wait_ready():
            print("BLAD: serwer (rate limit) nie wystartowal", file=sys.stderr)
            return 2
        rate_limit_checks(limited)

        untrusted = Server(
            {
                "rate_limit_per_minute": 1,
                "public_rate_limit_per_minute": 100,
                "trust_proxy": True,
                "trusted_proxies": ["10.0.0.0/8"],
            }
        )
        servers.append(untrusted)
        if not untrusted.wait_ready():
            print("BLAD: serwer (untrusted) nie wystartowal", file=sys.stderr)
            return 2
        single_config_load_check(untrusted)
        spoof_checks(untrusted)

        trusted = Server(
            {
                "rate_limit_per_minute": 1,
                "public_rate_limit_per_minute": 100,
                "trust_proxy": True,
                "trusted_proxies": ["127.0.0.1/32"],
            }
        )
        servers.append(trusted)
        if not trusted.wait_ready():
            print("BLAD: serwer (trusted) nie wystartowal", file=sys.stderr)
            return 2
        trusted_proxy_checks(trusted)
        repeated_xff_checks(trusted)
    finally:
        for item in servers:
            item.close()

    deployment_invariant_check()

    failed = [name for name, ok, _ in RESULTS if not ok]
    print()
    print(f"{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    if failed:
        for name in failed:
            print(f"FAILED: {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
