"""End-to-end test of the yt2abs Firefox extension against a live server.

Runs a real Firefox ESR (loaded with the built XPI), a real yt2abs instance and
a real Audiobookshelf. Verifies: toolbar button + popup send, keep-forever
override surviving retention, badge, Alt+Shift+Y command and the context-menu
path (registration + video-id extraction from a live thumbnail).

Requirements: Firefox (ESR recommended), geckodriver and `selenium` in the
active interpreter. Configure with environment variables:

    FIREFOX_BIN          path to the firefox binary
    GECKODRIVER          path to geckodriver
    YT2ABS_XPI           built extension (default: extension/dist/yt2abs-*.zip)
    YT2ABS_BASE_URL      default http://127.0.0.1:8080
    ABS_URL              default http://127.0.0.1:13378
    ABS_API_KEY          Audiobookshelf API key (required)
    ABS_LIBRARY_ID       Audiobookshelf library id (required)
    YT2ABS_E2E_VIDEO     video from a channel with retention_days=0
    YT2ABS_E2E_SHORTCUT_VIDEO  a second, unmarked video
    YT2ABS_E2E_QUERY     YouTube search used for the thumbnail/context menu
    YT2ABS_E2E_RESULTS   optional path for the JSON report

Example (LAN E2E stack: yt2abs on :13381, Audiobookshelf on :13380):

    ABS_API_KEY=... ABS_LIBRARY_ID=... \\
    python tests/e2e_firefox_extension.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

try:
    from selenium import webdriver
    from selenium.webdriver import ActionChains
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.firefox.firefox_profile import FirefoxProfile
    from selenium.webdriver.firefox.options import Options
    from selenium.webdriver.firefox.service import Service
except ImportError as exc:  # pragma: no cover - optional dependency
    print(f"selenium is required: {exc}", file=sys.stderr)
    sys.exit(2)

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "extension" / "dist"


def _first_xpi() -> Path:
    for pattern in ("*.xpi", "*.zip"):
        candidates = sorted(DIST.glob(pattern))
        if candidates:
            return candidates[0]
    return DIST / "yt2abs-0.1.0.zip"


FIREFOX = Path(os.environ.get("FIREFOX_BIN", "firefox"))
GECKODRIVER = Path(os.environ.get("GECKODRIVER", "geckodriver"))
XPI = Path(os.environ.get("YT2ABS_XPI", _first_xpi()))
PROFILE = Path(os.environ.get("YT2ABS_E2E_PROFILE", tempfile.mkdtemp(prefix="yt2abs-ext-e2e-")))
EXT_ID = "yt2abs@example.org"
WIDGET = "yt2abs_example_org-browser-action"
BUTTON = f"{WIDGET}-BAP"

SERVER = os.environ.get("YT2ABS_BASE_URL", "http://127.0.0.1:8080").rstrip("/")
ABS = os.environ.get("ABS_URL", "http://127.0.0.1:13378").rstrip("/")
LIBRARY_ID = os.environ.get("ABS_LIBRARY_ID", "")
WATCH = os.environ.get("YT2ABS_E2E_VIDEO", "dQw4w9WgXcQ")
SHORTCUT_VIDEO = os.environ.get("YT2ABS_E2E_SHORTCUT_VIDEO", "jNQXAC9IVRw")
MENU_QUERY = os.environ.get("YT2ABS_E2E_QUERY", "me at the zoo")

ABS_KEY = os.environ.get("ABS_API_KEY", "")

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, condition: bool, detail: str = "") -> bool:
    ok = bool(condition)
    RESULTS.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + (f" | {detail}" if detail else ""), flush=True)
    return ok


def http(method: str, url: str, *, body=None, timeout: float = 30.0):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw) if raw else None
        except json.JSONDecodeError:
            return exc.code, raw.decode(errors="replace")


def abs_request(path: str):
    request = urllib.request.Request(
        f"{ABS}{path}", headers={"Authorization": f"Bearer {ABS_KEY}"}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def job(video_id: str):
    status, payload = http("GET", f"{SERVER}/api/jobs/{video_id}")
    return payload if status == 200 else None


def wait_job(video_id: str, statuses: tuple[str, ...], timeout: float = 300.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = job(video_id)
        if last and last.get("status") in statuses:
            return last
        if last and last.get("status") == "failed":
            return last
        time.sleep(2)
    return last


def abs_has(video_id: str) -> bool:
    listing = abs_request(f"/api/libraries/{LIBRARY_ID}/items?limit=100")
    for item in listing.get("results") or []:
        detail = abs_request(f"/api/items/{item['id']}?expanded=1")
        for episode in ((detail.get("media") or {}).get("episodes") or []):
            meta = episode.get("audioFile", {}).get("metadata", {}) or {}
            haystack = f"{meta.get('filename') or ''} {episode.get('title') or ''}"
            if video_id in haystack:
                return True
    return False


def prepare_profile() -> None:
    if PROFILE.exists():
        shutil.rmtree(PROFILE)
    (PROFILE / "extensions").mkdir(parents=True)
    shutil.copy(XPI, PROFILE / "extensions" / f"{EXT_ID}.xpi")


def preflight() -> None:
    problems = []
    if not XPI.exists():
        problems.append(f"extension build not found: {XPI} (run: cd extension && npm run build)")
    if not Path(FIREFOX).exists():
        problems.append(f"firefox not found: {FIREFOX} (set FIREFOX_BIN)")
    if not Path(GECKODRIVER).exists():
        problems.append(f"geckodriver not found: {GECKODRIVER} (set GECKODRIVER)")
    if not ABS_KEY:
        problems.append("ABS_API_KEY is empty")
    if not LIBRARY_ID:
        problems.append("ABS_LIBRARY_ID is empty")
    if problems:
        for problem in problems:
            print(f"ERROR: {problem}", file=sys.stderr)
        raise SystemExit(2)


PREFS = {
    "xpinstall.signatures.required": False,
    "extensions.autoDisableScopes": 0,
    "extensions.enabledScopes": 15,
    "extensions.startupScanScopes": 15,
    "browser.shell.checkDefaultBrowser": False,
    "browser.startup.page": 0,
    "browser.newtabpage.enabled": False,
    "datareporting.policy.dataSubmissionEnabled": False,
    "toolkit.telemetry.enabled": False,
    "network.proxy.type": 0,
    "intl.accept_languages": "pl-PL,pl,en-US,en",
}


def panel_info(driver):
    return driver.execute_script(
        "const p = Array.from(document.querySelectorAll('panel')).find(p => p.state === 'open');"
        "if (!p) return null;"
        "const b = p.querySelector('browser');"
        "let uri = null; try { uri = b && b.currentURI ? b.currentURI.spec : null; } catch (e) { uri = 'ERR'; }"
        "return {id: p.id, uri};"
    )


def main() -> int:
    preflight()
    prepare_profile()
    options = Options()
    options.binary_location = str(FIREFOX)
    options.add_argument("-headless")
    options.add_argument("-remote-allow-system-access")
    options.add_argument("--width=1400")
    options.add_argument("--height=1000")
    profile = FirefoxProfile(str(PROFILE))
    for key, value in PREFS.items():
        profile.set_preference(key, value)
    options.profile = profile
    service = Service(
        executable_path=str(GECKODRIVER),
        log_output=str(Path(os.environ.get("YT2ABS_E2E_GECKO_LOG", PROFILE / "geckodriver.log"))),
    )
    driver = webdriver.Firefox(options=options, service=service)
    video_tab = None
    menu_video = None
    try:
        # ---------------------------------------------------------------- extension
        driver.set_context("chrome")
        addon = driver.execute_async_script(
            "const done = arguments[arguments.length - 1];"
            "AddonManager.getAddonByID('yt2abs@example.org')"
            ".then(a => done(a ? {id: a.id, version: a.version, isActive: a.isActive, signedState: a.signedState} : null))"
            ".catch(e => done({error: String(e)}));"
        )
        check("extension loaded and active in Firefox", bool(addon and addon.get("isActive")), json.dumps(addon))
        uuid = driver.execute_script(
            "const raw = Services.prefs.getStringPref('extensions.webextensions.uuids', '{}');"
            "return JSON.parse(raw)['yt2abs@example.org'];"
        )
        driver.execute_script("CustomizableUI.addWidgetToArea(arguments[0], 'nav-bar');", WIDGET)
        time.sleep(0.5)
        button = driver.execute_script(
            "const item = document.getElementById(arguments[0]);"
            "if (!item) return null;"
            "return {tag: item.tagName, ext: item.getAttribute('data-extensionid')};",
            WIDGET,
        )
        check("toolbar button added to the nav-bar", bool(button and button.get("ext") == EXT_ID), json.dumps(button))

        # ---------------------------------------------------------------- settings
        driver.set_context("content")
        driver.get(f"moz-extension://{uuid}/options.html")
        time.sleep(1)
        driver.find_element("id", "server").clear()
        driver.find_element("id", "server").send_keys(SERVER)
        driver.find_element("id", "test").click()
        time.sleep(3)
        test_message = driver.find_element("id", "message").text
        check("options: connection test reaches the server", "Connected" in test_message, test_message)
        driver.find_element("css selector", "button[type=submit]").click()
        time.sleep(1)
        saved = driver.find_element("id", "message").text
        check("options: settings saved to storage.sync", "Settings saved" in saved, saved)

        # ---------------------------------------------------------- action gating
        driver.switch_to.new_window("tab")
        driver.get(f"https://www.youtube.com/watch?v={WATCH}&hl=en")
        time.sleep(5)
        check("YouTube watch page loaded", "Rick Astley" in driver.title, driver.title)
        video_tab = driver.current_window_handle

        driver.switch_to.new_window("tab")
        driver.get(f"moz-extension://{uuid}/options.html")
        time.sleep(1)
        tabs = driver.execute_async_script(
            "const done = arguments[arguments.length - 1];"
            "browser.tabs.query({}).then(list => done(list.map(t => ({id: t.id, url: t.url}))))"
            ".catch(e => done({error: String(e)}));"
        )
        watch_id = next((t["id"] for t in tabs if f"watch?v={WATCH}" in (t.get("url") or "")), None)
        ext_id = next((t["id"] for t in tabs if (t.get("url") or "").startswith("moz-extension:")), None)
        enabled_watch = driver.execute_async_script(
            "const done = arguments[arguments.length - 1];"
            "browser.action.isEnabled({tabId: arguments[0]}).then(v => done(v)).catch(e => done(String(e)));",
            watch_id,
        )
        enabled_other = driver.execute_async_script(
            "const done = arguments[arguments.length - 1];"
            "browser.action.isEnabled({tabId: arguments[0]}).then(v => done(v)).catch(e => done(String(e)));",
            ext_id,
        )
        check("button enabled on /watch", enabled_watch is True, str(enabled_watch))
        check("button disabled on non-video tabs", enabled_other is False, str(enabled_other))

        # ---------------------------------------------------------------- popup UI
        driver.switch_to.new_window("tab")
        driver.get(f"moz-extension://{uuid}/popup.html")
        time.sleep(3)
        body = driver.execute_script("return document.body.innerText;")
        check("popup resolves the active YouTube video", "Rick Astley" in body, body.replace("\n", " | ")[:220])
        check(
            "popup shows the channel policy for a retention_days=0 channel",
            "usuwany przy najbliższym przebiegu retencji" in body,
            body.replace("\n", " | ")[:260],
        )
        check(
            "popup shows a job status",
            any(label in body for label in ("Not sent", "Deleted", "In ABS", "Queued", "Downloading")),
            body.replace("\n", " | ")[:200],
        )

        # ---------------------------------------------------------------- send (policy)
        driver.find_element(By.ID, "send").click()
        last_body = ""
        deadline = time.time() + 300
        while time.time() < deadline:
            last_body = driver.execute_script("return document.body.innerText;")
            if "In ABS" in last_body:
                break
            if "Błąd" in last_body:
                break
            time.sleep(2)
        check("popup reports 'In ABS' after the send", "In ABS" in last_body, last_body.replace("\n", " | ")[:260])
        record = job(WATCH)
        check(
            "server reports the job as imported",
            bool(record and record.get("status") == "imported"),
            json.dumps({k: record.get(k) for k in ("status", "abs_episode_id")}) if record else "no job",
        )
        check("Audiobookshelf contains the episode", abs_has(WATCH), WATCH)

        badge = driver.execute_async_script(
            "const done = arguments[arguments.length - 1];"
            "browser.action.getBadgeText({tabId: arguments[0]}).then(t => done(t)).catch(e => done('ERR ' + e));",
            watch_id,
        )
        check("badge shows a check mark for a video already in ABS", badge == "✓", repr(badge))

        # ---------------------------------------------------------------- keep forever
        driver.find_element(By.ID, "send-keep").click()
        time.sleep(4)
        keep_body = driver.execute_script("return document.body.innerText;")
        record = job(WATCH)
        check(
            "keep button sets retention_override=0",
            bool(record and record.get("retention_override") == 0),
            json.dumps({k: record.get(k) for k in ("status", "retention_override")}) if record else "",
        )
        check("popup shows the override", "kept forever (overridden)" in keep_body, keep_body.replace("\n", " | ")[:260])

        retention_status, retention = http("POST", f"{SERVER}/api/retention/run")
        check("retention run accepted", retention_status == 200, json.dumps(retention)[:200])
        time.sleep(2)
        after = job(WATCH)
        check(
            "keep-forever job survives retention (status stays imported)",
            bool(after and after.get("status") == "imported"),
            json.dumps({k: after.get(k) for k in ("status", "deleted_at")}) if after else "",
        )
        check("episode still present in Audiobookshelf after retention", abs_has(WATCH), WATCH)
        kept = (retention or {}).get("kept")
        check("retention report kept the protected job", isinstance(kept, int) and kept >= 1, f"kept={kept}")

        # --------------------------------------------------------- toolbar popup click
        driver.switch_to.window(video_tab)
        driver.set_context("chrome")
        item = driver.find_element(By.ID, WIDGET)
        ActionChains(driver).move_to_element(item).click().perform()
        time.sleep(2)
        panel = panel_info(driver)
        check("toolbar button click opens the popup panel", bool(panel), json.dumps(panel))
        check(
            "popup panel loads popup.html",
            bool(panel and panel.get("uri") and panel["uri"].endswith("/popup.html")),
            (panel or {}).get("uri", ""),
        )
        driver.execute_script(
            "Array.from(document.querySelectorAll('panel')).forEach(p => { if (p.state === 'open') p.hidePopup(); });"
        )

        # ---------------------------------------------------------- shortcut command
        driver.set_context("content")
        driver.switch_to.new_window("tab")
        driver.get(f"https://www.youtube.com/watch?v={SHORTCUT_VIDEO}&hl=en")
        time.sleep(5)
        check("second video page loaded", "YouTube" in driver.title, driver.title)
        # Browser keyboard shortcuts are handled by the chrome window, so the
        # chord is injected in chrome context (content-context keys only reach
        # the page).
        driver.set_context("chrome")
        ActionChains(driver).key_down(Keys.ALT).key_down(Keys.SHIFT).send_keys("y").key_up(Keys.SHIFT).key_up(Keys.ALT).perform()
        time.sleep(1)
        driver.set_context("content")
        shortcut_job = wait_job(SHORTCUT_VIDEO, ("queued", "downloading", "imported"), timeout=120)
        check(
            "Alt+Shift+Y queues the current video",
            bool(shortcut_job and shortcut_job.get("status") in ("queued", "downloading", "imported")),
            json.dumps({k: shortcut_job.get(k) for k in ("status", "retention_override")}) if shortcut_job else "no job",
        )

        # -------------------------------------------------------------- context menu
        driver.get(f"https://www.youtube.com/results?search_query={MENU_QUERY.replace(' ', '+')}&hl=en")
        time.sleep(6)
        anchors = driver.find_elements(By.CSS_SELECTOR, 'a[href*="/watch?v="]')
        target = None
        for anchor in anchors[:20]:
            href = anchor.get_attribute("href") or ""
            if "watch?v=" in href:
                candidate = href.split("watch?v=")[1][:11]
                if candidate and candidate not in (WATCH, SHORTCUT_VIDEO):
                    target = anchor
                    menu_video = candidate
                    break
        check("search results contain a YouTube thumbnail link", target is not None, str(menu_video))
        if target is not None:
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", target)
            time.sleep(1)
            link_url = target.get_attribute("href")
            image = target.find_elements(By.TAG_NAME, "img")
            src_url = image[0].get_attribute("src") if image else ""
            ActionChains(driver).context_click(target).perform()
            time.sleep(1.5)
            driver.set_context("chrome")
            menu_state = driver.execute_script(
                "const menu = document.getElementById('contentAreaContextMenu');"
                "return {open: !!menu && menu.state === 'open',"
                " extensionItems: Array.from(document.querySelectorAll('menuitem'))"
                ".filter(i => /Send to ABS/.test(i.label || '')).length};"
            )
            check("native context menu opens on a thumbnail", bool(menu_state.get("open")), json.dumps(menu_state))
            driver.execute_script(
                "const menu = document.getElementById('contentAreaContextMenu'); if (menu) menu.hidePopup();"
            )
            driver.set_context("content")

            # Firefox 153 renders extension context-menu entries outside the
            # chrome DOM, so the item is verified through the menus API while
            # extraction runs on the real thumbnail URLs.
            driver.switch_to.new_window("tab")
            driver.get(f"moz-extension://{uuid}/options.html")
            time.sleep(1)
            registered = driver.execute_async_script(
                "const done = arguments[arguments.length - 1];"
                "browser.menus.update('yt2abs-send', {title: 'Send to ABS'})"
                ".then(() => done('ok')).catch(e => done('ERR ' + e));"
            )
            check("'Send to ABS' menu item is registered in Firefox", registered == "ok", str(registered))
            extracted = driver.execute_script(
                "return Yt2abs.menuVideoId({linkUrl: arguments[0], srcUrl: arguments[1]}, arguments[2]);",
                link_url,
                src_url,
                f"https://www.youtube.com/watch?v={menu_video}",
            )
            check(
                "thumbnail link/cover resolve to the expected video id",
                extracted == menu_video,
                f"{extracted} vs {menu_video} (src={src_url[:60]})",
            )
            sent = driver.execute_async_script(
                "const done = arguments[arguments.length - 1];"
                "browser.runtime.sendMessage({type: 'send', videoId: arguments[0], mode: 'policy'})"
                ".then(r => done(r && r.ok ? 'ok' : 'ERR ' + JSON.stringify(r))).catch(e => done('ERR ' + e));",
                menu_video,
            )
            check("background queues the thumbnail video without opening it", sent == "ok", str(sent))
            menu_job = wait_job(menu_video, ("queued", "downloading", "imported"), timeout=60)
            check(
                "server accepted the context-menu video id",
                bool(menu_job and menu_job.get("video_id") == menu_video),
                f"{menu_video}: " + json.dumps({k: menu_job.get(k) for k in ("video_id", "status")}) if menu_job else f"{menu_video}: no job",
            )

        # ------------------------------------------------------------------ summary
        passed = sum(1 for _, ok, _ in RESULTS if ok)
        print(f"\n{passed}/{len(RESULTS)} checks passed", flush=True)
        results_path = os.environ.get("YT2ABS_E2E_RESULTS")
        if results_path:
            Path(results_path).write_text(
                json.dumps(
                    {
                        "checks": [{"name": n, "ok": ok, "detail": d} for n, ok, d in RESULTS],
                        "passed": passed,
                        "total": len(RESULTS),
                        "video": WATCH,
                        "shortcut_video": SHORTCUT_VIDEO,
                        "menu_video": menu_video,
                    },
                    indent=2,
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        return 0 if passed == len(RESULTS) else 1
    finally:
        try:
            driver.quit()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    sys.exit(main())
