/* Shared pure helpers for the yt2abs Firefox extension.
 *
 * Loaded as a classic script in the extension (background, popup, options) and
 * as a CommonJS module by the Node test-suite, so it must not touch browser
 * APIs. Everything here is deterministic and unit-tested in tests/lib.test.js.
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.Yt2abs = api;
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const VIDEO_ID_RE = /^[A-Za-z0-9_-]{11}$/;
  const WATCH_HOSTS = new Set([
    "youtube.com",
    "youtube-nocookie.com",
    "youtu.be",
    "ytimg.com",
    "i.ytimg.com",
    "img.youtube.com",
  ]);

  /** Parse a URL, tolerating relative values (e.g. a thumbnail path). */
  function parseUrl(value, base) {
    if (!value) return null;
    try {
      return new URL(String(value), base || undefined);
    } catch (err) {
      return null;
    }
  }

  function normaliseHost(hostname) {
    return String(hostname || "")
      .toLowerCase()
      .replace(/^(www|m|music)\./, "");
  }

  /**
   * Extract a YouTube video id from any URL the context menu can report:
   * watch links, youtu.be, Shorts, embeds, live links and thumbnail images.
   * Returns null for everything else (playlists, channels, avatars).
   */
  function extractVideoId(value, base) {
    const url = parseUrl(value, base);
    if (!url) return null;
    if (url.protocol !== "http:" && url.protocol !== "https:") return null;
    const host = normaliseHost(url.hostname);
    if (!WATCH_HOSTS.has(host)) return null;

    const parts = url.pathname.split("/").filter(Boolean);
    if (host === "youtu.be") {
      const id = parts[0];
      return VIDEO_ID_RE.test(id || "") ? id : null;
    }
    if (host === "youtube.com" || host === "youtube-nocookie.com") {
      if (parts[0] === "watch") {
        const id = url.searchParams.get("v");
        return VIDEO_ID_RE.test(id || "") ? id : null;
      }
      if (["shorts", "embed", "live", "v", "e"].includes(parts[0])) {
        const id = parts[1];
        return VIDEO_ID_RE.test(id || "") ? id : null;
      }
      return null;
    }
    // Thumbnails: i.ytimg.com/vi/<id>/..., i.ytimg.com/vi_webp/<id>/...,
    // img.youtube.com/vi/<id>/0.jpg
    const match = url.pathname.match(/^\/vi(?:_webp)?\/([A-Za-z0-9_-]{11})(?:\/|$)/);
    return match ? match[1] : null;
  }

  /** "watch" | "shorts" | null - drives the toolbar button state. */
  function pageKind(value, base) {
    const url = parseUrl(value, base);
    if (!url) return null;
    const host = normaliseHost(url.hostname);
    if (host !== "youtube.com") return null;
    const parts = url.pathname.split("/").filter(Boolean);
    if (parts[0] === "watch" && extractVideoId(url.href)) return "watch";
    if (parts[0] === "shorts" && extractVideoId(url.href)) return "shorts";
    return null;
  }

  function isSupportedPage(value, base) {
    return pageKind(value, base) !== null;
  }

  function watchUrl(videoId) {
    return `https://www.youtube.com/watch?v=${videoId}`;
  }

  /**
   * Video id for a context-menu click. Thumbnails carry the id in srcUrl,
   * links in linkUrl; fall back to the page URL so the menu also works on a
   * video page itself.
   */
  function menuVideoId(info, pageUrl) {
    const candidates = [info && info.linkUrl, info && info.srcUrl, pageUrl];
    for (const candidate of candidates) {
      const id = extractVideoId(candidate, pageUrl);
      if (id) return id;
    }
    return null;
  }

  /** "Send" inherits the channel policy; "keep" pins retention_override=0. */
  function buildJobPayload(videoId, mode) {
    if (!VIDEO_ID_RE.test(String(videoId || ""))) {
      throw new Error("Invalid YouTube video id.");
    }
    if (mode !== undefined && mode !== "policy" && mode !== "keep") {
      throw new Error(`Unknown send mode: ${mode}`);
    }
    return {
      url: watchUrl(videoId),
      retention_override: mode === "keep" ? 0 : null,
    };
  }

  const MODE_POLICY = "policy";
  const MODE_KEEP = "keep";

  /** "14 days" / "1 day" */
  function formatDays(days) {
    const value = Number(days);
    if (!Number.isFinite(value)) return "";
    if (value === 1) return "1 day";
    return `${value} days`;
  }

  /**
   * Human readable retention policy for the popup.
   * `policy` is /api/policy: {retention_days, source, channel_name}.
   * `override` is the job's retention_override (null = inherit).
   */
  function formatPolicy(policy, override) {
    if (override !== null && override !== undefined) {
      const days = Number(override);
      if (days === 0) return "kept forever (overridden)";
      return `deleted after ${formatDays(days)} (overridden)`;
    }
    if (!policy) return "unknown policy";
    const source = policy.source === "channel" ? "channel policy" : "default policy";
    const days = policy.retention_days;
    if (days === null || days === undefined) return `${source}: kept forever`;
    if (Number(days) === 0) return `${source}: deleted on the next retention run`;
    return `${source}: deleted after ${formatDays(days)}`;
  }

  function formatStatus(status) {
    switch (status) {
      case "queued":
        return "Queued";
      case "downloading":
        return "Downloading";
      case "imported":
        return "In ABS";
      case "failed":
        return "Failed";
      case "deleted":
        return "Deleted";
      default:
        return "Not sent";
    }
  }

  function isPending(status) {
    return status === "queued" || status === "downloading";
  }

  /** Badge text: a check mark only when the episode is already in ABS. */
  function badgeText(status) {
    return status === "imported" ? "✓" : "";
  }

  function statusClass(status) {
    if (status === "imported") return "ok";
    if (status === "failed") return "err";
    if (isPending(status)) return "pending";
    if (status === "deleted") return "muted";
    return "muted";
  }

  /** Short, user-facing message for an HTTP failure from the yt2abs API. */
  function describeHttpError(status, payload) {
    const detail = payload && typeof payload === "object" ? payload.detail : null;
    switch (status) {
      case 400:
        return typeof detail === "string" && detail ? detail : "The server rejected this YouTube link.";
      case 401:
      case 403:
        return "The server refused the request — check that the address points at yt2abs.";
      case 404:
        return "The server does not know this video.";
      case 409:
        return typeof detail === "string" && detail ? detail : "The server cannot accept a job right now.";
      case 429:
        return "Too many requests — try again in a moment.";
      default:
        if (status >= 500) return "yt2abs server error — check its logs.";
        return typeof detail === "string" && detail ? detail : `HTTP error ${status}.`;
    }
  }

  function parseRetryAfter(value) {
    if (value === null || value === undefined || value === "") return 0;
    const seconds = Number(value);
    if (Number.isFinite(seconds) && seconds >= 0) return Math.ceil(seconds);
    const when = Date.parse(String(value));
    if (Number.isNaN(when)) return 0;
    return Math.max(0, Math.ceil((when - Date.now()) / 1000));
  }

  /**
   * Normalise the configured server address: trim spaces, add a scheme when the
   * user typed a bare host, drop the trailing slash. Keeps a sub-path so a
   * reverse proxy like https://host/yt2abs works.
   */
  function normalizeServerUrl(value) {
    let text = String(value || "").trim();
    if (!text) return "";
    if (!/^[a-z][a-z0-9+.-]*:\/\//i.test(text)) {
      const host = text.split("/")[0];
      const isLocal =
        /^(localhost|127\.|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|\[::1\])/.test(host) ||
        !host.includes(".");
      text = `${isLocal ? "http" : "https"}://${text}`;
    }
    const url = parseUrl(text);
    if (!url || (url.protocol !== "http:" && url.protocol !== "https:")) return "";
    return `${url.origin}${url.pathname}`.replace(/\/+$/, "");
  }

  function serverProblem(value) {
    if (!String(value || "").trim()) return "Enter the yt2abs server address.";
    if (!normalizeServerUrl(value)) return "The address must start with http:// or https://.";
    return "";
  }

  function validateSettings(settings) {
    const problem = serverProblem(settings && settings.serverUrl);
    if (problem) return problem;
    return "";
  }

  function policyQuery({ channelId, channelName }) {
    const params = new URLSearchParams();
    if (channelId) params.set("channel_id", String(channelId).trim());
    if (channelName) params.set("name", String(channelName).trim());
    const query = params.toString();
    return query ? `?${query}` : "";
  }

  function shorten(text, limit) {
    const value = String(text || "").trim();
    const max = limit || 80;
    return value.length > max ? `${value.slice(0, max - 1)}…` : value;
  }

  return {
    VIDEO_ID_RE,
    extractVideoId,
    pageKind,
    isSupportedPage,
    watchUrl,
    menuVideoId,
    buildJobPayload,
    MODE_POLICY,
    MODE_KEEP,
    formatDays,
    formatPolicy,
    formatStatus,
    isPending,
    badgeText,
    statusClass,
    describeHttpError,
    parseRetryAfter,
    normalizeServerUrl,
    serverProblem,
    validateSettings,
    policyQuery,
    shorten,
  };
});
