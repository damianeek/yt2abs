"""End-to-end test of the yt2abs bookmarklet ("Send to ABS") in real Firefox.

The bookmarklet is the supported stand-in for the Firefox extension. This test
proves the whole user path on a real browser: read the code served by
``GET /bookmarklet``, save it as a real bookmark in the bookmarks toolbar,
click that bookmark on a real YouTube watch page and check that the server
created the job - twice, to prove the second click is idempotent.

It drives Firefox through Marionette/geckodriver, so it needs a Firefox build
that allows ``-remote-allow-system-access`` (ESR or Developer Edition). The
bookmark is inserted through Places (chrome context) and clicked through
Firefox's own toolbar, not by faking the navigation.

Requirements: Firefox, geckodriver and ``selenium`` in the active interpreter.

    FIREFOX_BIN          path to the firefox binary (default: firefox in PATH)
    GECKODRIVER          path to geckodriver (default: geckodriver in PATH)
    YT2ABS_BASE_URL      server under test (default http://127.0.0.1:8080)
    E2E_VIDEO_URL        YouTube page used for the click (default Rick Astley)
    E2E_WORKDIR          scratch directory (default: a fresh temp dir)
    E2E_SCREENSHOT       optional path for a PNG of the /add page

Example against a local instance on :18099 without auth:

    FIREFOX_BIN=/opt/firefox/firefox GECKODRIVER=/opt/geckodriver \\
    YT2ABS_BASE_URL=http://127.0.0.1:18099 \\
    .venv/bin/python tests/e2e_bookmarklet.py
"""
from __future__ import annotations

import html
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

try:
    from selenium import webdriver
    from selenium.webdriver.firefox.options import Options
except ImportError as exc:  # pragma: no cover - optional dependency
    print(f"selenium is required: {exc}", file=sys.stderr)
    sys.exit(2)

FIREFOX = os.environ.get("FIREFOX_BIN", "firefox")
GECKODRIVER = os.environ.get("GECKODRIVER", "geckodriver")
SERVER = os.environ.get("YT2ABS_BASE_URL", "http://127.0.0.1:8080").rstrip("/")
VIDEO = os.environ.get("E2E_VIDEO_URL", "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=42s")
WORKDIR = Path(os.environ.get("E2E_WORKDIR") or tempfile.mkdtemp(prefix="yt2abs-bookmarklet-"))
SCREENSHOT = Path(os.environ.get("E2E_SCREENSHOT", WORKDIR / "bookmarklet-add-page.png"))
PROFILE = WORKDIR / "ff-profile"
GEKODRIVER_PORT = int(os.environ.get("E2E_GECKODRIVER_PORT", "4449"))
MARIONETTE_PORT = int(os.environ.get("E2E_MARIONETTE_PORT", "2828"))

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(("PASS  " if ok else "FAIL  ") + name + (f"  [{detail}]" if detail else ""))


def http(url: str, *, auth: bool = True, timeout: float = 10.0) -> str:
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode()


def wait_port(port: int, seconds: float = 60) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except OSError:
            time.sleep(0.4)
    return False


