# yt2abs

You mark a YouTube video -> the audio lands in Audiobookshelf as a podcast
episode (one channel = one podcast), and after a configured time the episode is
removed (retention is counted from the moment you mark it).

> ### ⚠️ LAN only, no authentication
>
> yt2abs has no user accounts and no API token: the reachable port *is* the
> access control. Run it on a private network, bind the listener to a LAN
> address (`BIND_ADDR`) and never publish the port on a public address. Any
> client that can reach it can queue downloads, change channel policies and
> hard-delete episodes through retention.
>
> ### Legal
>
> This project is not affiliated with, endorsed by or sponsored by YouTube or
> Audiobookshelf. You are responsible for the content you download and for
> complying with YouTube's terms of service and with copyright law in your
> jurisdiction. The software is provided under the MIT licence, without warranty
> (see [`LICENSE`](LICENSE)).

Features: a FastAPI API (`POST /api/jobs`, `GET /api/jobs/{id}`, `GET /add`), a
worker with a durable SQLite queue, a yt-dlp download pipeline (audio + metadata
+ cover + atomic `mv`), a thin Audiobookshelf API client (scan, match by
filename, edit episode/podcast, hard delete), per-channel retention policies,
SponsorBlock, Discord/ntfy notifications, a WebUI, a bookmarklet and a Firefox
extension.

## Requirements

- Python 3.11+ (the module runs on the stdlib; `PyYAML` is optional, and
  without it `config.yaml` can be a JSON document)
- `yt-dlp` in `PATH` (tested on 2026.07.04)
- `ffmpeg`/`ffprobe` in `PATH` (needed for `mute` mode)

## Installation

```bash
git clone <repo-url> yt2abs && cd yt2abs
uv venv .venv
uv pip install -e '.[dev]'          # FastAPI, uvicorn, yt-dlp[default], PyYAML, httpx
cp config.example.yaml config.yaml  # fill in abs.url, abs.library_id, abs.library_path, paths.*
cp .env.example .env                # secrets: ABS_API_KEY, webhooks
```

Without `uv` (plain venv + pip):

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

