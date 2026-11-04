# Security policy

## Supported versions

The `main` branch and the newest `v*` tag receive fixes. Older images are not
patched; rebuild from a current tag.

## Reporting a vulnerability

Use GitHub's **private vulnerability reporting** (Security → Report a
vulnerability) on this repository. If that is unavailable, open an issue that
contains no exploit details and ask for a private channel.

Please include: affected version or image digest, the configuration used
(`config.yaml` with secrets removed), reproduction steps and the impact you
expect.

## Scope

**By design, yt2abs has no authentication.** It is a LAN-only service: the
reachable port is the access control (`README.md` → "Network exposure, rate
limit and reverse proxy"). The following are therefore *not* vulnerabilities:

- anyone who can reach the port can queue downloads, change channel policies or
  delete episodes through retention;
- `GET /add` can be triggered by a cross-site navigation (that is what the
  bookmarklet does);
- the WebUI shows the queue and policies without a login.

What *is* in scope:

- bypassing the `Host` allow-list or the cross-site write check;
- a path traversal, command injection or a way to write outside the configured
  library/temp/data directories;
- SSRF or a way to make the service download something other than a YouTube
  video;
- leaking the Audiobookshelf API key or notification tokens into logs, the API
  or a downloaded file;
- a rate-limit bypass that lets one client exhaust memory or CPU.

## Hardening checklist for operators

1. Bind the listener to a LAN address (`BIND_ADDR`) — never `0.0.0.0` on a host
   with a public interface.
2. Keep the port behind a firewall or a reverse proxy with a LAN ACL.
3. Set `api.allowed_hosts` to the names you actually use; IP literals,
   `localhost` and single-label names are always accepted.
4. Use a dedicated Audiobookshelf API key for yt2abs.
5. Keep the image and `yt-dlp` current; both are updated by the CI image build
   and by the in-process updater.
