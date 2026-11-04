/* Integration tests for background.js with a mocked WebExtension API.
 *
 * The browser UI (native context menu) cannot be driven by WebDriver in
 * headless Firefox, so these tests exercise the real background code paths:
 * menus.onClicked -> sendVideo -> POST /api/jobs, commands.onCommand,
 * tabs.onUpdated gating, badge updates and popup messages.
 */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const lib = require("../src/lib.js");
const api = require("../src/api.js");

const VIDEO = "dQw4w9WgXcQ";
const LIB_SOURCE = fs.readFileSync(path.join(__dirname, "../src/lib.js"), "utf8");
const API_SOURCE = fs.readFileSync(path.join(__dirname, "../src/api.js"), "utf8");
const BACKGROUND = fs.readFileSync(path.join(__dirname, "../src/background.js"), "utf8");

function buildBrowser(options) {
  const listeners = {};
  const calls = [];
  const settings = options.settings || { serverUrl: "http://192.168.1.10:13381" };
  const tabs = options.tabs || [];

  const browser = {
    storage: { sync: { get: async () => ({ ...settings }) } },
    tabs: {
      query: async (query) => {
        if (query && query.url) {
          return tabs.filter((tab) => /youtube\.com\/(watch|shorts)/.test(tab.url || ""));
        }
        return tabs;
      },
      get: async (id) => tabs.find((tab) => tab.id === id),
      sendMessage: async () => options.pageInfo || null,
      onActivated: { addListener: (fn) => (listeners.activated = fn) },
      onUpdated: { addListener: (fn) => (listeners.updated = fn) },
    },
    action: {
      enable: async (id) => calls.push({ type: "enable", tabId: id }),
      disable: async (id) => calls.push({ type: "disable", tabId: id }),
      setBadgeText: async (details) => calls.push({ type: "badge", ...details }),
      setBadgeBackgroundColor: async () => {},
      setBadgeTextColor: async () => {},
      isEnabled: async () => true,
    },
    menus: {
      removeAll: async () => {},
      create: (item) => {
        calls.push({ type: "menu-create", item });
        return item.id;
      },
      onClicked: { addListener: (fn) => (listeners.menuClicked = fn) },
    },
    commands: { onCommand: { addListener: (fn) => (listeners.command = fn) } },
    runtime: {
      onMessage: { addListener: (fn) => (listeners.message = fn) },
      onInstalled: { addListener: (fn) => (listeners.installed = fn) },
      onStartup: { addListener: (fn) => (listeners.startup = fn) },
      getURL: (relative) => `moz-extension://test-uuid/${relative}`,
    },
    notifications: {
      create: async (note) => calls.push({ type: "notification", message: note.message }),
    },
  };
  return { browser, listeners, calls };
}

function loadBackground(options = {}) {
  const { browser, listeners, calls } = buildBrowser(options);
  const httpCalls = [];
  const fetchImpl =
    options.fetch ||
    (async (url, init) => {
      httpCalls.push({ url, init });
      return {
        ok: true,
        status: 200,
        headers: { get: () => null },
        text: async () =>
          JSON.stringify(
            options.job || {
              video_id: VIDEO,
              status: "queued",
              retention_override: null,
            }
          ),
      };
    });

  const context = vm.createContext({
    browser,
    console,
    fetch: fetchImpl,
    setTimeout,
    clearTimeout,
    Date,
    Map,
    Set,
    Promise,
    URL,
    URLSearchParams,
    AbortController,
    JSON,
    Error,
  });
  // Load the extension scripts in the same global, exactly like the manifest
  // does, so api.js picks up the mocked fetch from globalThis.
  vm.runInContext(LIB_SOURCE, context, { filename: "lib.js" });
  vm.runInContext(API_SOURCE, context, { filename: "api.js" });
  vm.runInContext(BACKGROUND, context, { filename: "background.js" });
  const settle = () => new Promise((resolve) => setTimeout(resolve, 10));
  return { context, listeners, calls, httpCalls, settle };
}

test("context menu click sends the thumbnail's video id without opening the video", async () => {
  const { listeners, calls, httpCalls, settle } = loadBackground({
    tabs: [{ id: 1, url: "https://www.youtube.com/results?search_query=x" }],
  });
  await settle();
  await listeners.menuClicked(
    {
      menuItemId: "yt2abs-send",
      linkUrl: `https://www.youtube.com/watch?v=${VIDEO}`,
      srcUrl: `https://i.ytimg.com/vi/${VIDEO}/hqdefault.jpg`,
    },
    { id: 1, url: "https://www.youtube.com/results?search_query=x" }
  );
  await settle();

  const posts = httpCalls.filter((call) => call.init && call.init.method === "POST");
  assert.equal(posts.length, 1, "exactly one POST");
  assert.match(posts[0].url, /\/api\/jobs$/);
  const body = JSON.parse(posts[0].init.body);
  assert.equal(body.url, `https://www.youtube.com/watch?v=${VIDEO}`);
  assert.equal(body.retention_override, null);
  assert.ok(
    calls.some((call) => call.type === "notification" && /Sent/.test(call.message)),
    "success notification"
  );
});

