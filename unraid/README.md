# yt2abs on Unraid

Template: [`yt2abs.xml`](yt2abs.xml). By default it installs the image
`ghcr.io/damianeek/yt2abs:dev` (current code from the `dev` branch). For the
stable version, change the **Repository** field in Unraid to
`ghcr.io/damianeek/yt2abs:latest` (or `:sha-<7>` for a rollback).

## Requirements

- Unraid 6.12+ with a working Docker.
- Audiobookshelf with a podcast library.
- The same host library directory mounted in ABS and in yt2abs.
- Access to `ghcr.io` (a public package or `docker login`, see below).

## Installing the template

Copy the file from the repository to the Unraid flash drive:

```bash
mkdir -p /boot/config/plugins/dockerMan/templates-user
cp unraid/yt2abs.xml /boot/config/plugins/dockerMan/templates-user/my-yt2abs.xml
```

Then: **Docker -> Add Container -> Template -> yt2abs**. The template also
appears after refreshing the Docker page.

If the repository is private and the GHCR package is too, log in on the host:

```bash
echo "<PAT with read:packages>" | docker login ghcr.io -u damianeek --password-stdin
```

`/root` on Unraid is in RAM — after a restart the login must be repeated or
added to `/boot/config/go`, keeping the token in a file with 600 permissions.
Simpler: after the first build, set the `yt2abs` package to **Public** in the
GitHub Packages settings (the image contains no secrets).

## Before the first start

1. Create the directories and the configuration file:

   ```bash
   mkdir -p /mnt/user/appdata/yt2abs /mnt/user/media/yt2abs-tmp
   cp config.example.yaml /mnt/user/appdata/yt2abs/config.yaml
   chown -R 99:100 /mnt/user/appdata/yt2abs /mnt/user/media/yt2abs-tmp
   chmod 600 /mnt/user/appdata/yt2abs/config.yaml
   ```

   Take `config.example.yaml` from a repository checkout on the `dev` branch.

2. In `/mnt/user/appdata/yt2abs/config.yaml` set at least:

   - `abs.url` — the ABS address as seen from the container,
   - `abs.library_id` — the podcast library ID,
   - `abs.library_path: /youtube-podcasts`,
   - `paths.tmp_dir: /tmp-downloads`,
   - `paths.data_dir: /config/data`,
   - the `defaults`/`channels` policies.

3. In the template fill in:

   - `ABS_API_KEY` — the Audiobookshelf API key,
   - `ABS_LIBRARY_ID` — the same ID as in the config,
   - `YT2ABS_PUBLIC_URL` — the LAN HTTPS address of the reverse proxy, e.g.
     `https://yt2abs.example.com` (optional; used for bookmarklet links).

   There is no API token: yt2abs has no authentication and must stay inside the
   LAN.

   The **Library** mount must point to exactly the directory that ABS mounts as
   `/youtube-podcasts`. The **Temp downloads** mount is best kept on the same
   filesystem as the library.

4. The container runs as `99:100` (`--user=99:100` in ExtraParams). The
   `/config`, `/tmp-downloads` and library directories must be writable by that
   pair. If you use different IDs on the host, fix ExtraParams in the template.

## Network and reverse proxy

Unraid publishes port 8099 on all host interfaces. The service has no
authentication, so the LAN boundary is the only protection - never forward this
port from the WAN. A safe setup:

- keep the port reachable only from your LAN (firewall / reverse proxy ACL),
- put a reverse proxy (Zoraxy/NPM) with TLS in front and forward to `host_IP:8099`,
- set `YT2ABS_PUBLIC_URL` to the HTTPS address,
- enable `YT2ABS_TRUST_PROXY=1` and enter the **exact** proxy address in
  `YT2ABS_TRUSTED_PROXIES` (e.g. `10.1.2.1/32`), otherwise the
  `X-Forwarded-For` headers are ignored.

Health check:

```bash
curl -fsS http://127.0.0.1:8099/health
docker logs --tail 100 yt2abs
```

## Updates and rollback

- **Dev**: every push to `dev` publishes a new `:dev` tag; in Unraid use
  *Check for Updates* / *Force Update*.
- **Stable**: change Repository to `:latest`.
- **Rollback**: set Repository to `ghcr.io/damianeek/yt2abs:sha-<7>`
  (the commit number from the workflow log) and run *Force Update*.

Data in `/mnt/user/appdata/yt2abs` (SQLite, `data/bin`) and the library stay
untouched when the image is replaced.
