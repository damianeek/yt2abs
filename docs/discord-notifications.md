# Discord notifications (yt2abs)

Target channel: **#yt2abs** (**Apps** category, private hub Discord server —
server, channel and webhook identifiers are deliberately omitted in this
document). Notifications are sent exclusively through a single webhook — no
other backends.

## Environment variable

| variable              | meaning                                          |
| --------------------- | ------------------------------------------------ |
| `DISCORD_WEBHOOK_URL` | full Discord webhook URL (secret)                |
| `DISCORD_USERNAME`   | (optional) overridden message sender name        |

Example `.env` (the file **lives only locally**, outside the repo):

```bash
# .env - DO NOT commit (in .gitignore); value from the api_services.json vault
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/<id>/<token>
# DISCORD_USERNAME=yt2abs
```

`.env.example` in the repo contains only the template (empty values) — it is
tracked, the real `.env` is not.

## Rules for keeping the secret

1. The real URL lives exclusively in the application `.env` (deploy) and in an
   external vault/password manager (escrow, like the other homelab webhooks).
2. Never in `config.yaml`, code, commits, logs or chat.
3. `.gitignore` guards: `.env`, `.env.*` (exception: `.env.example`).

## Sending (implementation)

Module: `yt2abs/notify.py` (stdlib `urllib`, zero new dependencies).
The worker calls it after a job state change:

| event      | when                                       | default |
| ---------- | ------------------------------------------ | ------- |
| `imported` | job imported into Audiobookshelf           | yes     |
| `failed`   | job permanently failed (after `max_attempts`) | yes  |
| `retry`    | single failed attempt, will be retried     | no      |

Event selection: `DISCORD_NOTIFY_EVENTS` (`imported,retry,failed`, `all`,
`none`). Emergency switch: `DISCORD_NOTIFY_DISABLED=1` (the general
`NOTIFY_DISABLED=1` is honored too). Timeout: `DISCORD_NOTIFY_TIMEOUT`
(default 10 s). Throttling: 1 request/s (Discord's limit is 5 requests / 5 s
per channel).

Format: `POST` JSON with `{"content": ...}` or `{"embeds": [{"title", "description", "url"}]}`
when the message has a title. `allowed_mentions.parse = []`, so video titles do
not trigger `@everyone` pings.

Error behavior:

- no `DISCORD_WEBHOOK_URL` → notifications disabled, a warning logged once, no
  exception and no impact on the queue;
- HTTP error / timeout / DNS → `NotifyResult(delivered=False, error=...)`, a
  warning logged with a **censored** URL (the token never lands in the log);
- HTTP 429 → one retry after `retry_after` from the Discord response;
- a notifier exception never stops the worker.

Manual test (requires `DISCORD_WEBHOOK_URL` in the environment):

```bash
source .env
.venv/bin/python -m yt2abs.cli notify-test
# OK: powiadomienie dostarczone (HTTP 204).
```

`GET /health` reports `notifications.enabled` and `notifications.events` (flags
only — the URL is never returned by the API).

## Configuration in the WebUI (`/settings`)

The panel (`GET /settings`, linked on the main page) lets you choose which
operations send a notification: `imported`, `retry`, `failed`. It requires no
restart — the `PUT /api/notifications` endpoint changes the event set in the
running notifier instance used by the worker — and it is durable: the selection
goes to the `settings` table in SQLite (`key = notify.events`), so it survives a
process restart.

| Method | Path | Behavior |
| --- | --- | --- |
| `GET` | `/api/notifications` | `{configured, enabled, disabled_by_env, source, available, events[]}`; no URL |
| `PUT` | `/api/notifications` | `{"events": ["imported","failed"]}` → `200` + new state, `400` for an unknown name, `422` for a wrong type |
| `POST` | `/api/notifications/test` | real test send: `200 {delivered:true,status:204}`, `409` no webhook, `502` delivery error |
| `GET` | `/settings` | HTML panel (checkboxes + “Save” + “Send test notification”) |

