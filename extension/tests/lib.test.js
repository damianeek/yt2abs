/* Unit tests for the extension helpers (node --test, no dependencies). */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");

const lib = require("../src/lib.js");
const api = require("../src/api.js");

const VIDEO = "dQw4w9WgXcQ";

test("extractVideoId understands every YouTube link shape", () => {
  const cases = [
    `https://www.youtube.com/watch?v=${VIDEO}`,
    `https://www.youtube.com/watch?v=${VIDEO}&list=PL123&index=2&t=42s`,
    `https://m.youtube.com/watch?v=${VIDEO}`,
    `http://youtube.com/watch?app=desktop&v=${VIDEO}`,
    `https://youtu.be/${VIDEO}`,
    `https://youtu.be/${VIDEO}?si=abc`,
    `https://www.youtube.com/shorts/${VIDEO}`,
    `https://www.youtube.com/embed/${VIDEO}`,
    `https://www.youtube.com/live/${VIDEO}`,
    `https://www.youtube-nocookie.com/embed/${VIDEO}`,
    `https://i.ytimg.com/vi/${VIDEO}/hqdefault.jpg`,
    `https://i.ytimg.com/vi_webp/${VIDEO}/maxresdefault.webp`,
    `https://img.youtube.com/vi/${VIDEO}/0.jpg`,
  ];
  for (const url of cases) {
    assert.equal(lib.extractVideoId(url), VIDEO, url);
  }
});

test("extractVideoId ignores non-video URLs", () => {
  const cases = [
    "",
    null,
    "not a url",
    "https://www.youtube.com/",
    "https://www.youtube.com/feed/subscriptions",
    "https://www.youtube.com/playlist?list=PL123",
    "https://www.youtube.com/@SomeChannel",
    "https://www.youtube.com/channel/UC12345678901234567890",
    "https://yt3.ggpht.com/avatar.jpg",
    "https://example.com/watch?v=dQw4w9WgXcQ",
    "https://www.youtube.com/watch?v=short",
  ];
  for (const url of cases) {
    assert.equal(lib.extractVideoId(url), null, String(url));
  }
});

test("extractVideoId resolves relative context-menu links", () => {
  assert.equal(lib.extractVideoId(`/watch?v=${VIDEO}`, "https://www.youtube.com/results?q=x"), VIDEO);
  assert.equal(lib.extractVideoId(`/shorts/${VIDEO}`, "https://www.youtube.com/"), VIDEO);
});

test("pageKind enables the toolbar only on watch and shorts", () => {
  assert.equal(lib.pageKind(`https://www.youtube.com/watch?v=${VIDEO}`), "watch");
  assert.equal(lib.pageKind(`https://www.youtube.com/shorts/${VIDEO}`), "shorts");
  assert.equal(lib.pageKind(`https://www.youtube.com/embed/${VIDEO}`), null);
  assert.equal(lib.pageKind("https://www.youtube.com/playlist?list=PL1"), null);
  assert.equal(lib.isSupportedPage("https://example.com"), false);
});

test("menuVideoId prefers the clicked link, then the thumbnail, then the page", () => {
  assert.equal(lib.menuVideoId({ linkUrl: `https://youtu.be/${VIDEO}` }, "https://www.youtube.com/"), VIDEO);
  assert.equal(
    lib.menuVideoId({ srcUrl: `https://i.ytimg.com/vi/${VIDEO}/hqdefault.jpg` }, "https://www.youtube.com/results"),
    VIDEO
  );
  assert.equal(lib.menuVideoId({}, `https://www.youtube.com/watch?v=${VIDEO}`), VIDEO);
  assert.equal(lib.menuVideoId({ linkUrl: "https://www.youtube.com/@channel" }, "https://www.youtube.com/"), null);
});

