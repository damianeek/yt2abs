# Branches, Docker images and releases

The repository has two long-lived branches:

| Branch | Role | Image |
| --- | --- | --- |
| `dev` | current work, integration, live tests | `ghcr.io/damianeek/yt2abs-public:dev` |
| `main` | stable version, only verified changes | `ghcr.io/damianeek/yt2abs-public:latest` |

The current version of the code lives on `dev`. `main` is fast-forwarded from
`dev` only after the tests pass; we do not develop directly on `main`.

## Workflow

```bash
git switch dev
git pull --ff-only
# ... changes, tests ...
git add -A
git commit -m "..."
git push origin dev
```

Stable release:

```bash
git switch dev
git pull --ff-only
git switch main
git merge --ff-only dev
git push origin main

git tag -a v0.2.0 -m "yt2abs 0.2.0"
git push origin v0.2.0
```

After a merge to `main` and after a `v*` tag, the image is built automatically.
We do not push tags or working branches to `main`; the only long-lived
branches in `origin` are `main` and `dev`.

## Pipeline

Workflows:

- [`.github/workflows/tests.yml`](../.github/workflows/tests.yml) — unit tests
  (Python suite + Firefox extension) on every push and pull request;
- [`.github/workflows/docker.yml`](../.github/workflows/docker.yml) — image build
  and a smoke run of the published image. It runs on:

- a push to `dev` or `main`,
- a push of a `v*` tag,
- a manual `workflow_dispatch`.

It builds a multi-architecture image (`linux/amd64` for Unraid/x86_64 and
`linux/arm64` for ARM NAS/boards) and pushes it to GitHub Container Registry
(`ghcr.io`) with the BuildKit cache, an SBOM and a provenance attestation. It
requires no additional secrets — it uses `GITHUB_TOKEN` with the
`packages: write` permission.

| Tag | When it is created | Purpose |
| --- | --- | --- |
| `dev` | every push to `dev` | live tests, latest code |
| `sha-<7>` | every build | immutable rollback to a specific commit |
| `latest`, `main` | push/merge to `main` | stable version |
| `0.2.0`, `0.2` | `v0.2.0` tag | versioned release |

`latest` is assigned exclusively to `main` (hard-coded in the workflow), so
changing the default branch in GitHub does not move this tag.

## GitHub configuration (one-time)

1. **Actions**: make sure Actions are enabled, then
   Settings -> Actions -> General -> Workflow permissions ->
   *Read and write permissions* (otherwise `GITHUB_TOKEN` will not push the package).
2. **Package visibility**: after the first successful build, open your GitHub
   profile -> Packages -> `yt2abs` -> Package settings -> Change visibility. If
   the package is to be public (convenient pulling on Unraid without logging in),
   set it to *Public*. The image contains no secrets or `config.yaml`.
3. **Default branch** (optional): Settings -> Branches -> Default branch ->
   `dev`, if pull requests should target `dev` by default. The workflow still
   builds `latest` only from `main`.
4. **Branch protection** (optional): for `main`, block force-push and require a
   PR; for `dev`, blocking force-push is enough.

If the package stays private, the host pulling the image must log in:

```bash
# classic PAT with the read:packages scope (do not paste the token into the repo)
echo "<PAT>" | docker login ghcr.io -u damianeek --password-stdin
```

On Unraid `/root` is in RAM, so the login must be repeated after a restart or
added to `/boot/config/go` (keep the token outside the repo, e.g. in
`/boot/config/secrets/ghcr_token` with 600 permissions).

## Live test with compose

Compose builds locally by default, but you can point it at an image from CI:

```bash
export YT2ABS_IMAGE=ghcr.io/damianeek/yt2abs-public:dev
docker compose pull
docker compose up -d
curl -fsS http://127.0.0.1:8099/health
```

Rollback: set `YT2ABS_IMAGE=ghcr.io/damianeek/yt2abs-public:sha-<7>` and run
`docker compose up -d`. The same `sha-<7>` number is printed in the workflow log
and visible in the package description.

## Unraid

Ready-made template: [`unraid/yt2abs.xml`](../unraid/yt2abs.xml), instructions:
[`unraid/README.md`](../unraid/README.md). The template points to `:dev`; for
the stable version, change Repository to `ghcr.io/damianeek/yt2abs-public:latest`.