`yt-dlp` and `ffmpeg`/`ffprobe` must be in `PATH` (or point to the binary via
`YT2ABS_YTDLP=/path/to/yt-dlp`). The Firefox extension has its own
`package.json` in [`extension/`](extension) — see
[Firefox extension](#firefox-extension-yt2abs).

## Tests

```bash
uv venv .venv && uv pip install -e '.[dev]'       # FastAPI, uvicorn, httpx, PyYAML
make test                                          # unit tests (no network)
make e2e-playlist                                  # phone playlist E2E (offline)
make e2e-security                                  # real uvicorn: open LAN surface, rate limit, XFF trust
make e2e                                           # SponsorBlock E2E on a real video
YT2ABS_BASE_URL=http://127.0.0.1:8099 make e2e-service  # E2E against a running service
DISCORD_WEBHOOK_URL=... make e2e-discord           # E2E worker events -> Discord (real delivery)
ABS_API_KEY=... make e2e-retention                      # E2E retention/update (in a container)
YT2ABS_CONTAINER=... YT2ABS_CONFIG_HOST=... YT2ABS_BASE_URL=... \
  ABS_URL=... ABS_LIBRARY_ID=... make e2e-retention-restart  # E2E config change + restart
# If Docker runs on a different host without Python (e.g. an Unraid box), add
# YT2ABS_SSH_HOST=root@unraid (and optionally YT2ABS_SSH_KEY=...): the script drives
# Docker over SSH and writes the config remotely.
YT2ABS_BASE_URL=http://127.0.0.1:8082 ABS_API_KEY=... make e2e-webui  # WebUI/API E2E
```

`make test` selects `.venv/bin/python` on its own if it exists.

## Service: API + worker + SQLite + ABS client

### Local run (without Docker)

```bash
cp config.example.yaml config.yaml
# in config.yaml set abs.library_id, abs.library_path and paths.*
export ABS_API_KEY="<key from ABS: Settings -> API Keys>"
export YT2ABS_CONFIG=config.yaml
.venv/bin/python -m yt2abs        # http://127.0.0.1:8080
```

The worker, retention and the yt-dlp updater start together with the API (one
video at a time). Disabling the worker (and with it retention and autoupdate):
`YT2ABS_WORKER_ENABLED=0` (for example for endpoint tests). Retention can be run
manually: `curl -X POST http://127.0.0.1:8080/api/retention/run` — it responds `202` immediately and
works in the background; the state and report are in `GET /api/retention`. For
debugging/E2E there is a synchronous variant: `POST
/api/retention/run?wait=true`.

### Docker Compose (alongside ABS)

```bash
cp config.example.yaml config.yaml
cp .env.example .env               # ABS_API_KEY, ABS_LIBRARY_ID, PUID/PGID
docker compose up -d --build
curl -fsS http://127.0.0.1:8099/health
```

Compose mounts **the same host directory** of the library at `/youtube-podcasts`
in both yt2abs and ABS (`YT2ABS_LIBRARY_HOST`). Only then do the paths stored by
ABS (`item.path`) match what yt2abs sees. It is best for `tmp_dir` to be on the
same filesystem (fast `rename`); separate Docker bind mounts are handled by
`copy` + atomic publish. Defaults: `/mnt/user/media/youtube-podcasts`
and `/mnt/user/media/yt2abs-tmp`.

**The port is not published on all interfaces.** Compose binds
`${YT2ABS_BIND_ADDR:-127.0.0.1}:${YT2ABS_HTTP_PORT:-8099}` — by default only the
host can see the port (a reverse proxy on the same host or the Docker network).
For LAN-only access set `YT2ABS_BIND_ADDR=192.168.1.10` (the host address), never
`0.0.0.0` on a machine with a public interface. Safest: do not publish the port
at all (remove `ports:`) and put NPM on the same Docker network, entering the
**exact** NPM address as `/32` in `api.trusted_proxies` (e.g. `192.168.1.5/32` —
the same one you see as `client_addr` in the access log). To check what is
actually listening:

```bash
docker compose port yt2abs 8080      # e.g. 127.0.0.1:8099
ss -ltnp | grep 8099
```

### Image from CI (dev / latest)

Every push to `dev` builds and publishes `ghcr.io/damianeek/yt2abs:dev`;
`main` publishes `:latest`, and a `v*` tag — semver versions (`0.3.0`, `0.3`).
Images are multi-arch (`linux/amd64`, `linux/arm64`) with an SBOM and a
provenance attestation. Instead of building locally, pull the image from CI:

```bash
export YT2ABS_IMAGE=ghcr.io/damianeek/yt2abs:v0.3.2
docker compose pull
docker compose up -d
```

The image also ships `config.example.yaml` (`docker run --rm --entrypoint cat
ghcr.io/damianeek/yt2abs:v0.3.2 /app/config.example.yaml`), so a standalone run
starts from that template. Or run it on its own (no compose file):

```bash
docker run -d --name yt2abs --restart unless-stopped \
  -p 127.0.0.1:8099:8080 \
  -v /mnt/user/appdata/yt2abs/config.yaml:/app/config.yaml:ro \
  -v /mnt/user/appdata/yt2abs/data:/data \
  -v /mnt/user/media/yt2abs-tmp:/tmp-downloads \
  -v /mnt/user/media/youtube-podcasts:/youtube-podcasts:rw \
  -e TZ=Europe/Warsaw \
  -e YT2ABS_DATA_DIR=/data -e YT2ABS_TMP_DIR=/tmp-downloads \
  -e YT2ABS_LIBRARY_DIR=/youtube-podcasts \
  -e ABS_URL=http://192.168.1.10:13378 -e ABS_API_KEY=... -e ABS_LIBRARY_ID=... \
  ghcr.io/damianeek/yt2abs:v0.3.2
```

`:sha-<7>` is an immutable tag for rollbacks, and every tag can be pinned by
digest (`@sha256:...`, printed by the build and shown on the release page). For
a private GHCR package, run `docker login ghcr.io` first; making the package
public (see the link below) lets any host pull it without credentials. Branch rules, tags and GitHub configuration:
[`docs/branches-and-releases.md`](docs/branches-and-releases.md). Unraid:
[`unraid/yt2abs.xml`](unraid/yt2abs.xml) and [`unraid/README.md`](unraid/README.md).

### Endpoints

| Method | Path | Description |
| --- | --- | --- |
| `POST` | `/api/jobs` | `{"url": "...", "retention_override": 7}` → `202` new, `200` existing/re-queued; `0` = keep forever |
| `GET` | `/api/jobs/{video_id}` | status: `queued` / `downloading` / `imported` / `failed` / `deleted` + `retention.deadline` |
| `GET` | `/api/policy?channel_id=&name=` | effective channel retention policy (for the extension) |
| `GET` | `/api/jobs?status=&limit=` | job list + counters + retention deadline for each video |
| `POST` | `/api/jobs/{video_id}/retry` | retries only a `failed` job (clears the error and attempt counter) |
| `POST` | `/api/jobs/{video_id}/retention` | `{"override": 0}` = keep forever, `null` or `"default"` = return to the channel policy (field required, range 0..36500) |
| `DELETE` | `/api/jobs/{video_id}` | deletes the episode from ABS (hard) and marks it `deleted`; `502` when ABS does not respond |
| `GET` | `/api/channels` | channels from `config.yaml`, the database and the panel + effective policy |
| `POST` | `/api/channels` | `{"channel": "UC..."}` or a YouTube link → new channel in the panel |
| `PUT` | `/api/channels/{channel_id}` | edit policy; `"retention_days": "default"` restores `config.yaml` |
| `DELETE` | `/api/channels/{channel_id}` | removes the change stored in the panel (back to `config.yaml`) |
| `GET` | `/api/abs/status` | live ABS status: reachability, server version, `library_id` |
| `GET` | `/api/retention` | retention job state + report of the last run (`dry_run`, `reconcile_blocked`, `planned`) |
| `POST` | `/api/retention/run` | manual retention run: `202` + background run; `?wait=true` = synchronous (E2E) |
| `GET` | `/api/retention/preview` | preview of the next run: zero writes, zero deletions, `planned` list |
| `GET` | `/api/system` | yt-dlp version, ABS configuration, limits, default policies |
| `POST` | `/api/ytdlp/update` | check for and install the latest yt-dlp |
| `GET` | `/api/notifications` | notification state: which events are enabled, where the choice comes from (no URL) |
| `PUT` | `/api/notifications` | `{"events": ["imported","failed"]}` → stored in SQLite + effective immediately |
| `POST` | `/api/notifications/test` | sends a test notification to Discord (`200` delivered, `409` no webhook, `502` error) |
| `GET` | `/add?url=...` | bookmarklet fallback (navigation, not fetch — YouTube CSP); no credentials needed |
| `GET` | `/bookmarklet` | page with the bookmarklet and Firefox installation instructions |
| `GET` | `/settings` | WebUI panel: selection of operations that send a notification |
| `GET` | `/health` | public probe: only `status` (`ok`/`degraded`) and thread liveness; details in `/api/system` |

There is **no authentication**: the service is meant for a trusted LAN, so the
reachable port is the access control. Every page and every `/api/*` endpoint
answers without credentials; `GET /health` stays minimal (no diagnostics) and
`GET /api/system` carries the details. Never publish the port on a public
address — bind it to one LAN interface (`BIND_ADDR`) and keep the reverse-proxy
ACL in place if one is used.

URL normalization: `watch`, `youtu.be`, `shorts`, `embed`, `live`, `v`; the
parameters `&t`, `&list`, `si` are ignored, and the key is `video_id`. Links
outside YouTube → `400`. Re-`POST`ing the same video does not create a duplicate;
`failed` and `deleted` are retried from scratch.

### WebUI: queue, channels, configuration

Address: `http://<host>:8099/` (Docker Compose) or `http://127.0.0.1:8080/`
(local run). The page is plain HTML/CSS/JS served by the same FastAPI — no
separate container and no CSS framework. It works on a phone (cards instead of
tables, 42 px buttons, no horizontal scrolling).

No login: the panel calls the API directly. There is no token, no session
cookie and no `?token=` bootstrap — the WebUI works as soon as the page loads,
as long as the client is on the LAN.

Tabs:

- **Queue** — an “add video” form (optionally retention: channel policy /
  keep forever / custom number of days) and a job list with thumbnail, title,
  channel, status (`Queued`, `Downloading`, `Imported`, `Failed`, `Deleted`),
  date added, deadline from the current policy and error text. Filters:
  **Active**, **All**, **Errors**, **Deleted**. Actions: **Retry** for `failed`,
  **Keep forever** (`retention_override = 0`; the button becomes **Use channel
  policy**), **Delete** (hard delete of the episode through ABS) and **Add
  again** for deleted ones. The list refreshes every 5 s.
- **Channels** — a list from `config.yaml`, the database and the panel: name,
  `channel_id`, retention (**Keep forever** = `null`), SponsorBlock (mode +
  categories), unfinished protection. Saving takes effect immediately
  (retention recalculates the deadline from `added_at`) and is durable: changes
  go to the `settings` table in SQLite, so they survive a container restart.
  `config.yaml` remains a read-only base (compose mounts it `:ro`); **Reset to
  config.yaml** removes the entry from the panel. A new channel is added via
  `UC...` or a `youtube.com/@handle` link (resolved by yt-dlp on add).
- **Configuration** — default retention and `protect_unfinished`, live ABS status
  (server version, `library_id`, path, connection error), yt-dlp version and
  the retention schedule; manual retention run and yt-dlp update buttons, plus
  shortcuts to Discord notifications and the bookmarklet.

Zero semantics (important for manual changes): `retention_override = 0` on a
video means **keep forever**, while `retention_days: 0` in a channel policy
means **delete on the next run**; `null` = keep. The deadline is always counted
from `added_at` (the moment it was marked), not from the publication date.

### Pipeline and resilience

1. `yt-dlp --dump-single-json` → `channel_id`, title, description, publication date, chapters.
2. `bestaudio[ext=m4a]` + `--embed-metadata --embed-thumbnail --embed-chapters` → `tmp_dir`.
3. `cover.jpg` from the channel avatar (`avatar_uncropped`) on the first video.
4. atomic `os.replace` → `<library_path>/<Channel>/<YYYY-MM-DD> - <Title> [<video_id>].m4a`.
   The channel folder name is remembered in the `channels` table by `channel_id`.
   When tmp and the library are separate Docker bind mounts (`EXDEV`), the file
   is copied to `.<name>.part` in the target folder and atomically renamed —
   ABS never sees a partial `.m4a`.
5. `POST /api/libraries/{id}/scan`, polling until the item and episode appear
   (`audioFile.metadata.filename`), `PATCH` of the episode (title, description + link, `pubDate`).
6. `status=imported` + `abs_item_id` / `abs_episode_id`.

Queue: `videos.status=queued`, `next_attempt_at`; the worker takes the oldest
due job, marks it `downloading` and increments `attempts`. Failure → retry with
backoff (`worker.retry_backoff_seconds`, default 60/300/900 s), after
`worker.max_attempts` → `failed`. After a restart `downloading` returns to the queue.

### ABS client

`yt2abs/abs_client.py` (stdlib `urllib`, injectable transport in tests):
`scan_library`, `scan_item`, `list_library_items`, `find_item_by_path`,
`find_episode_by_filename`, `update_episode`, `edit_podcast`,
`delete_episode(hard=True)`. API version: ABS 2.37.1 (episodes in `media.episodes`).

## Retention

The retention job runs in the same process as the worker, by default **every
hour** (`retention.interval_minutes`) and once at startup
(`retention.run_on_startup`). Manually: `POST /api/retention/run`,
preview: `GET /api/retention`.

Policy hierarchy (highest wins):

1. `videos.retention_override` — per-video override (`0` = keep forever),
2. `channels.<channel_id>.retention_days` — channel policy (`null` = keep,
   `0` = delete on the next run),
3. `defaults.retention_days` — 14 days by default (`null` = keep).

The deadline is always counted **from `added_at`** (the moment it was marked),
not from the publication date, and **recalculated on every run** from the
current policy. That is why changing a channel from `7` to `null` before the
deadline protects all its episodes, while changing it to `0` deletes them on the
next run.

`protect_unfinished: true` (channel or defaults): if an episode has started but
unfinished listening in Audiobookshelf (`mediaProgress`), deletion is postponed
to `2 * N` days after it was marked. After that deadline the episode disappears
despite unfinished listening. A failure to read progress postpones deletion
until the next run (safer than deleting blindly).

Deletion happens **exclusively through the ABS API**:
`DELETE /api/podcasts/{item}/episode/{episode}?hard=1` — ABS removes the episode
entry **and the audio file**. The podcast folder, `cover.jpg` and settings
remain, so an empty channel is still a valid podcast. After success:
`status=deleted`, `deleted_at`; on failure the episode stays `imported` and the
job retries on the next run.

The same run performs **reconcile**: if a podcast or episode was deleted
manually in ABS, the SQLite row gets `status=deleted` (instead of a perpetual
`imported`). Re-marking a deleted video (`POST /api/jobs` or the bookmarklet)
clears the old `abs_*`/`file_path`, resets `added_at` and downloads the video
again.

### Reconcile safety net

A wrong `ABS_URL` (e.g. an E2E instance instead of production), a stopped ABS or
a restored Audiobookshelf database must not mass-mark the library as deleted.
That is why reconcile has three safeguards:

- **No listing = no run.** When `GET /api/libraries/{id}/items` fails (wrong
  `ABS_URL`, stopped ABS, restored database), the listing is empty while the
  database is non-empty (wrong `library_id`), or the `paths.library_path`
  directory does not exist or is empty (missing mount, e.g. an empty Docker
  bind mount), the whole run is skipped: zero writes, zero deletions, an entry
  in the report and an alert.
- **Disappearance limit.** If a single run would mark more rows than
  `min(retention.reconcile_max_deletions, ceil(retention.reconcile_max_fraction *
  checked))`, the run ends with a `reconcile_blocked` report and **zero writes**
  (including for ordinary retention deletions in that run). `0` disables the
  given limit. The percentage limit also applies to small libraries, so for a
  deliberate mass cleanup in ABS you must raise `reconcile_max_deletions`.
- **File and filename.** A row gets `deleted` only when the file really
  disappeared from disk. If an episode has a different ID but the same filename
  — yt2abs refreshes `abs_*` (report `resynced`) instead of treating it as
  deleted. When the file still exists, the row stays `imported` and the report
  contains an error to investigate.

### Preview and dry run

`retention.dry_run: true` (or `YT2ABS_RETENTION_DRY_RUN=1`) computes exactly the
same plan but does not write statuses, does not delete episodes and does not
send notifications. `GET /api/retention/preview` does the same on demand and
returns a `planned` list. Useful before the first run on a production library:

```bash
curl -s http://192.168.1.10:8099/api/retention/preview | jq '.planned, .reconcile_blocked'
```

### Listening progress across multiple accounts

`protect_unfinished` reads ABS progress. By default (`abs.progress_user: ""`)
yt2abs fetches the account list via `GET /api/users` and checks the progress of
**all** listeners; this requires an API key with administrator permissions. If
that fails with 403, it falls back to the key owner and the report contains a
warning. Any other error (timeout, 5xx, unknown account) makes the progress
**unknown** in that run: episodes with `protect_unfinished` past the deadline
are postponed (not deleted), and the report and alert signal this.
`abs.progress_user: <login or id>` limits the check to a single account (that
account must be visible to the API key — usually this requires an admin key or
pointing at the key owner).

### Configuration changes require a restart

Policies from `config.yaml` are read at process startup. Changing
`retention_days` in the file only takes effect after a container restart
(`docker compose up -d --force-recreate`); in the WebUI panel a channel policy
can be changed without a restart (the entry in SQLite takes precedence over the
file).

## Network exposure, rate limit and reverse proxy

The service has **no authentication**: it is designed for a private network, so
the reachable port *is* the access control. The intended setup is a LAN address
or a reverse proxy on the same host with its own LAN ACL (e.g. NPM:
`yt2abs.example.com` → `192.168.1.10:8099`). Never forward the port from the WAN.

- Bind to one interface: `BIND_ADDR` / `YT2ABS_BIND_ADDR` (default `127.0.0.1`
  for a local run; the container sets `0.0.0.0` internally and the published
  port decides who can reach it).
- **Host names:** requests are accepted when the `Host` header is an IP literal,
  `localhost` or a single-label name (`nas`). A dotted name (`yt2abs.example.com`)
  must be listed in `api.allowed_hosts` (env `YT2ABS_ALLOWED_HOSTS`) or match
  `api.public_url`; anything else gets `403`. This blocks DNS rebinding, where a
  malicious page resolves its own domain to your LAN address. State-changing
  requests (POST/PUT/PATCH/DELETE) are additionally refused when the browser
  reports a foreign `Origin` or a cross-site `Sec-Fetch-Site`.
- Any client that can reach the port can queue downloads, change channel
  policies and hard-delete episodes through retention. That is the accepted
  trade-off of LAN-only mode — do not put it on a public address.
- Rate limit per IP: `api.rate_limit_per_minute` (default 60/min for the API and
  pages) and `api.public_rate_limit_per_minute` (10/min for `GET /add`) →
  `429` with `Retry-After`. A value of `0` **disables** the given limit. The
  bucket table is bounded (LRU, 10,000 keys), so rotating the key does not
  consume memory. The key is the exact IPv4 address or the `/64` network for
  IPv6 — an IPv6 client usually controls the whole `/64`, so rotating addresses
  within it does not create new buckets.
- The `X-Forwarded-For` / `X-Real-IP` headers are honored **only** when the
  client's TCP address (`scope.client`) belongs to `api.trusted_proxies` (CIDR
  list). **All lines** of the header are read (HAProxy with `option forwardfor`
  appends its own line after the client line) and the rightmost field of the
  **last** `X-Forwarded-For` line is used (the one the proxy appended) or
  `X-Real-IP`; the left fields are controlled by the client. By default
  `trust_proxy: false` and an empty list. `trust_proxy: true` without a list is
  a compatibility mode (trusts everyone) and only logs a warning. A list that
  covers **everyone** (`0.0.0.0/0`, `::/0`, and also `0.0.0.0/1` +
  `128.0.0.0/1`) also only warns — set `api.trust_any_peer: true` (env:
  `YT2ABS_TRUST_ANY_PEER=1`) when that is deliberate. Entries with host bits
  (`192.168.1.5/8`) are rejected, and prefixes wider than `/16` (IPv4) and `/48`
  (IPv6) log a warning. Uvicorn starts with `--no-proxy-headers` so it does not
  overwrite `scope.client` with its own, less strict interpretation of XFF.