test("buildJobPayload maps keep to retention_override=0 and policy to null", () => {
  assert.deepEqual(lib.buildJobPayload(VIDEO, "policy"), {
    url: `https://www.youtube.com/watch?v=${VIDEO}`,
    retention_override: null,
  });
  assert.deepEqual(lib.buildJobPayload(VIDEO, "keep"), {
    url: `https://www.youtube.com/watch?v=${VIDEO}`,
    retention_override: 0,
  });
  assert.deepEqual(lib.buildJobPayload(VIDEO), {
    url: `https://www.youtube.com/watch?v=${VIDEO}`,
    retention_override: null,
  });
  assert.throws(() => lib.buildJobPayload("nope", "policy"), /video id/);
  assert.throws(() => lib.buildJobPayload(VIDEO, "weird"), /mode/);
});

test("formatPolicy explains channel policies and overrides", () => {
  assert.equal(
    lib.formatPolicy({ retention_days: 7, source: "channel" }, null),
    "channel policy: deleted after 7 days"
  );
  assert.equal(
    lib.formatPolicy({ retention_days: null, source: "channel" }, null),
    "channel policy: kept forever"
  );
  assert.equal(
    lib.formatPolicy({ retention_days: 0, source: "defaults" }, null),
    "default policy: deleted on the next retention run"
  );
  assert.equal(
    lib.formatPolicy({ retention_days: 30, source: "defaults" }, 0),
    "kept forever (overridden)"
  );
  assert.equal(
    lib.formatPolicy({ retention_days: 30, source: "defaults" }, 3),
    "deleted after 3 days (overridden)"
  );
  assert.equal(lib.formatPolicy({ retention_days: 1, source: "channel" }, null), "channel policy: deleted after 1 day");
  assert.equal(lib.formatPolicy(null, null), "unknown policy");
});

test("status labels and badge text", () => {
  assert.equal(lib.formatStatus("queued"), "Queued");
  assert.equal(lib.formatStatus("imported"), "In ABS");
  assert.equal(lib.formatStatus(null), "Not sent");
  assert.equal(lib.badgeText("imported"), "✓");
  assert.equal(lib.badgeText("queued"), "");
  assert.equal(lib.badgeText(null), "");
  assert.equal(lib.isPending("downloading"), true);
  assert.equal(lib.isPending("failed"), false);
});

test("normalizeServerUrl accepts LAN addresses, reverse-proxy subpaths and https", () => {
  assert.equal(lib.normalizeServerUrl("192.168.1.10:13381"), "http://192.168.1.10:13381");
  assert.equal(lib.normalizeServerUrl("  http://192.168.1.10:13381/  "), "http://192.168.1.10:13381");
  assert.equal(lib.normalizeServerUrl("abs.example.com/yt2abs/"), "https://abs.example.com/yt2abs");
  assert.equal(lib.normalizeServerUrl("localhost:8080"), "http://localhost:8080");
  assert.equal(lib.normalizeServerUrl("https://abs.example.com"), "https://abs.example.com");
  assert.equal(lib.normalizeServerUrl("ftp://abs.example.com"), "");
  assert.equal(lib.normalizeServerUrl(""), "");
});

test("validateSettings only needs a server address", () => {
  assert.match(lib.validateSettings({ serverUrl: "" }), /server address/);
  assert.equal(lib.validateSettings({ serverUrl: "http://x:1" }), "");
});

test("describeHttpError maps API failures to English messages", () => {
  assert.match(lib.describeHttpError(401, { detail: "x" }), /refused the request/);
  assert.match(lib.describeHttpError(400, { detail: "Not a YouTube link." }), /Not a YouTube link\./);
  assert.match(lib.describeHttpError(429, null), /Too many requests/);
  assert.match(lib.describeHttpError(500, null), /server error/);
});

test("parseRetryAfter handles seconds and HTTP dates", () => {
  assert.equal(lib.parseRetryAfter("12"), 12);
  assert.equal(lib.parseRetryAfter(null), 0);
  assert.equal(lib.parseRetryAfter(""), 0);
  const soon = new Date(Date.now() + 5000).toUTCString();
  const parsed = lib.parseRetryAfter(soon);
  assert.ok(parsed >= 3 && parsed <= 6, `parsed=${parsed}`);
});

