# Bookmarklet “Send to ABS” — a bookmark instead of an add-on

The bookmarklet is a temporary replacement for the paused Firefox extension.
It works in every version of Firefox, requires no signing or installation, and
does exactly one thing: it takes the address of the open YouTube video and sends
it to yt2abs so the audio lands in Audiobookshelf.

**What it does not do** (that stays in the extension, if someone finishes it): it
does not show a “✓ w ABS” badge, it has no context menu on thumbnails, no
keyboard shortcut and no “Keep forever” button. The bookmarklet always uses
the channel retention policy (`retention_override` = none).

## Variant 1 — drag the ready-made button (simplest)

1. Go to `https://<server>/bookmarklet` in Firefox.
3. Enable the bookmarks bar: **Ctrl+Shift+B** (or *View → Toolbars → Bookmarks Toolbar*).
4. Drag the blue **“▶ Send to ABS”** button onto the bookmarks bar.
5. Open any video on YouTube and click the new bookmark.

The server generates the bookmark code itself and embeds its public address in
it (`api.public_url` or `YT2ABS_PUBLIC_URL`), so you do not need to copy it.

## Variant 2 — add the bookmark manually

When dragging does not work (e.g. Firefox in fullscreen mode or a remote
desktop):

1. Open `https://<server>/bookmarklet` and copy the code from the field below the button.
2. Right-click the bookmarks bar → **Add Bookmark…** (or **Ctrl+D** on any page,
   then *Edytuj*).
3. Name: `Send to ABS`; address: paste the copied `javascript:…` code.
   If Firefox strips the `javascript:` prefix, add it back manually.
4. Save and click the bookmark on a YouTube video page.

### Code to paste

Replace `<server>` with your instance address (no trailing slash, e.g.
`https://yt2abs.example.com` or `http://192.168.1.10:13381`):

```javascript
javascript:(()=>{const u=encodeURIComponent(location.href);location.href="https://<server>/add?url="+u;})()
```

For the version working in the LAN under `http://192.168.1.10:13381`:

```javascript
javascript:(()=>{const u=encodeURIComponent(location.href);location.href="http://192.168.1.10:13381/add?url="+u;})()
```

The server shows the same code (only with a slash and quotes) on the
`/bookmarklet` page — if they differ, the version from the page is the correct one.

## Variant 3 — new tab, YouTube stays open

The version from variant 1/2 moves the current tab to the yt2abs page (the video
disappears from the screen). If you prefer to keep the YouTube tab, use this
bookmark:

```javascript
javascript:(()=>{window.open("https://<server>/add?url="+encodeURIComponent(location.href),"_blank");})()
```

Firefox then opens a new tab with the status, and the video stays in the old
one. If the browser blocks the popup, allow popups for `youtube.com` (the icon
next to the address bar).

## What happens after clicking

The bookmark opens `GET https://<server>/add?url=<video-address>`. The server
checks whether the link points to a YouTube video and adds it to the queue:

- first time: **“Dodano do kolejki.”**,
- next time: **“This video is already in the system — showing its status.”**
  (refreshing the page does not retry the import; retrying is a separate button),
- a link outside YouTube: **“Ten link nie prowadzi do YouTube.”**.

This is a plain navigation, not `fetch` — YouTube blocks cross-origin requests
(CSP), so the bookmark cannot talk to the server in the background. `/add` is
the write endpoint: it validates the address itself and accepts only YouTube
videos. No credentials are involved — the server is LAN-only.

## Common problems

| Symptom | Cause and solution |
| --- | --- |
| “Ten link nie prowadzi do YouTube.” | The bookmark was clicked on the yt2abs page (or another page that is not a video). Go back to the video tab and click there. |
| The bookmark does nothing | Firefox did not execute `javascript:` — check that the bookmark address starts exactly with `javascript:` (no space before it). |
| A new tab opens but is empty | Popups are blocked (variant 3) — allow them for `youtube.com`. |
| The bookmark points to an old server address | The address is “baked into” the bookmark code. Fix `api.public_url` on the server and fetch the bookmark again from `/bookmarklet`. |

## Verification

Tested on a real Firefox **ESR 153.5.1** (headless, geckodriver 0.37.1) with a
real `youtube.com/watch?v=…&t=42s` page and a live yt2abs instance.

Checked (18/18 assertions, `tests/e2e_bookmarklet.py`):

- the code from `/bookmarklet` is valid `javascript:` and points to the tested server,
- `/bookmarklet` opens without credentials and the bookmark code contains no
  secret,
- the bookmark is saved to the **bookmarks bar** with the exact `javascript:…` address,
- clicking the button on the bar opens `/add?url=https%3A%2F%2Fwww.youtube.com%2Fwatch%3Fv%3D…%26t%3D42s`
  (the `&t=` parameters go through `encodeURIComponent`),
- the server creates a `queued` job for the correct `video_id`,
- a second click does not duplicate the job (“This video is already in the system”),
- clicking outside YouTube ends with a clear server error.

Reproducing the test (requires Firefox, geckodriver and `selenium`):

```bash
FIREFOX_BIN=/opt/firefox/firefox GECKODRIVER=/opt/geckodriver \
YT2ABS_BASE_URL=http://127.0.0.1:18099 \
.venv/bin/python tests/e2e_bookmarklet.py
```
