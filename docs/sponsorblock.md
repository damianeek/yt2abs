# SponsorBlock per channel - implementation details

This document describes the layer added in the "SponsorBlock per channel in
yt-dlp" layer. Code: `yt2abs/sponsorblock.py`,
`yt2abs/config.py`, `yt2abs/downloader.py`. Tests: `tests/test_sponsorblock.py`,
`tests/test_config.py`, `tests/test_downloader.py` and the E2E
`tests/e2e_sponsorblock.py`.

## 1. Configuration model

`config.yaml` (template: `config.example.yaml`) keeps policies in
`channels.<channel_id>` and default values in `defaults`. The key is the YouTube
`channel_id` (`UC...`), as agreed in the project.

```yaml
defaults:
  retention_days: 14
  protect_unfinished: true
  sponsorblock: off
channels:
  UCzz4CoEgSgWNs9ZAvRMhW2A:
    name: Everything Electric CARS
    retention_days: 7
    sponsorblock: remove:sponsor
```

Value resolution: **channel > defaults > built-in `off`**. A channel missing
from the file gets `defaults` (i.e. with an empty file - `off`, exactly as
before the change). `load_config()` accepts a path or looks for `config.yaml`,
then `config.example.yaml`. When `PyYAML` is not installed, the file can be a
JSON document (JSON is a subset of YAML) - so everything works on the bare
stdlib.

## 2. Grammar of `sponsorblock` values

It is normalized by `yt2abs.sponsorblock.parse_setting()` into a
`(mode, categories)` pair (frozen dataclass `Setting`):

| input                                | result                                   |
| ------------------------------------ | ---------------------------------------- |
| `off` / `false` / `null` / missing   | `off`, no actions at all                 |
| `true`                               | `remove`, categories `("sponsor",)`      |
| `remove` / `mark` / `mute`           | mode with the default `sponsor` category |
| `remove:sponsor,selfpromo`           | mode + explicit list                     |
| `all`, `default`, `-category`        | shortcuts expanded to a concrete list    |
| `{mode: ..., categories: [...]}`     | dictionary form                          |
| `[sponsor, selfpromo]`               | `remove` with those categories           |

Validation is strict: an unknown category, an unknown mode, an empty list after
expanding shortcuts or an unknown key in the dictionary form -> a
`SponsorBlockConfigError` exception, wrapped in `ConfigError` with the
channel number/identifier in the message (no silent ignoring of typos).

The categories correspond to `yt_dlp.postprocessor.sponsorblock.SponsorBlockPP.CATEGORIES`.
`poi_highlight` and `chapter` are not "skippable", so they are rejected for
`remove`/`mute` and allowed for `mark` (consistent with what yt-dlp accepts).

## 3. Mapping to yt-dlp

`ytdlp_args(setting, api=...)`:

| mode     | yt-dlp arguments                                   |
| -------- | -------------------------------------------------- |
| `off`    | `[]` (exactly today's command)                     |
| `remove` | `--sponsorblock-remove <sponsor,...>`              |
| `mark`   | `--sponsorblock-mark <sponsor,...>`                |
| `mute`   | `[]` - handled locally (yt-dlp has no mute)        |

If `defaults.sponsorblock_api` differs from `https://sponsor.ajay.app`,
`--sponsorblock-api <url>` is added.

The category range is always passed explicitly (the `all`/`default` shortcuts
and `-cat` exclusions are expanded on our side), so the behavior is identical
for yt-dlp and for local muting.

## 4. `mute` mode

yt-dlp can only cut segments (`remove`) or add chapters (`mark`); it has no
muting. That is why we do `mute` ourselves:

1. `fetch_segments(video_id, categories)` - `GET /api/skipSegments/<hash4>`
   with `service=YouTube`, `categories=[...]`, `actionTypes=["skip"]`
   (`hash4` = the first 4 characters of the SHA-256 of `video_id`, as in yt-dlp).
   Both response shapes of the API are supported: grouped by `videoID`
   (`{"videoID": ..., "segments": [...]}`, hash endpoint) and flat
   (`?videoID=` endpoint).
2. Filters: `actionType == skip`, category in the list, `end > start`, and when
   `video_duration` is provided (from `ffprobe`), segments with a different
   video length are rejected (`|videoDuration - duration| > 1 s`), so we do not
   cut a modified version of the video.
3. `build_mute_filter()` merges overlapping segments and builds a single ffmpeg
   filter: `volume=enable='between(t,start,end)+...':volume=0`.
4. `mute_segments()` rewrites the file through ffmpeg (`-c:a aac -q:a 2`) to a
   temporary file and replaces it atomically (`os.replace`). The temporary file
   never stays on disk; when there are no segments, the file is not touched at
   all.

The cost of `mute`: one AAC transcode (about 130 kb/s, ~1-2 s per 20 min of
audio). If that is a problem for a particular channel, the alternative is
`remove` (no transcode, but it shortens the file) or `mark` (keeps the audio,
chapters only).

## 5. Download flow

`download_for_channel(url, dest, config, ...)`:

1. no `channel_id` -> `probe_metadata()` (one `--simulate --print
   channel_id|id`, without downloading media),
2. `config.channel(channel_id)` -> the channel policy,
3. `download(...)` -> `yt-dlp --format bestaudio[ext=m4a] --output
   "<dest>/%(upload_date>%Y-%m-%d)s - %(title).200B [%(id)s].%(ext)s"
   [--sponsorblock-*] [--extractor-args ...] <url>`,
   the final path taken from `--print after_move:filepath`,
4. when the mode is `mute` -> the optional muting step from point 4.

`defaults.ytdlp_args` lets you add arbitrary arguments to every download
(e.g. `--extractor-args youtube:player_client=web_embedded` on hosts where the
IP is blocked by YouTube - for us the default clients get `HTTP 403`, while
`web_embedded` works; we do not change this by default so as not to change the
behavior on the target server).

## 6. Verification

Unit tests: 68 (`python3 -m unittest discover -s tests -t .`), covering parsing
and validation, mapping to yt-dlp arguments, the mute filter, the
`yt-dlp -> ffmpeg` order, the fallback to `defaults` for an unknown channel and
download errors. Also run without `PyYAML` (JSON path) and with `PyYAML`.

E2E (`YT2ABS_E2E=1 python3 tests/e2e_sponsorblock.py`, video `sI_MzL3HIa0`, one
`sponsor` segment 81.503-100.767 s):

| mode     | duration   | sponsor window (mean_volume) | notes                                |
| -------- | ---------- | ---------------------------- | ------------------------------------ |
| `off`    | 1282.79 s  | -17.8 dB                     | no sponsorblock flags in the command |
| `remove` | 1263.52 s  | segment removed              | difference == 19.26 s (1.5 s tolerance) |
| `mute`   | 1282.79 s  | -91.0 dB                     | duration unchanged, silence in the segment |

The test machine has no access to the home IP, so the E2E used
`--extractor-args youtube:player_client=web_embedded` via
`YT2ABS_E2E_YTDLP_ARGS`. On the target server (home IP) it should work without
this workaround; if not - put those arguments into `defaults.ytdlp_args`.

Additionally, the download regression was checked: the file from `fetch` with
`sponsorblock: off` is **bit for bit** the same as from a manual
`yt-dlp -f bestaudio[ext=m4a]` (same sha256:
`f0b3a18aebddaaa0793bb5e0449cc9af8e0245bfdd09bee32b3c709d817c192b`,
20 746 626 B) - the absence of the setting adds no arguments or
postprocessing.

JSON report from the last run: `docs/e2e-sponsorblock-report.json`.