test("context menu click on a bare thumbnail (srcUrl only) still resolves the video", async () => {
  const { listeners, httpCalls, settle } = loadBackground({});
  await settle();
  await listeners.menuClicked(
    { menuItemId: "yt2abs-send", srcUrl: `https://i.ytimg.com/vi/${VIDEO}/maxresdefault.jpg` },
    { id: 2, url: "https://www.youtube.com/@channel" }
  );
  await settle();
  const thumbPosts = httpCalls.filter((call) => call.init && call.init.method === "POST");
  assert.equal(thumbPosts.length, 1);
  const body = JSON.parse(thumbPosts[0].init.body);
  assert.equal(body.url, `https://www.youtube.com/watch?v=${VIDEO}`);
});

test("context menu ignores items it does not own and non-YouTube targets", async () => {
  const { listeners, httpCalls, calls, settle } = loadBackground({});
  await settle();
  await listeners.menuClicked({ menuItemId: "other", linkUrl: `https://youtu.be/${VIDEO}` }, { id: 1 });
  await listeners.menuClicked({ menuItemId: "yt2abs-send", linkUrl: "https://example.com/photo.jpg" }, { id: 1 });
  await settle();
  assert.equal(httpCalls.length, 0);
  assert.ok(calls.some((call) => call.type === "notification" && /does not point/.test(call.message)));
});

test("Alt+Shift+Y command sends the current tab", async () => {
  const { listeners, httpCalls, settle } = loadBackground({
    tabs: [{ id: 7, url: `https://www.youtube.com/watch?v=${VIDEO}`, active: true }],
  });
  await settle();
  await listeners.command("send-current");
  await settle();
  const commandPosts = httpCalls.filter((call) => call.init && call.init.method === "POST");
  assert.equal(commandPosts.length, 1);
  const body = JSON.parse(commandPosts[0].init.body);
  assert.equal(body.url, `https://www.youtube.com/watch?v=${VIDEO}`);
});

test("command on a non-video tab notifies instead of sending", async () => {
  const { listeners, httpCalls, calls, settle } = loadBackground({
    tabs: [{ id: 3, url: "https://www.youtube.com/feed/subscriptions", active: true }],
  });
  await settle();
  await listeners.command("send-current");
  await settle();
  assert.equal(httpCalls.length, 0);
  assert.ok(calls.some((call) => call.type === "notification" && /Open a YouTube/.test(call.message)));
});

test("tabs.onUpdated enables the action on /watch, disables it elsewhere and sets the badge", async () => {
  const { listeners, calls, settle } = loadBackground({
    job: { video_id: VIDEO, status: "imported", retention_override: null },
  });
  await settle();
  listeners.updated(9, { status: "complete" }, { id: 9, url: "https://www.youtube.com/feed/subscriptions" });
  await settle();
  listeners.updated(8, { status: "complete" }, { id: 8, url: `https://www.youtube.com/watch?v=${VIDEO}` });
  await settle();

  assert.ok(calls.some((call) => call.type === "disable" && call.tabId === 9), "disabled off /watch");
  assert.ok(calls.some((call) => call.type === "enable" && call.tabId === 8), "enabled on /watch");
  assert.ok(
    calls.some((call) => call.type === "badge" && call.tabId === 8 && call.text === "✓"),
    "badge check mark when the video is already in ABS"
  );
});

test("popup messages: context, status, send (keep) and policy", async () => {
  const { listeners, httpCalls, settle } = loadBackground({
    tabs: [{ id: 4, url: `https://www.youtube.com/watch?v=${VIDEO}`, active: true }],
    pageInfo: { videoId: VIDEO, channelId: "UC123", channelName: "Rick Astley", title: "Test" },
    job: { video_id: VIDEO, status: "imported", retention_override: 0 },
  });
  await settle();

  const context = await listeners.message({ type: "context" });
  assert.equal(context.page.videoId, VIDEO);
  assert.equal(context.page.channelName, "Rick Astley");
  assert.equal(context.settings.serverUrl, "http://192.168.1.10:13381");

  const status = await listeners.message({ type: "status", videoId: VIDEO });
  assert.equal(status.ok, true);
  assert.equal(status.job.status, "imported");

  const sent = await listeners.message({ type: "send", videoId: VIDEO, mode: "keep", tabId: 4 });
  assert.equal(sent.ok, true);
  const keep = httpCalls.find((call) => /\/api\/jobs$/.test(call.url));
  assert.equal(JSON.parse(keep.init.body).retention_override, 0);

  const policy = await listeners.message({ type: "policy", channelId: "UC123", channelName: "Rick Astley" });
  assert.equal(policy.ok, true);
});

test("unconfigured extension returns a clear error instead of calling the server", async () => {
  const { listeners, httpCalls, settle } = loadBackground({ settings: { serverUrl: "" } });
  await settle();
  const result = await listeners.message({ type: "send", videoId: VIDEO, mode: "policy" });
  assert.equal(result.ok, false);
  assert.match(result.error, /server address/i);
  assert.equal(httpCalls.length, 0);
});

test("the context menu is created once, with a YouTube document filter", async () => {
  const { calls, settle } = loadBackground({});
  await settle();
  await new Promise((resolve) => setTimeout(resolve, 20));
  const created = calls.filter((call) => call.type === "menu-create");
  assert.equal(created.length, 1, "no duplicate menus from onInstalled + startup");
  assert.equal(created[0].item.title, "Send to ABS");
  assert.deepEqual([...created[0].item.contexts], ["link", "image"]);
  assert.deepEqual([...created[0].item.documentUrlPatterns], ["*://*.youtube.com/*"]);
});