- `api.public_url` is optional; it tells the app its own https address behind a
  proxy. It is used to build the bookmarklet link and to keep redirects on
  https (a `/add/` typo must not send the browser to plain http). It enables
  no authentication.

NPM/nginx example (HTTPS only, LAN ACL):

```nginx
location / {
    proxy_pass http://192.168.1.10:8099;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

In `config.yaml` set the proxy allow-list then: `trust_proxy: false`,
`trusted_proxies: ["192.168.1.5/32"]` — the exact NPM address seen as
`client_addr` in the access log (usually the reverse proxy address, not the whole
`172.16.0.0/12`). An entry with host bits (`192.168.1.5/8`) is rejected at startup
because it would silently widen trust to the whole network. An address in the
form `::ffff:192.168.1.5/128` (dual-stack listener) is reduced to `192.168.1.5/32`.

**The proxy must always append its own `X-Forwarded-For` line** — nginx:
`proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for`, HAProxy:
`option forwardfor` without `if-none`. If the proxy only forwards the client
header (or sets only `X-Real-IP`), the client still chooses its own rate-limit
bucket, because the rightmost entry of the last line comes from it.

## Bookmarklet in Firefox

1. Open `https://<server>/bookmarklet` in Firefox (no login step).
2. Enable the bookmarks bar (Ctrl+Shift+B) and drag the
   **“Send to ABS”** button onto the bar.