def main() -> int:
    if PROFILE.exists():
        shutil.rmtree(PROFILE)
    PROFILE.mkdir(parents=True)
    (PROFILE / "user.js").write_text(
        'user_pref("browser.toolbars.bookmarks.visibility", "always");\n'
        'user_pref("browser.shell.checkDefaultBrowser", false);\n'
        'user_pref("browser.startup.page", 0);\n'
        'user_pref("browser.aboutwelcome.enabled", false);\n'
        'user_pref("datareporting.policy.dataSubmissionEnabled", false);\n'
        'user_pref("toolkit.telemetry.reportingpolicy.firstRun", false);\n'
        'user_pref("remote.system-access-check.enabled", false);\n'
        f'user_pref("marionette.port", {MARIONETTE_PORT});\n'
    )

    ff = subprocess.Popen(
        [FIREFOX, "-headless", "-no-remote", "-profile", str(PROFILE),
         "-marionette", "-remote-allow-system-access", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    gd = None
    drv = None
    try:
        if not wait_port(MARIONETTE_PORT):
            check("Firefox started", False, "marionette port never opened")
            return 1
        version = subprocess.run([FIREFOX, "--version"], capture_output=True, text=True)
        check("Firefox started", True, version.stdout.strip())

        gd = subprocess.Popen(
            [GECKODRIVER, "--connect-existing", "--marionette-port", str(MARIONETTE_PORT),
             "--port", str(GEKODRIVER_PORT)],
            stdout=open(WORKDIR / "geckodriver.log", "wb"), stderr=subprocess.STDOUT,
        )
        if not wait_port(GEKODRIVER_PORT):
            check("geckodriver attached to the running Firefox", False)
            return 1
        drv = webdriver.Remote(command_executor=f"http://127.0.0.1:{GEKODRIVER_PORT}",
                               options=Options())

        def set_context(ctx: str) -> None:
            req = urllib.request.Request(
                f"http://127.0.0.1:{GEKODRIVER_PORT}/session/{drv.session_id}/moz/context",
                data=json.dumps({"context": ctx}).encode(),
                headers={"Content-Type": "application/json"}, method="POST",
            )
            urllib.request.urlopen(req, timeout=30).read()

        def click_bookmark(title: str) -> str:
            set_context("chrome")
            result = drv.execute_script(
                """
                const title = arguments[0];
                const items = [...document.querySelectorAll('#PlacesToolbarItems > toolbarbutton')];
                const button = items.find(i => i.label === title);
                if (!button) return 'not-found';
                button.doCommand ? button.doCommand() : button.click();
                return 'clicked:' + button.label;
                """, title)
            set_context("content")
            return result

        def wait_for_add_page(seconds: float = 20) -> str:
            deadline = time.time() + seconds
            while time.time() < deadline:
                if "/add?url=" in drv.current_url:
                    return drv.current_url
                time.sleep(0.5)
            return drv.current_url

        # --- 1. the bookmarklet exactly as the server ships it ---------------
        drv.get(f"{SERVER}/bookmarklet")
        time.sleep(1)
        page = drv.page_source
        pre = re.search(r"<pre>(.*?)</pre>", page, re.S)
        code = html.unescape(pre.group(1)).strip() if pre else ""
        check("bookmarklet page serves javascript: code",
              code.startswith("javascript:") and "/add?url=" in code, code[:60] + "...")
        check("bookmarklet targets the server under test", f"{SERVER}/add?url=" in code)

        # --- 2. a real YouTube watch page -----------------------------------
        drv.get(VIDEO)
        time.sleep(3)
        current = drv.current_url
        if "consent" in current:
            drv.add_cookie({"name": "SOCS", "value": "CAI", "domain": ".youtube.com"})
            drv.get(VIDEO)
            time.sleep(3)
            current = drv.current_url
        check("YouTube watch page loaded", "youtube.com/watch" in current, current)

        # --- 3. save the bookmark in the bookmarks toolbar -------------------
        set_context("chrome")
        guid = drv.execute_async_script(
            """
            const code = arguments[0], title = arguments[1];
            const done = arguments[arguments.length - 1];
            (async () => {
              try {
                const { PlacesUtils } = ChromeUtils.importESModule(
                  "resource://gre/modules/PlacesUtils.sys.mjs");
                let bm = await PlacesUtils.bookmarks.fetch({url: code});
                if (!bm) {
                  bm = await PlacesUtils.bookmarks.insert({
                    parentGuid: PlacesUtils.bookmarks.toolbarGuid,
                    url: code, title: title});
                }
                done(bm.guid);
              } catch (e) { done("ERR:" + e); }
            })();
            """, code, "Send to ABS")
        check("bookmark saved in the bookmarks toolbar",
              bool(guid) and not str(guid).startswith("ERR"), str(guid))

        stored = drv.execute_async_script(
            """
            const guid = arguments[0];
            const done = arguments[arguments.length - 1];
            (async () => {
              const { PlacesUtils } = ChromeUtils.importESModule(
                "resource://gre/modules/PlacesUtils.sys.mjs");
              const bm = await PlacesUtils.bookmarks.fetch({guid: guid});
              done(bm ? JSON.stringify({title: bm.title, url: bm.url.href}) : "missing");
            })();
            """, guid)
        info = json.loads(stored) if str(stored).startswith("{") else {}
        check("bookmark persisted with the exact javascript: URL",
              info.get("url") == code and info.get("title") == "Send to ABS",
              info.get("title", ""))

        # --- 4. click it in Firefox's own bookmarks toolbar -------------------
        check("toolbar bookmark clicked in the Firefox UI",
              str(click_bookmark("Send to ABS")).startswith("clicked"))

        landed = wait_for_add_page()
        check("click navigated to /add?url=...", "/add?url=" in landed, landed[:110])
        video_id = re.search(r"/watch\?v=([A-Za-z0-9_-]{11})", VIDEO)
        video_id = video_id.group(1) if video_id else ""
        check("video URL correctly encoded in the query",
              f"youtube.com%2Fwatch%3Fv%3D{video_id}" in landed)
        check("extra YouTube params survive encoding", "%26t%3D" in landed)

        body = drv.find_element("tag name", "body").text
        check("/add page reports a queued job",
              "Added to the queue" in body or "already in the system" in body,
              body.replace("\n", " ")[:90])
        drv.save_screenshot(str(SCREENSHOT))

        job = json.loads(http(f"{SERVER}/api/jobs/{video_id}"))
        check("server stored the job for the right video id",
              job.get("video_id") == video_id, job.get("status", ""))

        # --- 5. the second click must not duplicate the job -------------------
        drv.get(VIDEO)                      # back on a YouTube tab, like a user
        time.sleep(2)
        click_bookmark("Send to ABS")
        wait_for_add_page()
        body2 = drv.find_element("tag name", "body").text
        check("second click is idempotent (no duplicate job)",
              "już jest w systemie" in body2, body2.replace("\n", " ")[:90])

        jobs = json.loads(http(f"{SERVER}/api/jobs?limit=50"))
        rows = jobs if isinstance(jobs, list) else jobs.get("items", jobs.get("jobs", []))
        same = [row for row in rows if row.get("video_id") == video_id]
        check("exactly one job row for the video", len(same) == 1, f"{len(same)} rows")

        # --- 6. clicking it off YouTube fails cleanly -------------------------
        drv.get(f"{SERVER}/")
        time.sleep(1)
        click_bookmark("Send to ABS")
        wait_for_add_page()
        body3 = drv.find_element("tag name", "body").text
        check("off-YouTube click shows a clear server error",
              "nie prowadzi do YouTube" in body3, body3.replace("\n", " ")[:70])

        drv.save_screenshot(str(SCREENSHOT.with_name("bookmarklet-off-youtube.png")))
        print(f"\nscreenshot: {SCREENSHOT}")
    finally:
        try:
            if drv:
                drv.quit()
        except Exception:
            pass
        for proc in (gd, ff):
            if proc:
                proc.terminate()
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    for name in FAIL:
        print("  FAILED:", name)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
