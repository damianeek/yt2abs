/* Toolbar popup: shows the current video, channel policy, job status and the
 * two send actions. All HTTP work happens in the background page. */
"use strict";

const lib = Yt2abs;

const state = {
  videoId: null,
  tab: null,
  page: null,
  settings: null,
  job: null,
  policy: null,
  busy: false,
  pollTimer: null,
};

function el(id) {
  return document.getElementById(id);
}

function show(viewId) {
  for (const id of ["view-setup", "view-unsupported", "view-video"]) {
    el(id).hidden = id !== viewId;
  }
}

function showError(message) {
  const node = el("error");
  node.textContent = message || "";
  node.hidden = !message;
}

function setHint(message) {
  const node = el("hint");
  node.textContent = message || "";
  node.hidden = !message;
}

function setBusy(busy) {
  el("send").disabled = busy;
  el("send-keep").disabled = busy || (state.job && state.job.retention_override === 0);
}

function openOptions() {
  browser.runtime.openOptionsPage();
  window.close();
}

function renderTitle() {
  const page = state.page || {};
  el("video-title").textContent = lib.shorten(page.title || state.videoId || "", 90);
  el("channel-name").textContent = page.channelName ? `Channel: ${page.channelName}` : "Channel: unknown";
}

function renderPolicy() {
  el("policy").textContent = lib.formatPolicy(state.policy, state.job ? state.job.retention_override : null);
}

function renderStatus() {
  const job = state.job;
  const status = job ? job.status : null;
  const node = el("status");
  node.textContent = lib.formatStatus(status);
  node.className = `status ${lib.statusClass(status)}`;
  let detail = "";
  if (status === "failed" && job.error) detail = lib.shorten(job.error, 90);
  else if (status === "imported") detail = "episode is in Audiobookshelf";
  else if (status === "deleted") detail = "deleted by retention";
  else if (status === "queued") detail = "waiting for download";
  else if (status === "downloading") detail = "downloading and importing";
  el("status-detail").textContent = detail;
  const keep = el("send-keep");
  if (job && job.retention_override === 0) {
    keep.textContent = "Kept forever";
  } else {
    keep.textContent = "Keep forever";
  }
  setBusy(state.busy);
}

async function loadStatus() {
  if (!state.page || !state.page.videoId) return;
  const response = await browser.runtime.sendMessage({
    type: "status",
    videoId: state.page.videoId,
  });
  if (response && response.ok) {
    state.job = response.job;
    renderStatus();
    renderPolicy();
    if (state.job && lib.isPending(state.job.status)) startPolling();
  } else if (response && response.error && response.code !== "config") {
    showError(response.error);
  }
}

async function loadPolicy() {
  const page = state.page || {};
  const response = await browser.runtime.sendMessage({
    type: "policy",
    channelId: page.channelId,
    channelName: page.channelName,
  });
  if (response && response.ok) {
    state.policy = response.policy;
    renderPolicy();
  }
}

function startPolling() {
  if (state.pollTimer) return;
  state.pollTimer = setTimeout(async () => {
    state.pollTimer = null;
    await loadStatus();
    const status = state.job && state.job.status;
    if (lib.isPending(status)) {
      startPolling();
      return;
    }
    if (status === "imported") {
      browser.runtime.sendMessage({
        type: "refresh-badge",
        videoId: state.page.videoId,
        tabId: state.tab ? state.tab.id : undefined,
      });
    }
  }, 2500);
}

async function submit(mode) {
  if (state.busy) return;
  state.busy = true;
  setBusy(true);
  showError("");
  setHint("");
  try {
    const response = await browser.runtime.sendMessage({
      type: "send",
      videoId: state.page.videoId,
      mode,
      tabId: state.tab ? state.tab.id : undefined,
    });
    if (!response || !response.ok) {
      showError((response && response.error) || "Could not send the video.");
      return;
    }
    state.job = response.job;
    renderStatus();
    renderPolicy();
    setHint(mode === lib.MODE_KEEP ? "Sent and kept forever." : "Sent to the ABS queue.");
    if (state.job && lib.isPending(state.job.status)) startPolling();
  } finally {
    state.busy = false;
    setBusy(false);
  }
}

async function init() {
  el("open-options").addEventListener("click", openOptions);
  el("setup-open").addEventListener("click", openOptions);
  el("send").addEventListener("click", () => submit(lib.MODE_POLICY));
  el("send-keep").addEventListener("click", () => submit(lib.MODE_KEEP));

  let context;
  try {
    context = await browser.runtime.sendMessage({ type: "context" });
  } catch (err) {
    showError("Could not read the extension configuration.");
    return;
  }
  if (!context) {
    showError("No response from the background page — reload the extension.");
    return;
  }

  state.settings = context.settings || {};
  state.tab = context.tab || null;
  state.page = context.page || {};
  state.videoId = state.page.videoId || null;
  el("server-label").textContent = state.settings.serverUrl || "no server";

  const problem = lib.validateSettings(state.settings);
  if (problem) {
    show("view-setup");
    return;
  }
  if (!state.videoId) {
    show("view-unsupported");
    return;
  }
  show("view-video");
  renderTitle();
  renderStatus();
  await Promise.all([loadStatus(), loadPolicy()]);
}

document.addEventListener("DOMContentLoaded", init);