3. On a YouTube video page click the bookmark — it opens
   `https://<server>/add?url=<encoded-address>` with the download status.

The bookmarklet is a plain navigation (`location.href`), not `fetch`, because
YouTube blocks cross-origin requests (CSP). No cookie or header is involved:
`GET /add` simply queues the video.
`/add` validates the URL and accepts only YouTube videos (other links → `400`).
The public address to paste into the bookmarklet is set by `api.public_url`
(or `YT2ABS_PUBLIC_URL`).

Full instructions (manually adding a bookmark, ready-made code, a variant that
opens a new tab, common problems) and a description of testing in a real
Firefox: [`docs/bookmarklet-firefox.md`](docs/bookmarklet-firefox.md). E2E test:
`tests/e2e_bookmarklet.py` (Firefox + geckodriver + selenium).

## Firefox extension (yt2abs)

A native extension (Manifest V3) as a more convenient alternative to the
bookmarklet: a toolbar button, a context menu on thumbnails, a keyboard shortcut
and a status preview without opening the server page.

### Features

| Element | Behavior |
| --- | --- |
| Toolbar icon | active on `/watch` and `/shorts`; click opens the popup |
| Popup | title, channel, channel retention policy, job status, two buttons |
| “Add to queue” | `POST /api/jobs` with `retention_override=null` (inherits the channel policy) |
| “Keep forever” | `retention_override=0` — retention never deletes the episode |
| Badge ✓ | the video is already in ABS (`GET /api/jobs/{video_id}` → `imported`) |
| Context menu | “Send to ABS” on a thumbnail or link, without opening the video |
| <kbd>Alt</kbd>+<kbd>Shift</kbd>+<kbd>Y</kbd> | sends the current video |

The popup fetches the channel policy from `GET /api/policy` (matching by
`channel_id`, then by channel name). The per-video override
(`retention_override`) takes precedence over the channel policy.

### Installation

1. Build or download the signed `.xpi` file (see below).
2. In Firefox: `about:addons` → gear icon → **Zainstaluj dodatek z pliku…**
   → select the `.xpi`.
3. Pin the icon to the bar (Extensions panel → pin) — Firefox hides new add-ons
   in the extensions panel by default.
4. Open the extension settings (⚙ in the popup), enter the server address,
   click **Test connection**, then **Save**. The address is the same
   reverse proxy as ABS over HTTPS (e.g. `https://abs.example.com`) or the
   direct LAN address (`http://192.168.1.10:13381`).

Only the address is stored in the browser's `storage.sync` — there is no token
to keep. The extension does not collect any data
(`data_collection_permissions: none`).

### Build and signing (AMO, `unlisted` channel)

Requirements: Node.js 18+; the scripts use `npx web-ext@10`.

```bash
cd extension
npm test          # unit and integration tests (node --test, no dependencies)
npm run lint      # web-ext lint: 0 errors and 0 warnings
npm run build     # dist/yt2abs-0.1.0.zip
```

Signing the **unlisted** version (outside the public AMO catalog): generate API
keys at https://addons.mozilla.org/developers/addon/api/key/ and set them in the
environment:

```bash
export WEB_EXT_API_KEY=user:00000000:000
export WEB_EXT_API_SECRET=...
cd extension
npm run sign      # web-ext sign --channel=unlisted
```

`web-ext sign` saves the signed `.xpi` file in `extension/dist/`; install it via
`about:addons`. The `unlisted` channel means it is not listed in the public
catalog, but the file is signed by AMO and works in regular Firefox. The add-on
ID (`yt2abs@example.org`) must remain unchanged — otherwise AMO treats the next
version as a new add-on.

### Development without signing

