# Contributing

Thanks for looking at yt2abs. A few ground rules keep the project predictable.

## Before you start

- Open an issue for anything larger than a bug fix, so the approach is agreed
  before code is written.
- Security problems do **not** go into issues — see `SECURITY.md`.

## Local setup

```bash
uv venv .venv && uv pip install -e '.[dev]'   # or: python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
cp config.example.yaml config.yaml            # the app refuses to start without it
make test                                     # unit tests, no network
make e2e-security                             # real uvicorn: rate limit, proxy trust, host guard
```

`yt-dlp` and `ffmpeg` must be in `PATH` for the end-to-end scripts.

## Conventions

- **Language:** code, comments, identifiers, documentation and commit messages
  are English. User-facing strings are English too.
- **Commits:** one logical change per commit, imperative subject
  (`fix(retention): ...`), body explaining *why* when it is not obvious.
- **Tests:** every behaviour change needs a test that fails before it. Run
  `make test` (and the extension suite: `cd extension && node --test tests/*.test.js`)
  before opening a pull request.
- **Style:** standard library first; no new runtime dependency without a reason
  in the issue. Type hints on public functions.
- **Security posture:** the service stays LAN-only and unauthenticated. Changes
  that weaken the `Host` allow-list, the cross-site write check, the URL
  allow-list or the path handling will be rejected.

## Pull requests

1. Branch from `dev`, keep the diff focused.
2. Describe the problem, the solution and how you verified it (commands and
   results, not "should work").
3. CI must be green: the `tests` workflow (Python + extension) and the `docker`
   workflow (build + smoke run of the image).
4. Documentation under `docs/` follows the same language rules as code.

## Releases

Releases are cut from `main` with a `v*` tag; the workflow publishes a
multi-arch image to GHCR. See `docs/branches-and-releases.md`.
