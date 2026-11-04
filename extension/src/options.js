/* Options page: server address in storage.sync. */
"use strict";

const lib = Yt2abs;
const api = Yt2absApi;

const STORAGE_DEFAULTS = { serverUrl: "" };

function el(id) {
  return document.getElementById(id);
}

function message(text, kind) {
  const node = el("message");
  node.textContent = text || "";
  node.className = kind || "";
}

function formSettings() {
  return {
    serverUrl: lib.normalizeServerUrl(el("server").value),
  };
}

async function load() {
  const stored = await browser.storage.sync.get(STORAGE_DEFAULTS);
  el("server").value = stored.serverUrl || "";
  el("version").textContent = browser.runtime.getManifest().version;
}

async function save(event) {
  event.preventDefault();
  const settings = formSettings();
  const problem = lib.validateSettings(settings);
  if (problem) {
    message(problem, "err");
    return;
  }
  try {
    await browser.storage.sync.set(settings);
    el("server").value = settings.serverUrl;
    await browser.runtime.sendMessage({ type: "settings-changed" });
    message("Settings saved.", "ok");
  } catch (err) {
    message(`Could not save: ${err.message || err}`, "err");
  }
}

async function test() {
  const settings = formSettings();
  const problem = lib.validateSettings(settings);
  if (problem) {
    message(problem, "err");
    return;
  }
  message("Checking connection…");
  try {
    const system = await api.getSystem(settings);
    const ytdlp = (system && system.ytdlp && system.ytdlp.version) || "?";
    const library = (system && system.abs && system.abs.library_id) || "?";
    const short = library.length > 8 ? `${library.slice(0, 8)}…` : library;
    message(`Connected: yt2abs ${system.version || "?"}, yt-dlp ${ytdlp}, library ${short}`, "ok");
  } catch (err) {
    message(api.describeFailure(err), "err");
  }
}

async function init() {
  el("form").addEventListener("submit", save);
  el("test").addEventListener("click", test);
  await load();
}

document.addEventListener("DOMContentLoaded", init);