```bash
cd extension
FIREFOX_BIN=/usr/bin/firefox npm start   # web-ext run = temporary installation
```

Alternatively `about:debugging#/runtime/this-firefox` → **Load Temporary
Add-on** → `extension/src/manifest.json`. Firefox Release requires signing;
ESR and Developer Edition honor `xpinstall.signatures.required=false`
(used in E2E tests).

### Permissions

- `*://*.youtube.com/*` (content script) — reads the title, name and channel ID
  from the current page so the popup can show the retention policy,
- `http://*/*`, `https://*/*` — connection to the yt2abs server at any address
  (self-hosted, the address is not known at build time),
- `storage`, `tabs`, `menus`, `notifications`.

### Notes

- Statuses in the popup: `Queued`, `Downloading`, `In ABS`, `Failed`, `Deleted`.
- The extension has its own CSP policy without `upgrade-insecure-requests`, so
  it also works with a server over `http://` in the LAN. If the browser
  administrator enforces a `LocalNetworkAccess` policy, the server address must
  be added to `SkipDomains`.
- The context menu appears on `youtube.com` pages for links and images; it does
  not require opening the video.
- **Status: work paused** (2026-10-21). The extension works in development
  mode, but there is no signed `.xpi` file, so for now Firefox Release users
  must use the bookmarklet. MVP scope, risks and conditions for returning to
  the topic is paused; the bookmarklet below is the supported replacement.

## yt-dlp updates

At startup and once a day (`ytdlp.interval_hours`, default 24 h) the service
checks the version on PyPI. If a newer one exists, it downloads the standalone
`yt-dlp_linux` binary (or `aarch64`/`macos`/`exe`) to
`<paths.data_dir>/bin/yt-dlp` (in the container `/data/bin`, i.e. a durable
volume), validates it with `--version` and atomically replaces the file. When
GitHub is unavailable, it tries `python -m pip install --upgrade yt-dlp`.

The log contains the old and new version and the method (`binary`/`pip`), and
the state is visible in `GET /api/system` (`ytdlp.version`, `last_update`; the
public `/health` shows only `status` and thread liveness). When necessary:
`POST /api/ytdlp/update`. Disabling: `ytdlp.update: false` or
`YT2ABS_YTDLP_UPDATE=0`.

## Environment variables

Keep secrets exclusively in the environment (the `.env` in the repo is ignored
by `.gitignore`). A template of all variables with comments:
[`.env.example`](.env.example). When running without Docker, `.env` is **not**
loaded automatically — export it (`set -a; . ./.env; set +a`) or pass the
variables explicitly; `docker compose` loads `.env` itself.

For variables marked “overrides `config.yaml`”, the environment takes precedence
over the file.

### Service

| variable | default | meaning |
| --- | --- | --- |
| `YT2ABS_CONFIG` | `config.yaml` / `config.yml` in cwd | configuration file path |
| `YT2ABS_HOST` / `YT2ABS_PORT` | `0.0.0.0` / `8080` | listen address (local run) |
| `YT2ABS_LOG_LEVEL` | `info` | log level |
| `YT2ABS_WORKER_ENABLED` | `1` | `0` = API only (no worker, retention or autoupdate) |
| `YT2ABS_PUBLIC_URL` | empty | public HTTPS address of the reverse proxy (links, redirects) |
| `YT2ABS_API_RATE_LIMIT` | `60` | requests/min/IP for `/api/*`; `0` disables the limit |
| `YT2ABS_PUBLIC_RATE_LIMIT` | `10` | requests/min/IP for `GET /add`; `0` disables the limit |
| `YT2ABS_TRUST_PROXY` | `1` | trust `X-Forwarded-For` / `X-Real-IP` (reverse proxy); `0` when the port is exposed directly |

### Paths, retention and yt-dlp (override `config.yaml`)

| variable | default | meaning |
| --- | --- | --- |
| `YT2ABS_DATA_DIR` | `paths.data_dir` from config | durable data (SQLite, `bin/`) |
| `YT2ABS_TMP_DIR` | `paths.tmp_dir` | download working directory |
| `YT2ABS_LIBRARY_DIR` | `paths.library_path` | ABS library as seen by the process |
| `YT2ABS_RETENTION_INTERVAL_MINUTES` | `60` | retention run interval in minutes |
| `YT2ABS_RETENTION_ON_STARTUP` | `1` | run retention at startup |
| `YT2ABS_RETENTION_DRY_RUN` | `0` | `1` = report only, no deletion |
| `YT2ABS_YTDLP` | `yt-dlp` | yt-dlp command/binary |
| `YT2ABS_YTDLP_BIN_DIR` | `<data_dir>/bin` | location of the standalone binary |
| `YT2ABS_YTDLP_UPDATE` | `1` | yt-dlp auto-update |
| `YT2ABS_YTDLP_UPDATE_INTERVAL_HOURS` | `24` | PyPI check interval |

### Audiobookshelf (override `config.yaml`, `abs` section)

| variable | default | meaning |
| --- | --- | --- |
| `ABS_URL` | `http://localhost:13378` | ABS address as seen by the process |
| `ABS_API_KEY` | — | ABS API key (Settings → API Keys); **required for import** |
| `ABS_LIBRARY_ID` | — | podcast library ID (`GET /api/libraries`) |
| `ABS_LIBRARY_PATH` | — | library path in the ABS account (the same as the volume) |
| `ABS_PROGRESS_USER` | empty | ABS account whose started episodes are protected; empty = all |

### Notifications

| variable | default | meaning |
| --- | --- | --- |
| `DISCORD_WEBHOOK_URL` | empty | full webhook URL (secret); empty = disabled |
| `DISCORD_USERNAME` | empty | overridden sender name |
| `DISCORD_NOTIFY_EVENTS` | `imported,failed` | which events to send (`all` / `none`) |
| `DISCORD_NOTIFY_DISABLED` | `0` | disables sending even with a URL set |
| `DISCORD_NOTIFY_TIMEOUT` | `10` | request timeout (s) |
| `NTFY_TOPIC` | empty | ntfy topic; empty = channel disabled (zero requests) |
| `NTFY_URL` | `https://ntfy.sh` | ntfy server (root only, no path) |
| `NTFY_TOKEN` | empty | token for a protected topic |
| `NTFY_NOTIFY_EVENTS` | `failed,deleted` | which events to send |
| `NTFY_NOTIFY_DISABLED` | `0` | disables sending even with a topic set |
| `NTFY_NOTIFY_TIMEOUT` | `10` | request timeout (s) |
| `NOTIFY_DISABLED` | `0` | global switch for all notifications |

### `docker compose` only

| variable | default | meaning |
| --- | --- | --- |
| `PUID` / `PGID` | `99` / `100` | file owner (Unraid: `nobody:users`; locally `$(id -u)`/`$(id -g)`) |
| `YT2ABS_HTTP_PORT` | `8099` | port published on the host |
| `YT2ABS_LIBRARY_HOST` | `/mnt/user/media/youtube-podcasts` | host library volume |
| `YT2ABS_TMP_HOST` | `/mnt/user/media/yt2abs-tmp` | host working directory |
| `YT2ABS_DATA_HOST` | `./data` | host durable data directory |

