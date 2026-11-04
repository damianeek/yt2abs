/* yt2abs background event page (Manifest V3, Firefox).
 *
 * Responsibilities:
 *  - toolbar button enabled only on /watch and /shorts,
 *  - badge "✓" when the video is already in Audiobookshelf,
 *  - context menu "Send to ABS" on YouTube thumbnails/links,
 *  - Alt+Shift+Y command for the current video,
 *  - the single place that talks to the yt2abs HTTP API.
 */
"use strict";

const lib = Yt2abs;
const api = Yt2absApi;

const MENU_ID = "yt2abs-send";
const STORAGE_DEFAULTS = { serverUrl: "" };
const NOTIFICATION_ICON = "icons/icon-48.png";
const BADGE_TTL_MS = 20000;
const BADGE_BG = "#2e7d32";

const badgeCache = new Map();

async function readSettings() {
  const stored = await browser.storage.sync.get(STORAGE_DEFAULTS);
  return {
    serverUrl: lib.normalizeServerUrl(stored.serverUrl),
    rawServerUrl: String(stored.serverUrl || ""),
  };
}

async function currentTab() {
  const tabs = await browser.tabs.query({ active: true, currentWindow: true });
  return (tabs && tabs[0]) || null;
}

function isExtensionPage(url) {
  return !url || !/^https?:/i.test(url);
}

async function bestYoutubeTab() {
  const tabs = await browser.tabs.query({
    url: ["*://*.youtube.com/watch*", "*://*.youtube.com/shorts*"],
  });
  const candidates = (tabs || []).filter((tab) => /^https?:/i.test(tab.url || ""));
  candidates.sort((a, b) => (b.lastAccessed || 0) - (a.lastAccessed || 0));
  return candidates[0] || null;
}

async function pageInfoForTab(tab) {
  const info = {
    videoId: lib.extractVideoId(tab && tab.url),
    channelId: "",
    channelName: "",
    title: (tab && tab.title) || "",
    url: (tab && tab.url) || "",
  };
  if (!tab || tab.id === undefined || !/youtube\.com/i.test(tab.url || "")) return info;
  try {
    const extra = await browser.tabs.sendMessage(tab.id, { type: "page-info" });
    if (extra) {
      info.videoId = info.videoId || extra.videoId || null;
      info.channelId = extra.channelId || "";
      info.channelName = extra.channelName || "";
      info.title = extra.title || info.title;
    }
  } catch (err) {
    // Content script not available (page still loading, YouTube Music, ...).
  }
  return info;
}

async function setBadge(tabId, text) {
  if (typeof tabId !== "number" || tabId < 0) return;
  try {
    await browser.action.setBadgeText({ tabId, text: text || "" });
    if (text) {
      await browser.action.setBadgeBackgroundColor({ tabId, color: BADGE_BG });
      try {
        await browser.action.setBadgeTextColor({ tabId, color: "#ffffff" });
      } catch (err) {
        // Older Firefox: default text colour is fine.
      }
    }
  } catch (err) {
    // Tab already closed.
  }
}

async function refreshBadge(tab) {
  if (!tab || typeof tab.id !== "number" || tab.id < 0) return;
  const videoId = lib.extractVideoId(tab.url);
  if (!videoId) return setBadge(tab.id, "");
  const cached = badgeCache.get(videoId);
  if (cached && Date.now() - cached.at < BADGE_TTL_MS) {
    return setBadge(tab.id, lib.badgeText(cached.status));
  }
  const settings = await readSettings();
  if (lib.validateSettings(settings)) return setBadge(tab.id, "");
  try {
    const job = await api.getJob(settings, videoId);
    const status = job ? job.status : null;
    badgeCache.set(videoId, { at: Date.now(), status });
    return setBadge(tab.id, lib.badgeText(status));
  } catch (err) {
    return setBadge(tab.id, "");
  }
}

async function refreshTab(tab) {
  if (!tab || typeof tab.id !== "number" || tab.id < 0) return;
  const supported = lib.isSupportedPage(tab.url);
  try {
    if (supported) await browser.action.enable(tab.id);
    else await browser.action.disable(tab.id);
  } catch (err) {
    // Tab closed while we were deciding.
  }
  await refreshBadge(tab);
}

let menusPromise = null;

function configureMenus() {
  // Serialise the calls: onInstalled and the startup call race otherwise, and
  // the second removeAll() would delete the item created by the first call.
  if (!menusPromise) {
    menusPromise = (async () => {
      try {
        await browser.menus.removeAll();
      } catch (err) {
        // Nothing created yet.
      }
      try {
        browser.menus.create({
          id: MENU_ID,
          title: "Send to ABS",
          contexts: ["link", "image"],
          documentUrlPatterns: ["*://*.youtube.com/*"],
        });
      } catch (err) {
        console.error("yt2abs: cannot create the context menu", err);
      }
    })();
  }
  return menusPromise;
}