Hierarchy: **panel entry > `DISCORD_NOTIFY_EVENTS` > `imported,failed`**.
`source` in the response says where the current selection comes from (`webui`,
`env`, `default`). An empty event list disables job notifications, but the test
button still works. `DISCORD_NOTIFY_DISABLED`/`NOTIFY_DISABLED` remains the
emergency overriding switch (`disabled_by_env: true`, `enabled: false`).

The webhook URL never leaves the process: the panel shows only whether the
webhook is configured, and `GET /api/notifications` contains neither the URL nor
the token.

### WebUI configuration E2E

```bash
DISCORD_WEBHOOK_URL=... make e2e-notify-settings
```

`tests/e2e_notify_settings.py` starts the real application (FastAPI + worker +
SQLite) with a pipeline stub (no yt-dlp and no ABS), changes the selection via
`PUT /api/notifications` and checks that:

1. with `["failed"]` exactly one `failed` message arrives (no `retry`),
2. with `[]` there is **no** POST to the webhook at all,
3. with `["imported"]` only `imported` arrives,
4. the selection is in SQLite and after an application restart `source = webui`, with the events unchanged.

The notifier sends through a local proxy that appends `?wait=true` and stores
the message ID; the script reads each message independently from Discord
(`GET /api/webhooks/{id}/{token}/messages/{message_id}`). Run on 2026-10-20:
**18/18 PASS** (`failed` id `1561879402487226504`, `imported` id
`1561879417847021569`). Report: `.e2e/notify-settings-<UTC>/report.json`.

## Connectivity test

```bash
source .env
curl -sS -o /dev/null -w '%{http_code}\n' -X POST \
  -H 'Content-Type: application/json' \
  -d '{"content":"yt2abs: test polaczenia - wiadomosc testowa."}' \
  "$DISCORD_WEBHOOK_URL"
# expected: 204; the message is visible in #yt2abs
```

Test performed 2026-10-20 00:24 CEST (curl → 204, message confirmed by reading
the channel through the API, message id `1561867593843867800`).

## E2E test (real delivery + read from Discord)

`tests/e2e_discord.py` — outside `make test`, because it requires network,
`yt-dlp`/`ffmpeg` and a secret. It does not touch the production Audiobookshelf
(it uses a local ABS stub).

```bash
DISCORD_WEBHOOK_URL=... make e2e-discord
# or: DISCORD_WEBHOOK_URL=... .venv/bin/python tests/e2e_discord.py
```

What it does:

1. starts a local Audiobookshelf stub (scan / item list / episodes / PATCH),
2. sets up a proxy on `127.0.0.1` that forwards every POST to the real webhook
   with `?wait=true` and stores the message `id` returned by Discord,
3. starts the real application: FastAPI + worker + SQLite + yt-dlp,
4. sends `notify-test` from the CLI, then queues the video `dQw4w9WgXcQ`
   (expected `imported`, attempt 1) and a non-existent `AAAAAAAAAAA`
   (expected `retry` → `failed`),
5. reads each message **independently from Discord**:
   `GET /api/webhooks/{id}/{token}/messages/{message_id}` and compares
   `id`, `channel_id` and the embed title,
6. saves `report.json` in `.e2e/discord-<UTC>/` (config, evidence log, message IDs).

Result of the 2026-10-20 01:02 CEST run — **11/11 checks PASS**, channel
`#yt2abs`, all messages confirmed by reading (message IDs are non-secret test
evidence — they cannot be used without the webhook token):

| event | message id | timestamp (UTC) |
| --- | --- | --- |
| `notify-test` (CLI) | `1561877342165671940` | 2026-10-19T23:02:56 |
| `retry` (`AAAAAAAAAAA`, attempt 1/2) | `1561877349786714142` | 2026-10-19T23:02:58 |
| `imported` (`dQw4w9WgXcQ`, attempt 1) | `1561877380396613643` | 2026-10-19T23:03:05 |
| `failed` (`AAAAAAAAAAA`, after 2 attempts) | `1561877385828368546` | 2026-10-19T23:03:07 |

Note: on hosts with a datacenter address YouTube returns `HTTP 403` for the
default clients — the test sets `defaults.ytdlp_args:
["--extractor-args", "youtube:player_client=web_embedded"]` in the E2E config.
The end-to-end check lives in `tests/e2e_discord.py` (needs a real webhook).