Variables used only by the E2E scripts (`YT2ABS_BASE_URL`, `YT2ABS_CONTAINER`,
`YT2ABS_CONFIG_HOST`, `YT2ABS_SSH_HOST`, `YT2ABS_SSH_KEY`, `WEB_EXT_API_KEY`,
`WEB_EXT_API_SECRET`) are described in the [Tests](#tests) chapter.

## Configuration (`config.yaml`)

The template is [`config.example.yaml`](config.example.yaml) - copy it as
`config.yaml`. Unknown sections are ignored, so settings of other parts of the
service (`abs`, ...) can live in the same file.

```yaml
defaults:
  retention_days: 14          # null = keep forever; counted from marking
                              # 0 = delete on the next run
  protect_unfinished: true    # do not delete a started but unfinished episode
  sponsorblock: false         # off by default = behavior as before
  sponsorblock_api: https://sponsor.ajay.app
  ytdlp_args: []              # e.g. ["--extractor-args", "youtube:player_client=web_embedded"]

api:
  # No authentication: LAN-only, the port is the control. Never publish it.
  # Optional public HTTPS address of the reverse proxy (bookmarklet links).
  public_url: https://yt2abs.example.com
  rate_limit_per_minute: 60    # 0 = no limit
  public_rate_limit_per_minute: 10
  # X-Forwarded-For / X-Real-IP are trusted only from addresses in this CIDR list.
  # Enter the EXACT proxy address as /32 (e.g. 192.168.1.5/32) - the same one
  # you see as `client_addr` in the access log. An entry with host bits (192.168.1.5/8)
  # is rejected, and a prefix shorter than /16 (IPv4) / /48 (IPv6) warns.
  # Empty = headers ignored.
  trust_proxy: false          # true without a list: trusts every peer (warns)
  trusted_proxies: []

abs:
  # Empty = progress of all accounts via /api/users (admin key);
  # set a login/id to check only one account.
  progress_user: ""

retention:
  interval_minutes: 60
  run_on_startup: true
  dry_run: false               # true = report only, zero writes and deletions
  reconcile_max_deletions: 20  # reconcile safety limit (0 = no limit)
  reconcile_max_fraction: 0.2  # percentage limit from 10 rows (0 = no limit)

playlist:                     # adding from a phone: a single unlisted playlist
  enabled: false              # disabled by default
  url: https://www.youtube.com/playlist?list=PLxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
  interval_minutes: 15        # min 5; kill-switch: YT2ABS_PLAYLIST_ENABLED=0

ytdlp:
  update: true
  interval_hours: 24

channels:
  UCzz4CoEgSgWNs9ZAvRMhW2A:
    name: Everything Electric CARS
    retention_days: 7
    sponsorblock: remove              # cut sponsor segments from the file
  UCKEEP000000000000000000:
    name: Archive channel
    retention_days: null              # keep, even when defaults change
```

Hierarchy: **video override > channel setting > defaults**. An unknown channel
gets `defaults`, so a missing entry == today's behavior. `retention_days: 0` in
a channel policy means “delete on the next run”; `retention_override: 0` on a
single video means “keep forever” (the Phase 1a contract).

The channel key must be a real YouTube ID (`UC` + 22 characters). A typo, handle
or URL matches no row and the channel silently gets `defaults` — that is why
`yt2abs check` prints a warning for such keys and a list of channels from the
config that are not in the database.

## Adding from a phone (unlisted YouTube playlist)

Instead of the bookmarklet, you can drop a video into an **unlisted** YouTube
playlist that yt2abs polls in the background. A new `video_id` goes into the
queue exactly like `POST /api/jobs` (status `queued`, source `playlist`), so the
channel policy, SponsorBlock and retention work unchanged.

```yaml
playlist:
  enabled: true
  url: https://www.youtube.com/playlist?list=PLxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
  interval_minutes: 15        # min 5; latency is acceptable
  # timeout: 120              # yt-dlp listing limit (seconds)
```

```dotenv
# .env - polling kill-switch: 1/true = no change, 0/false = disable;
# does not enable the playlist (playlist.enabled in config.yaml decides that)
YT2ABS_PLAYLIST_ENABLED=0
```

How it works:

- the poller starts with the application (a separate thread alongside the worker
  and retention), with the first run immediately, then every `interval_minutes`;
- listing is `yt-dlp --flat-playlist --print "%(id)s" --no-warnings
  --ignore-errors` + `defaults.ytdlp_args` with a timeout — no download and no
  metadata;
- new videos get `source=playlist`; **every existing entry** (including
  `failed` and `deleted`) is skipped, so the poller does not resurrect episodes
  deleted by retention;
- a listing error (no network, deleted playlist, bot check) is only a `WARNING`
  in the logs and a retry on the next tick — it does not stop the application
  and does not send ntfy; ntfy notifications still concern only
  `failed`/`deleted` from the worker. `GET /health` is public and shows only
  `last_report.error: true|false`; the redacted error detail (without playlist
  ID/URL) is in `GET /api/system`;
- removing a video from the playlist **does not delete** the episode from ABS
  and does not change retention — the channel policy still governs the
  lifecycle;
- the status is visible in `GET /health` → `playlist: {enabled, configured,
  interval_minutes, last_run, last_report}` — without the playlist ID and URL.

Limitations:

- **unlisted playlist only** (accessible via link) — a private playlist requires
  logging in. The poller does not add cookies itself; if you have `--cookies` in
  `defaults.ytdlp_args`, the listing inherits them along with the rest of the
  pipeline (at your own risk);
- latency up to `interval_minutes` (minimum 5) — a deliberate trade-off, this is
  not instant adding;
- no feedback to the phone: the confirmation is the episode in ABS, and an error
  is visible in the logs/WebUI and (optionally) in ntfy for `failed`;
- the first run with a non-empty playlist **queues all** videos not yet in the
  database — before enabling it, it is worth emptying the playlist or accepting
  such a backfill;
- `failed` from the playlist does not return automatically — fixing it requires
  “Retry” in the WebUI/API (the poller never re-queues existing entries);
- only a link to the playlist page (`youtube.com/playlist?list=...`) is
  accepted; a `watch?v=...&list=...` link is rejected so that a single mark does
  not pull in the whole playlist.

Offline verification (stdlib, fake yt-dlp and ABS, no network):

```bash
make e2e-playlist      # or: .venv/bin/python tests/e2e_playlist.py
```

## Discord notifications

The worker sends notifications to a Discord webhook (`yt2abs/notify.py`, stdlib
only, zero new dependencies):

| event      | when                                         | default |
| ---------- | -------------------------------------------- | ------- |
| `imported` | job imported into Audiobookshelf             | yes     |
| `failed`   | job permanently failed after `max_attempts`  | yes     |
| `retry`    | failed attempt, job returns to the queue     | no      |

Configuration via environment variables:

| variable                  | meaning                                                |
| ------------------------- | ------------------------------------------------------ |
| `DISCORD_WEBHOOK_URL`    | full webhook URL (secret); empty = notifications off    |
| `DISCORD_USERNAME`       | optional sender name                                    |
| `DISCORD_NOTIFY_DISABLED`| `1` disables sending even with a URL set                |
| `DISCORD_NOTIFY_TIMEOUT` | request timeout in seconds (default 10)                 |
| `DISCORD_NOTIFY_EVENTS`  | `imported,retry,failed`, `all` or `none`                |

### Selecting operations in the WebUI (`/settings`)

The panel at `http://<host>:<port>/settings` (also linked on the main page) lets
you select **which operations send a notification**: `imported`, `retry`,
`failed`. Saving (`PUT /api/notifications`) takes effect immediately — the
worker uses the same notifier instance, so no restart is needed — and is
durable: the selection goes to the `settings` table in SQLite.

Hierarchy: **panel entry > `DISCORD_NOTIFY_EVENTS` > default value**
(`imported,failed`). An empty event list = job notifications disabled (the test
button still works). The webhook URL is never returned by the API or shown in
the panel — it comes exclusively from the environment. The “Send test
powiadomienie” button (`POST /api/notifications/test`) gives real delivery
verification without waiting for a job.

The selection status is also visible in `GET /api/notifications` (`source`:
`webui` / `env` / `default`) and in `GET /api/system` (`notifications.events`).

A missing URL, HTTP error, timeout or DNS failure does not stop the worker — the
notification is skipped/logged and the queue keeps working. Manual test:

```bash
source .env
.venv/bin/python -m yt2abs.cli notify-test
```

Details and payload format: [`docs/discord-notifications.md`](docs/discord-notifications.md).

## ntfy notifications (errors only)

A separate channel on [ntfy](https://ntfy.sh) (`yt2abs/ntfy.py`, stdlib only)
sends **errors only** — successes and retries never reach this topic:

| event     | when                                              | default |
| --------- | ------------------------------------------------- | ------- |
| `failed`  | job permanently failed after `max_attempts`       | yes     |
| `deleted` | retention deleted the episode from Audiobookshelf and disk | yes     |

The `imported` and `retry` events have no hook in the ntfy module, so they
cannot be enabled. The `failed` message contains the video title, `video_id` and
the reason (error), and `deleted` — the video title, `video_id`, the reason
(“retention: N days overdue”) and the filename (without the full path). The
channel title is **best-effort**: when the error occurs before metadata is
fetched (private video, deleted, regional block), the message says “unknown channel — error
before fetching metadata”, and the notification title does not
end with a dash. Both events have a link to the video in the `click` field.

A bulk deletion in a single retention run is **one summary message** (count, up
to 10 items, “… and N more”), sent after the run ends and outside its lock — so
cleaning N episodes does not consume the ntfy limit that `failed` alerts need.

Configuration via environment variables (like Discord; **disabled** by default):

| variable                 | meaning                                                      |
| ------------------------ | ------------------------------------------------------------ |
| `NTFY_TOPIC`            | target topic; **empty = zero HTTP requests**                  |
| `NTFY_URL`              | server (default `https://ntfy.sh`; the scheme can be omitted) |
| `NTFY_TOKEN`            | optional token (`Authorization: Bearer`) for protected topics |
| `NTFY_NOTIFY_DISABLED`  | `1` disables sending even with a topic set                    |
| `NTFY_NOTIFY_TIMEOUT`   | request timeout in seconds (default 10, max 30)               |
| `NTFY_NOTIFY_EVENTS`    | `failed,deleted`, `all` or `none` (both by default)           |

`NTFY_URL` must be the **server root**: a path, query, fragment and userinfo
are rejected at startup with a clear warning, because JSON publish only works on
`/`, and the topic must be given exclusively in `NTFY_TOPIC`. 301/302/303
redirects are not followed — the token never reaches another host.

Example in `.env`:

```dotenv
NTFY_TOPIC=yt2abs-example
# NTFY_URL=https://ntfy.sh
# NTFY_TOKEN=tk_...
```

Behavior and security:

- no `NTFY_TOPIC` or `NTFY_NOTIFY_DISABLED=1` → the notifier is a no-op and
  makes no request (even with `NTFY_URL` set);
- dedupe per `(event, video_id)` per hour + a minimum 1 s gap between different
  messages — a repeated retention run does not spam;
- only a 2xx with ntfy confirmation (`event=message`, `id`, matching topic) is
  considered delivered; a 2xx HTML response (e.g. an application page behind a
  redirect) is an error;
- transient errors (5xx, 429, connection error) are retried at most 2 times with
  a backoff of 1 s and 3 s; 429 respects `Retry-After` (default 5 s); a read
  timeout is **not** retried, to avoid publishing the message twice;
- an HTTP/timeout/DNS error is logged and never stops the worker or retention;
- reconcile (episode manually deleted in ABS) does **not** send `deleted` — that
  is not a deletion by retention;
- the topic and token are not returned by the API or logged; `GET /api/system`
  shows only `ntfy: {enabled, configured, events}`.

Manual test and E2E (local HTTP sink, no network):

```bash
set -a; . ./.env; set +a
.venv/bin/python -m yt2abs.cli ntfy-test
make e2e-ntfy
```

## SponsorBlock per channel

Values of the `sponsorblock` field (channel or `defaults`):

| value                          | what happens                                              |
| ------------------------------ | --------------------------------------------------------- |
| `off` (default)                | nothing - file and metadata unchanged                     |
| `remove`                       | yt-dlp cuts segments (`--sponsorblock-remove`)            |
| `mark`                         | yt-dlp keeps the audio, adds chapters (`--sponsorblock-mark`) |
| `mute`                         | yt2abs mutes segments locally (ffmpeg), duration unchanged |
| `remove:sponsor,selfpromo`     | as above + an explicit category list                      |
| `mute:all,-filler`             | shortcuts `all` / `default` and exclusions `-category`    |
| `{mode: mute, categories: [sponsor]}` | dictionary form (e.g. from a future WebUI)         |
| `[sponsor, selfpromo]`         | list == `remove` with those categories                    |

The available categories are the same ones yt-dlp knows: `sponsor`, `intro`,
`outro`, `selfpromo`, `preview`, `filler`, `interaction`, `music_offtopic`,
`hook` (for `mark` additionally `poi_highlight`, `chapter`). An invalid
category/mode is a configuration error, not a silent ignore.

### Inspecting settings and downloading from the CLI

```bash
python3 -m yt2abs.cli check    --config config.yaml
python3 -m yt2abs.cli sb-args  --config config.yaml --channel-id UCzz4CoEgSgWNs9ZAvRMhW2A
python3 -m yt2abs.cli fetch    --url "https://www.youtube.com/watch?v=..." \
                               --config config.yaml --dest downloads/
```

`fetch` detects the video's `channel_id` itself (one metadata request), selects
the channel policy and returns JSON with the result (`sponsorblock`, `muted`,
`segments`, `ytdlp_args`). `--channel-id` allows skipping detection.

### How it connects to the MVP code

- `yt2abs.config.load_config()` + `config.channel(channel_id)` - the channel
  policy (`retention_days`, `protect_unfinished`, `sponsorblock`).
- `yt2abs.downloader.build_audio_args(setting=...)` - yt-dlp arguments
  (`--format bestaudio[ext=m4a]` + `--sponsorblock-*`).
- `yt2abs.downloader.download_for_channel(...)` - a full download with the
  channel policy, including post-download muting.

Implementation details and E2E results: [`docs/sponsorblock.md`](docs/sponsorblock.md).

## Phase 3 — extras (SponsorBlock + ntfy)

Phase 3 adds two independent extras, both **disabled by default**: per-channel
SponsorBlock policy and ntfy error notifications. The basic flow does not
change — a marked video goes through the worker to Audiobookshelf, and retention
removes it according to the channel policy (`override > channel > defaults`).

### SponsorBlock: per-channel policy

The `sponsorblock` setting works in `defaults` and in `channels` (the channel
takes precedence). Modes: `off` (nothing), `remove` (yt-dlp cuts segments),
`mark` (chapters, audio unchanged) and `mute` (yt2abs mutes locally with ffmpeg,
file duration unchanged). You can specify categories, e.g.
`remove:sponsor,selfpromo` or `mute:all,-filler`.

```yaml
defaults:
  sponsorblock: false          # off - MVP behavior, file unchanged
channels:
  UCzz4CoEgSgWNs9ZAvRMhW2A:    # Everything Electric CARS
    retention_days: 7
    sponsorblock: remove       # == remove:sponsor
  UCEXAMPLEmute00000000000:
    sponsorblock: mute:sponsor,selfpromo
```

The configuration is validated at startup (`make check`): an invalid mode or
category is an error, not a silent ignore. The full list of values, categories
and how `mute` works — the [SponsorBlock per channel](#sponsorblock-per-channel)
chapter.

### ntfy notifications (only `failed` and `deleted`)

The optional ntfy topic receives **errors only**: a permanently failed import
(`failed`) and an episode actually deleted by retention (`deleted`). Successes
and retries have no hook, so they cannot be enabled. Manual deletion in the
WebUI/ABS and reconcile do not send `deleted`.

```dotenv
# .env - empty NTFY_TOPIC = zero HTTP requests (disabled by default)
NTFY_TOPIC=yt2abs-example
# NTFY_URL=https://ntfy.sh
# NTFY_TOKEN=tk_...
```

A missing topic (or `NTFY_NOTIFY_DISABLED=1`) means the notifier is a no-op;
`GET /api/system` shows only `ntfy: {enabled, configured, events}`, never the
topic. Details, dedupe and the variable table — the
[ntfy notifications](#ntfy-notifications-errors-only) chapter.

### Beyond the original Phase 3 scope

- **Adding from a phone (unlisted YouTube playlist)** — **DELIVERED**; the
  playlist poller is disabled by default (`playlist.enabled: false`), description
  and limitations in the
  [Adding from a phone](#adding-from-a-phone-unlisted-youtube-playlist) chapter.
- **A web page for editing channel policies** — **DELIVERED** later, as a
  separate task (the
  [WebUI: queue, channels, configuration](#webui-queue-channels-configuration)
  chapter).

### Verification

`tests/e2e_phase3.py` (stdlib, no network) runs the real application, worker,
pipeline and retention with fake yt-dlp/ABS and a local ntfy sink:

```bash
.venv/bin/python tests/e2e_phase3.py     # scenario A: extras enabled,
                                         # scenario B: extras disabled
```

It checks marking → import into ABS → retention, the `--sponsorblock-*` flags
passed to yt-dlp (remove/mark/off), `failed`/`deleted` notifications, dedupe and
the absence of any requests with an empty `NTFY_TOPIC`.

## Project status

Implemented (2026-10-20):

- FastAPI API: `POST /api/jobs`, `GET /api/jobs/{id}`, `GET /api/jobs`,
  `GET /add`, `/bookmarklet`, `GET /health`, `GET /api/system`,
  `GET/POST /api/retention*`, `POST /api/ytdlp/update`; `GET /` with a simple
  form;
- worker with a durable SQLite queue (`videos`, `channels`), retry with backoff,
  `downloading` recovery after a restart;
- pipeline: yt-dlp metadata, `bestaudio[ext=m4a]` + tags/cover/chapters,
  channel avatar as `cover.jpg`, atomic `mv`, ABS scan + matching by filename +
  episode `PATCH` (title, description with link, `pubDate`);
- **retention**: hourly job, override > channel > defaults hierarchy, deadline
  from `added_at`, `protect_unfinished` up to `2 * N`, hard delete through the
  ABS API, reconcile of manually deleted episodes, an empty podcast remains;
- **network model**: LAN-only and **without authentication** (the port is the
  access control), per-IP rate limit with LRU (IPv6 key `/64`),
  `trusted_proxies` allow-list for XFF (all header lines, exact proxy `/32`,
  `trust_any_peer` as a deliberate escape hatch), HTTPS via reverse proxy;
- **bookmarklet** `GET /add?url=` + `/bookmarklet` page with Firefox
  instructions;
- **yt-dlp auto-update** at startup and every 24 h (binary in `/data/bin`, pip
  fallback), status in `/api/system` (public `/health` only `status` + `alive`);
- ABS client 2.37.1 (`scan`, `find-by-filename`, `edit episode/podcast`,
  `delete episode hard=1`, `mediaProgress`);
- `Dockerfile` (Python 3.12 + yt-dlp + ffmpeg + Deno 2.9.7), `docker-compose.yml`
  with a shared library volume and durable `/data`, README and tests.

Post-MVP extras: per-channel SponsorBlock and ntfy notifications (Phase 3 — the
[Phase 3](#phase-3--extras-sponsorblock--ntfy) chapter), a WebUI panel with
channel policies and adding from a phone via an unlisted YouTube playlist
(`playlist:`, disabled by default).

## E2E results (SponsorBlock)

Test video: `sI_MzL3HIa0` (1282.79 s, `sponsor` segment 81.503-100.767 s):

| mode     | file duration        | sponsor window (mean_volume) |
| -------- | -------------------- | ---------------------------- |
| `off`    | 1282.79 s (unchanged)| -17.8 dB (audible)           |
| `remove` | 1263.52 s (-19.26 s) | removed                      |
| `mute`   | 1282.79 s (unchanged)| -91.0 dB (silence)           |

Full report: [`docs/e2e-sponsorblock-report.json`](docs/e2e-sponsorblock-report.json).

## License

The code is released under the **MIT** license — the full text is in the
[`LICENSE`](LICENSE) file. This means, among other things, unrestricted use,
modification and redistribution (including commercial) provided the copyright
notice and license text are retained; the software is provided without warranty.

External dependencies have their own licenses: `yt-dlp` (Unlicense), `FastAPI`
and `uvicorn` (MIT), `PyYAML` (MIT), and `ffmpeg` is invoked as a separate
program (LGPL/GPL depending on the build) — it is not linked with this code.