async function notify(message) {
  try {
    await browser.notifications.create({
      type: "basic",
      iconUrl: browser.runtime.getURL(NOTIFICATION_ICON),
      title: "yt2abs",
      message: lib.shorten(message, 220),
    });
  } catch (err) {
    // Notifications are best-effort (disabled by the OS, etc.).
  }
}

async function sendVideo(message) {
  const settings = await readSettings();
  const problem = lib.validateSettings(settings);
  if (problem) return { ok: false, error: problem, code: "config", status: 0 };
  const mode = message.mode === lib.MODE_KEEP ? lib.MODE_KEEP : lib.MODE_POLICY;
  try {
    const job = await api.postJob(settings, message.videoId, mode);
    badgeCache.delete(message.videoId);
    if (typeof message.tabId === "number") refreshBadge({ id: message.tabId, url: lib.watchUrl(message.videoId) });
    return { ok: true, job };
  } catch (err) {
    return {
      ok: false,
      error: api.describeFailure(err),
      code: err.code || "error",
      status: err.status || 0,
      retryAfter: err.retryAfter || 0,
    };
  }
}

async function buildContext() {
  const settings = await readSettings();
  let tab = await currentTab();
  if (!tab || isExtensionPage(tab.url)) {
    tab = (await bestYoutubeTab()) || tab;
  }
  const page = await pageInfoForTab(tab);
  return {
    settings: { serverUrl: settings.serverUrl, rawServerUrl: settings.rawServerUrl },
    tab: tab ? { id: tab.id, url: tab.url, title: tab.title } : null,
    page,
  };
}

async function handleMessage(message) {
  switch (message && message.type) {
    case "context":
      return buildContext();
    case "status": {
      const settings = await readSettings();
      const problem = lib.validateSettings(settings);
      if (problem) return { ok: false, error: problem, code: "config", job: null };
      try {
        const job = await api.getJob(settings, message.videoId);
        if (job) badgeCache.set(message.videoId, { at: Date.now(), status: job.status });
        return { ok: true, job };
      } catch (err) {
        return { ok: false, error: api.describeFailure(err), code: err.code || "error", job: null };
      }
    }
    case "policy": {
      const settings = await readSettings();
      if (lib.validateSettings(settings)) return { ok: false, error: "Not configured", policy: null };
      try {
        const policy = await api.getPolicy(settings, {
          channelId: message.channelId,
          channelName: message.channelName,
        });
        return { ok: true, policy };
      } catch (err) {
        return { ok: false, error: api.describeFailure(err), policy: null };
      }
    }
    case "send":
      return sendVideo(message);
    case "refresh-badge":
      badgeCache.delete(message.videoId);
      await refreshBadge({ id: message.tabId, url: lib.watchUrl(message.videoId) });
      return { ok: true };
    case "settings-changed": {
      badgeCache.clear();
      const tab = await currentTab();
      if (tab) await refreshTab(tab);
      return { ok: true };
    }
    default:
      return { ok: false, error: "Unknown request." };
  }
}

browser.runtime.onMessage.addListener((message) => handleMessage(message));

browser.runtime.onInstalled.addListener(() => {
  configureMenus();
});

browser.runtime.onStartup.addListener(() => {
  configureMenus();
});

browser.commands.onCommand.addListener(async (command) => {
  if (command !== "send-current") return;
  const tab = await currentTab();
  const videoId = lib.extractVideoId(tab && tab.url);
  if (!videoId) {
    await notify("Open a YouTube video (watch) or Shorts.");
    return;
  }
  const result = await sendVideo({ videoId, mode: lib.MODE_POLICY, tabId: tab.id });
  await notify(result.ok ? `Sent ${videoId} to the ABS queue.` : `Could not send: ${result.error}`);
});

browser.menus.onClicked.addListener(async (info, tab) => {
  if (!info || info.menuItemId !== MENU_ID) return;
  const videoId = lib.menuVideoId(info, tab && tab.url);
  if (!videoId) {
    await notify("This item does not point to a YouTube video.");
    return;
  }
  const result = await sendVideo({ videoId, mode: lib.MODE_POLICY, tabId: tab && tab.id });
  await notify(
    result.ok ? `Sent ${videoId} to the ABS queue.` : `Could not send: ${result.error}`
  );
});

browser.tabs.onActivated.addListener(async ({ tabId }) => {
  try {
    await refreshTab(await browser.tabs.get(tabId));
  } catch (err) {
    // Tab gone.
  }
});

browser.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  const urlChanged = typeof changeInfo.url === "string";
  if (changeInfo.status !== "complete" && !urlChanged) return;
  refreshTab(tab);
});

configureMenus();