function fakeFetch(handler) {
  const calls = [];
  const impl = async (url, options) => {
    calls.push({ url, options });
    return handler(url, options, calls.length);
  };
  impl.calls = calls;
  return impl;
}

function jsonResponse(status, payload, headers) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: (name) => (headers && headers[name.toLowerCase()]) || null },
    text: async () => (payload === undefined ? "" : JSON.stringify(payload)),
  };
}

test("request sends no credentials and joins the base path", async () => {
  const settings = { serverUrl: "http://192.168.1.10:13381" };
  const fetchImpl = fakeFetch(() => jsonResponse(200, { version: "0.1.0" }));
  const payload = await api.request(settings, "/api/system", { fetchImpl });
  assert.deepEqual(payload, { version: "0.1.0" });
  assert.equal(fetchImpl.calls[0].url, "http://192.168.1.10:13381/api/system");
  assert.equal(fetchImpl.calls[0].options.headers.Authorization, undefined);
  assert.equal(fetchImpl.calls[0].options.method, "GET");
});

test("request posts JSON and keeps retention_override=0 for keep", async () => {
  const settings = { serverUrl: "http://abs.local/yt2abs/" };
  const fetchImpl = fakeFetch(() => jsonResponse(202, { video_id: VIDEO, status: "queued" }));
  const job = await api.postJob(settings, VIDEO, "keep", { fetchImpl });
  assert.equal(job.status, "queued");
  const call = fetchImpl.calls[0];
  assert.equal(call.url, "http://abs.local/yt2abs/api/jobs");
  assert.equal(call.options.headers["Content-Type"], "application/json");
  const body = JSON.parse(call.options.body);
  assert.deepEqual(body, { url: `https://www.youtube.com/watch?v=${VIDEO}`, retention_override: 0 });
});

test("request reports auth, rate-limit and network failures", async () => {
  const settings = { serverUrl: "http://abs.local" };
  await assert.rejects(
    () => api.request(settings, "/api/system", { fetchImpl: fakeFetch(() => jsonResponse(401, { detail: "x" })) }),
    (err) => err instanceof api.ApiError && err.status === 401 && /refused the request/.test(err.message)
  );
  await assert.rejects(
    () =>
      api.request(settings, "/api/jobs", {
        fetchImpl: fakeFetch(() => jsonResponse(429, null, { "retry-after": "7" })),
      }),
    (err) => err.status === 429 && err.retryAfter === 7 && /Too many/.test(err.message)
  );
  await assert.rejects(
    () =>
      api.request(settings, "/api/system", {
        fetchImpl: async () => {
          throw new Error("boom");
        },
      }),
    (err) => err.code === "network" && /No connection/.test(err.message)
  );
});

test("getJob turns 404 into null and getPolicy builds the query", async () => {
  const settings = { serverUrl: "http://abs.local" };
  const notFound = await api.getJob(settings, VIDEO, {
    fetchImpl: fakeFetch(() => jsonResponse(404, { detail: "Brak" })),
  });
  assert.equal(notFound, null);

  const fetchImpl = fakeFetch(() => jsonResponse(200, { retention_days: 7, source: "channel" }));
  const policy = await api.getPolicy(
    settings,
    { channelId: "UC123", channelName: "Channel & co" },
    { fetchImpl }
  );
  assert.equal(policy.retention_days, 7);
  assert.match(fetchImpl.calls[0].url, /\/api\/policy\?channel_id=UC123&name=Channel\+%26\+co$/);
});

test("request refuses to run without a server address", async () => {
  await assert.rejects(
    () => api.request({ serverUrl: "" }, "/api/system"),
    (err) => err.code === "no_server"
  );
});
