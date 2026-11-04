"use strict";

const POLL_MS = 5000;

const state = {
  tab: "queue",
  jobs: [],
  counts: {},
  filter: "active",
  channels: [],
  channelDefaults: {},
  system: null,
  abs: null,
  loading: false,
};

const $ = (id) => document.getElementById(id);

function el(tag, props, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "text") node.textContent = value;
    else if (key === "className") node.className = value;
    else if (key === "dataset") Object.assign(node.dataset, value);
    else if (key.startsWith("on") && typeof value === "function") {
      node.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (key in node) node[key] = value;
    else node.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

async function api(path, options = {}) {
  const { method = "GET", body = null } = options;
  const headers = { Accept: "application/json" };
  if (body !== null) headers["Content-Type"] = "application/json";
  let response;
  try {
    response = await fetch(path, {
      method,
      headers,
      body: body === null ? undefined : JSON.stringify(body),
      cache: "no-store",
    });
  } catch (err) {
    throw new ApiError(0, "Network error: " + err.message);
  }
  const text = await response.text();
  let data = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = text;
    }
  }
  if (!response.ok) {
    const detail =
      data && typeof data === "object" && data.detail
        ? data.detail
        : typeof data === "string" && data
          ? data
          : `HTTP ${response.status}`;
    throw new ApiError(response.status, String(detail));
  }
  return data;
}

/* ------------------------------------------------------------------- boot */
async function boot() {
  bindUi();
  try {
    state.system = await api("/api/system");
    await loadAll();
  } catch (err) {
    renderFatal(err.message);
  }
}

function renderFatal(message) {
  const list = $("job-list");
  list.replaceChildren(el("p", { className: "empty error", text: message }));
}

function bindUi() {
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => switchTab(tab.dataset.tab));
  });

  $("add-form").addEventListener("submit", addJob);
  $("add-retention").addEventListener("change", () => {
    $("add-days-field").hidden = $("add-retention").value !== "days";
  });
  $("refresh-jobs").addEventListener("click", () => refreshJobs().catch(showQueueError));
  document.querySelectorAll("#filters .chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      state.filter = chip.dataset.filter;
      document.querySelectorAll("#filters .chip").forEach((other) => {
        other.classList.toggle("active", other === chip);
      });
      renderJobs();
    });
  });

  $("add-channel-form").addEventListener("submit", addChannel);
  $("refresh-channels").addEventListener("click", () =>
    loadChannels().catch(showChannelError),
  );
  $("refresh-config").addEventListener("click", () => loadAll().catch(showConfigError));
  $("run-retention").addEventListener("click", runRetention);
  $("update-ytdlp").addEventListener("click", updateYtdlp);

  window.setInterval(() => {
    if (
      state.tab === "queue" &&
      document.visibilityState === "visible" &&
      !state.loading
    ) {
      refreshJobs(true).catch(() => {});
    }
  }, POLL_MS);
}

function switchTab(name) {
  state.tab = name;
  document.querySelectorAll(".tab").forEach((tab) => {
    const active = tab.dataset.tab === name;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", String(active));
  });
  document.querySelectorAll(".tab-panel").forEach((panel) => {
    panel.hidden = panel.id !== `tab-${name}`;
  });
  if (name === "channels" && !state.channels.length) {
    loadChannels().catch(showChannelError);
  }
  if (name === "config") {
    loadAbsStatus().catch(() => {});
  }
}

/* -------------------------------------------------------------------- load */
async function loadAll() {
  state.loading = true;
  try {
    const [system, jobs, channels] = await Promise.all([
      api("/api/system"),
      api("/api/jobs?limit=200"),
      api("/api/channels"),
    ]);
    state.system = system;
    state.jobs = jobs.jobs || [];
    state.counts = jobs.counts || {};
    state.channels = channels.channels || [];
    state.channelDefaults = channels.defaults || {};
    renderJobs();
    renderChannels();
    renderConfig();
    loadAbsStatus().catch(() => {});
  } catch (err) {
    if (err.status !== 401) throw err;
  } finally {
    state.loading = false;
  }
}

async function refreshJobs(silent = false) {
  try {
    const data = await api("/api/jobs?limit=200");
    state.jobs = data.jobs || [];
    state.counts = data.counts || {};
    renderJobs();
  } catch (err) {
    if (!silent && err.status !== 401) showQueueError(err);
  }
}

async function loadChannels() {
  const data = await api("/api/channels");
  state.channels = data.channels || [];
  state.channelDefaults = data.defaults || {};
  renderChannels();
  if (state.system) renderConfig();
}

async function loadAbsStatus() {
  try {
    const data = await api("/api/abs/status");
    state.abs = data;
  } catch (err) {
    state.abs = {
      configured: true,
      reachable: false,
      error: err.status === 401 ? null : err.message,
    };
  }
  if (state.system) renderConfig();
}

function showQueueError(err) {
  const node = $("queue-summary");
  node.className = "error";
  node.textContent = err.message;
}

function showChannelError(err) {
  const node = $("channel-summary");
  node.className = "error";
  node.textContent = err.message;
}

function showConfigError(err) {
  setNotice("config-result", err.message, "error");
}

/* ------------------------------------------------------------------- queue */
async function addJob(event) {
  event.preventDefault();
  const url = $("add-url").value.trim();
  const mode = $("add-retention").value;
  let override;
  if (mode === "keep") override = 0;
  else if (mode === "days") override = Number.parseInt($("add-days").value, 10);
  if (mode === "days" && (!Number.isInteger(override) || override < 1)) {
    setNotice("add-result", "Enter a number of days greater than zero. \u201cKeep forever\u201d is a separate option.", "error");
    return;
  }
  const button = event.target.querySelector("button[type=submit]");
  button.disabled = true;
  try {
    const body = { url };
    if (override !== undefined) body.retention_override = override;
    const job = await api("/api/jobs", { method: "POST", body });
    const message =
      job.created || job.requeued
        ? `Added \u201c${job.title || job.video_id}\u201d to the queue.`
        : `This video is already in the system (status: ${statusLabel(job.status)}).`;
    setNotice("add-result", message, "ok");
    $("add-url").value = "";
    $("add-retention").value = "default";
    $("add-days-field").hidden = true;
    await refreshJobs();
  } catch (err) {
    setNotice("add-result", err.message, "error");
  } finally {
    button.disabled = false;
  }
}

function setNotice(id, message, kind) {
  const node = $(id);
  node.textContent = message;
  node.className = "notice" + (kind ? " " + kind : "");
  node.hidden = false;
}

let toastTimer = null;
function showToast(message) {
  const node = $("toast");
  node.textContent = message;
  node.hidden = false;
  if (toastTimer) window.clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => {
    node.hidden = true;
  }, 8000);
}

function statusLabel(status) {
  return (
    {
      queued: "queued",
      downloading: "downloading",
      imported: "imported",
      failed: "failed",
      deleted: "deleted",
    }[status] || status
  );
}

function formatDate(value) {
  if (!value) return "\u2014";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat("en-US", {
    dateStyle: "short",
    timeStyle: "short",
  }).format(date);
}

function retentionText(job) {
  const retention = job.retention || {};
  if (job.status === "deleted") {
    return `Deleted ${formatDate(job.deleted_at)}`;
  }
  if (retention.keep) {
    return retention.source === "override" || retention.source === "keep"
      ? "Kept forever"
      : "No deadline (channel policy)";
  }
  if (job.status !== "imported") {
    return retention.days === 0
      ? "After import: deletion on the next run"
      : `Expires after import: ${retention.days} days`;
  }
  if (retention.days === 0) {
    return "To be deleted on the next retention run";
  }
  if (!retention.deadline) {
    return `Retention: ${retention.days} days`;
  }
  const when = formatDate(retention.deadline);
  const overdue = new Date(retention.deadline).getTime() < Date.now();
  const suffix = retention.source === "override" ? " \u2014 set for this video" : "";
  return overdue
    ? `Overdue (${when}) \u2014 deletion on the next run${suffix}`
    : `Until ${when} (${retention.days} days)${suffix}`;
}

function visibleJobs() {
  if (state.filter === "all") return state.jobs;
  if (state.filter === "active") {
    return state.jobs.filter((job) => job.status !== "deleted");
  }
  return state.jobs.filter((job) => job.status === state.filter);
}

function renderJobs() {
  const counts = state.counts || {};
  const active =
    (counts.queued || 0) +
    (counts.downloading || 0) +
    (counts.imported || 0) +
    (counts.failed || 0);
  $("queue-summary").className = "muted";
  $("queue-summary").textContent =
    `Active: ${active} \u00b7 queued: ${counts.queued || 0} \u00b7 downloading: ` +
    `${counts.downloading || 0} \u00b7 imported: ${counts.imported || 0} \u00b7 ` +
    `errors: ${counts.failed || 0} \u00b7 deleted: ${counts.deleted || 0}`;

  const jobs = visibleJobs();
  const list = $("job-list");
  if (!jobs.length) {
    list.replaceChildren(
      el("p", {
        className: "empty",
        text:
          state.filter === "active"
            ? "No active jobs. Paste a video link above."
            : "No jobs in this filter.",
      }),
    );
    return;
  }
  list.replaceChildren(...jobs.map(jobCard));
}

function jobCard(job) {
  const template = $("job-template");
  const card = template.content.firstElementChild.cloneNode(true);
  const image = card.querySelector(".thumb");
  image.src = `https://i.ytimg.com/vi/${encodeURIComponent(job.video_id)}/mqdefault.jpg`;
  image.alt = "";
  image.addEventListener("error", () => {
    image.hidden = true;
  });

  const title = card.querySelector(".job-title");
  if (job.url) {
    title.append(
      el(
        "a",
        { href: job.url, target: "_blank", rel: "noopener noreferrer" },
        job.title || job.video_id,
      ),
    );
  } else {
    title.textContent = job.title || job.video_id;
  }

  const badge = card.querySelector(".badge");
  badge.classList.add(job.status);
  badge.textContent = statusLabel(job.status);

  const channel = job.channel_title || job.channel_id || "unknown channel";
  card.querySelector(".job-meta").textContent =
    `${channel} \u00b7 added ${formatDate(job.added_at)}`;
  card.querySelector(".job-retention").textContent = retentionText(job);

  if (job.error) {
    const error = card.querySelector(".job-error");
    error.textContent = job.error;
    error.hidden = false;
  }

  const actions = card.querySelector(".job-actions");
  if (job.status === "failed") {
    actions.append(
      actionButton("Retry", "ghost", async () => {
        await api(`/api/jobs/${encodeURIComponent(job.video_id)}/retry`, {
          method: "POST",
        });
        await refreshJobs();
      }),
    );
  }
  if (job.status !== "deleted") {
    const keep = job.retention_override === 0;
    actions.append(
      actionButton(keep ? "Restore policy" : "Keep forever", "ghost", async () => {
        await api(`/api/jobs/${encodeURIComponent(job.video_id)}/retention`, {
          method: "POST",
          body: { override: keep ? null : 0 },
        });
        await refreshJobs();
      }),
    );
    actions.append(
      actionButton("Delete", "danger", async () => {
        const label = job.title || job.video_id;
        if (!window.confirm(`Delete \u201c${label}\u201d from Audiobookshelf and the queue?`)) return;
        try {
          await api(`/api/jobs/${encodeURIComponent(job.video_id)}`, {
            method: "DELETE",
          });
          await refreshJobs();
        } catch (err) {
          setNotice("add-result", err.message, "error");
        }
      }),
    );
  } else if (job.url) {
    actions.append(
      actionButton("Add again", "ghost", async () => {
        await api("/api/jobs", { method: "POST", body: { url: job.url } });
        await refreshJobs();
      }),
    );
  }
  return card;
}

function actionButton(label, variant, handler) {
  const button = el("button", {
    type: "button",
    className: `small ${variant}`,
    text: label,
  });
  button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      await handler();
    } catch (err) {
      showToast(err.message);
    } finally {
      button.disabled = false;
    }
  });
  return button;
}

/* Channels and configuration are appended below. */

/* ---------------------------------------------------------------- channels */
function retentionLabel(days) {
  if (days === null || days === undefined) return "keep forever";
  if (days === 0) return "delete on the next run";
  return `${days} days`;
}

function sponsorModeLabel(mode) {
  return (
    {
      off: "disabled",
      remove: "removing segments",
      mark: "marking with chapters",
      mute: "muting",
    }[mode] || mode
  );
}

function field(labelText, control) {
  return el("label", { className: "field" }, el("span", { text: labelText }), control);
}

function renderChannels() {
  const list = $("channel-list");
  const defaults = state.channelDefaults || {};
  $("channel-summary").className = "muted";
  $("channel-summary").textContent =
    `Channels: ${state.channels.length} \u00b7 default retention: ` +
    `${retentionLabel(defaults.retention_days)} \u00b7 protect unfinished: ` +
    `${defaults.protect_unfinished ? "yes" : "no"}`;
  if (!state.channels.length) {
    list.replaceChildren(
      el("p", { className: "empty", text: "No channels. Add your first channel below." }),
    );
    return;
  }
  list.replaceChildren(...state.channels.map(channelCard));
}

function channelCard(channel) {
  const uiRetention = channel.retention_source === "ui";
  const retentionValue = uiRetention
    ? channel.retention_days === null
      ? "keep"
      : "days"
    : "default";
  const retentionDays =
    channel.retention_days === null || channel.retention_days === undefined
      ? ""
      : String(channel.retention_days);
  const sponsorUi = channel.sponsorblock_source === "ui";
  const sponsorMode = sponsorUi ? channel.sponsorblock_mode : "default";
  const categories = (channel.sponsorblock_categories || []).join(",");
  const protectUi = channel.protect_unfinished_source === "ui";
  const protectValue = protectUi ? String(channel.protect_unfinished) : "default";

  const nameInput = el("input", { type: "text", value: channel.name || "", maxLength: 200 });
  const retentionSelect = el(
    "select",
    {},
    el("option", {
      value: "default",
      text: `Inherit: ${retentionLabel(channel.inherited_retention_days)}`,
    }),
    el("option", { value: "keep", text: "Keep forever" }),
    el("option", { value: "days", text: "Custom number of days" }),
  );
  retentionSelect.value = retentionValue;
  const daysInput = el("input", {
    type: "number",
    min: "0",
    step: "1",
    value: retentionDays,
  });
  const daysWrap = field("Retention days", daysInput);
  daysWrap.append(
    el("small", {
      className: "hint",
      text: "0 = delete on the next run (hard delete from ABS)",
    }),
  );
  daysWrap.hidden = retentionValue !== "days";

  const sponsorSelect = el(
    "select",
    {},
    el("option", {
      value: "default",
      text: `Inherit: ${sponsorModeLabel(channel.inherited_sponsorblock_mode)}`,
    }),
    el("option", { value: "off", text: "Disabled" }),
    el("option", { value: "remove", text: "Cut segments" }),
    el("option", { value: "mark", text: "Mark with chapters" }),
    el("option", { value: "mute", text: "Mute segments" }),
  );
  sponsorSelect.value = sponsorMode;
  const categoriesInput = el("input", {
    type: "text",
    value: categories,
    placeholder: "sponsor,selfpromo",
  });
  const categoriesWrap = field("Categories", categoriesInput);
  categoriesWrap.hidden = !["remove", "mark", "mute"].includes(sponsorMode);

  const protectSelect = el(
    "select",
    {},
    el("option", {
      value: "default",
      text: `Inherit: ${channel.inherited_protect_unfinished ? "yes" : "no"}`,
    }),
    el("option", { value: "true", text: "Yes \u2014 do not delete started episodes" }),
    el("option", { value: "false", text: "No \u2014 delete on schedule" }),
  );
  protectSelect.value = protectValue;

  const notice = el("p", { className: "notice", hidden: true });
  const saveButton = el("button", { type: "button", className: "primary small", text: "Save" });
  const resetButton = el("button", {
    type: "button",
    className: "ghost small",
    text: channel.in_config
      ? "Restore from config.yaml"
      : channel.folder_name
        ? "Restore default policy"
        : "Remove channel from panel",
  });

  const initial = {
    name: (channel.name || "").trim(),
    retention: uiRetention
      ? channel.retention_days === null
        ? null
        : channel.retention_days
      : "default",
    sponsor: sponsorUi ? channel.sponsorblock : null,
    protect: protectUi ? channel.protect_unfinished : null,
  };

  function currentName() {
    return nameInput.value.trim();
  }
  function currentRetention() {
    if (retentionSelect.value === "default") return "default";
    if (retentionSelect.value === "keep") return null;
    const value = Number.parseInt(daysInput.value, 10);
    return Number.isInteger(value) && value >= 0 ? value : undefined;
  }
  function currentSponsor() {
    if (sponsorSelect.value === "default") return null;
    if (sponsorSelect.value === "off") return "off";
    const cats = categoriesInput.value.trim();
    return cats ? `${sponsorSelect.value}:${cats}` : sponsorSelect.value;
  }
  function currentProtect() {
    if (protectSelect.value === "default") return null;
    return protectSelect.value === "true";
  }

  retentionSelect.addEventListener("change", () => {
    daysWrap.hidden = retentionSelect.value !== "days";
  });
  sponsorSelect.addEventListener("change", () => {
    categoriesWrap.hidden = !["remove", "mark", "mute"].includes(sponsorSelect.value);
  });

  saveButton.addEventListener("click", async () => {
    const body = {};
    if (currentName() !== initial.name) body.name = currentName();
    const retention = currentRetention();
    if (retention === undefined) {
      showCardNotice(notice, "Enter a valid retention day count.", "error");
      return;
    }
    if (JSON.stringify(retention) !== JSON.stringify(initial.retention)) {
      body.retention_days = retention;
    }
    const sponsor = currentSponsor();
    if (sponsor !== initial.sponsor) body.sponsorblock = sponsor;
    const protect = currentProtect();
    if (protect !== initial.protect) body.protect_unfinished = protect;
    if (!Object.keys(body).length) {
      showCardNotice(notice, "No changes.", "ok");
      return;
    }
    if (
      body.retention_days === 0 &&
      !window.confirm(
        "0-day retention will delete all imported episodes of this channel on the next run. Continue?",
      )
    ) {
      return;
    }
    saveButton.disabled = true;
    try {
      await api(`/api/channels/${encodeURIComponent(channel.channel_id)}`, {
        method: "PUT",
        body,
      });
      await loadChannels();
      await refreshJobs(true);
      showCardNotice(notice, "Saved. The change applies immediately.", "ok");
    } catch (err) {
      showCardNotice(notice, err.message, "error");
    } finally {
      saveButton.disabled = false;
    }
  });

  resetButton.addEventListener("click", async () => {
    if (!window.confirm("Discard panel changes saved for this channel?")) return;
    resetButton.disabled = true;
    try {
      await api(`/api/channels/${encodeURIComponent(channel.channel_id)}`, {
        method: "DELETE",
      });
      await loadChannels();
      await refreshJobs(true);
    } catch (err) {
      showCardNotice(notice, err.message, "error");
    } finally {
      resetButton.disabled = false;
    }
  });

  const sourceLabel =
    channel.source === "ui"
      ? channel.in_config
        ? "panel + config"
        : "panel"
      : channel.source === "config"
        ? "config.yaml"
        : "database";
  const fields = el(
    "div",
    { className: "channel-fields" },
    field("Name", nameInput),
    field("Retention", retentionSelect),
    daysWrap,
    field("SponsorBlock", sponsorSelect),
    categoriesWrap,
    field("Protect unfinished", protectSelect),
  );
  const actions = el("div", { className: "channel-actions" }, saveButton);
  if (channel.has_override) actions.append(resetButton);

  return el(
    "article",
    { className: "channel-card" },
    el(
      "div",
      { className: "channel-head" },
      el("h3", { text: channel.name || channel.channel_id }),
      el("span", { className: "source-tag", text: sourceLabel }),
      el("code", { className: "channel-id", text: channel.channel_id }),
    ),
    fields,
    actions,
    notice,
  );
}

function showCardNotice(_node, message, kind) {
  setNotice("channel-result", message, kind);
}

async function addChannel(event) {
  event.preventDefault();
  const channel = $("add-channel-id").value.trim();
  const name = $("add-channel-name").value.trim();
  if (!channel) return;
  const button = event.target.querySelector("button[type=submit]");
  button.disabled = true;
  try {
    const body = { channel };
    if (name) body.name = name;
    const result = await api("/api/channels", { method: "POST", body });
    setNotice(
      "add-channel-result",
      result.created
        ? `Added channel ${result.channel.name || result.channel.channel_id}.`
        : `Channel was already listed \u2014 saved settings for ${result.channel.channel_id}.`,
      "ok",
    );
    $("add-channel-id").value = "";
    $("add-channel-name").value = "";
    await loadChannels();
  } catch (err) {
    setNotice("add-channel-result", err.message, "error");
  } finally {
    button.disabled = false;
  }
}

/* ----------------------------------------------------------------- config */
function configCard(title, rows) {
  const dl = el("dl");
  for (const [label, value] of rows) {
    dl.append(el("dt", { text: label }));
    dl.append(el("dd", {}, value instanceof Node ? value : String(value)));
  }
  return el("div", { className: "config-card" }, el("h3", { text: title }), dl);
}

function renderConfig() {
  const system = state.system;
  if (!system) return;
  const defaults = system.defaults || {};
  const abs = system.abs || {};
  const absLive = state.abs;
  const ytdlp = system.ytdlp || {};
  const retention = system.retention || {};
  const auth = system.auth || {};
  const report = retention.last_report;

  let absStatus;
  if (!absLive) {
    absStatus = el("span", { className: "muted", text: "checking\u2026" });
  } else if (!absLive.configured) {
    absStatus = el("span", { className: "error", text: "not configured" });
  } else if (absLive.reachable) {
    absStatus = el("span", { text: `connected (version ${absLive.version || "?"})` });
  } else {
    absStatus = el("span", {
      className: "error",
      text: `no connection: ${absLive.error || "unknown error"}`,
    });
  }

  let retentionStatus = retention.enabled ? "enabled" : "disabled";
  if (retention.last_run) retentionStatus += ` \u00b7 last run ${formatDate(retention.last_run)}`;
  if (report) {
    retentionStatus +=
      ` \u00b7 checked ${report.checked}, deleted ${report.deleted.length}, ` +
      `deferred ${report.deferred.length}, errors ${report.errors.length}`;
  }

  const grid = $("config-grid");
  grid.replaceChildren(
    configCard("Defaults", [
      ["Retention", retentionLabel(defaults.retention_days)],
      ["Protect unfinished", defaults.protect_unfinished ? "yes" : "no"],
    ]),
    configCard("Audiobookshelf", [
      ["Status", absStatus],
      ["URL", abs.url || "—"],
      ["Library (library_id)", abs.library_id || "\u2014"],
      ["Path", abs.library_path || "\u2014"],
    ]),
    configCard("yt-dlp", [
      ["Version", ytdlp.version || "\u2014"],
      ["Auto-update", ytdlp.enabled ? `yes, every ${ytdlp.interval_hours} h` : "disabled"],
      ["Last change", ytdlp.last_update ? ytdlp.last_update.checked_at || "\u2014" : "\u2014"],
    ]),
    configCard("Retention", [
      ["Schedule", retentionStatus],
      ["Interval", `${retention.interval_minutes || 0} min`],
      ["On startup", retention.run_on_startup ? "yes" : "no"],
      ["Mode", retention.dry_run ? "dry run (no writes)" : "normal"],
      [
        "Safety fuse",
        retention.reconcile_blocked
          ? `blocked: ${retention.reconcile_blocked.reason}`
          : "ok",
      ],
      [
        "ABS progress",
        retention.progress_scope === "single_user"
          ? `account ${retention.progress_user || "?"}`
          : "all accounts",
      ],
    ]),
    configCard("Access", [
      [
        "API limit",
        auth.rate_limit_per_minute ? `${auth.rate_limit_per_minute} / min` : "no limit",
      ],
      [
        "/add limit",
        auth.public_rate_limit_per_minute
          ? `${auth.public_rate_limit_per_minute} / min`
          : "no limit",
      ],
      ["Public URL", auth.public_url || "\u2014"],
    ]),
  );
}

async function waitForRetention(previousStartedAt) {
  for (let attempt = 0; attempt < 180; attempt += 1) {
    await new Promise((resolve) => setTimeout(resolve, 1000));
    const status = await api("/api/retention");
    const startedAt = status.last_report ? status.last_report.started_at : null;
    if (!status.running && startedAt !== previousStartedAt) return status;
  }
  throw new Error("Retention is taking longer than 3 minutes - check GET /api/retention.");
}

async function runRetention() {
  const button = $("run-retention");
  button.disabled = true;
  try {
    const before = await api("/api/retention");
    const previous = before.last_report ? before.last_report.started_at : null;
    const result = await api("/api/retention/run", { method: "POST" });
    let report = result;
    if (result && result.status === "started") {
      setNotice("config-result", "Retention: run started in the background\u2026", "ok");
      const status = await waitForRetention(previous);
      report = status.last_report || {};
    }
    setNotice(
      "config-result",
      `Retention: checked ${report.checked || 0}, deleted ${(report.deleted || []).length}, ` +
        `deferred ${(report.deferred || []).length}, errors ${(report.errors || []).length}.` +
        (report.dry_run ? " (dry run - nothing was written)" : ""),
      "ok",
    );
    await loadAll();
  } catch (err) {
    setNotice("config-result", err.message, "error");
  } finally {
    button.disabled = false;
  }
}

async function updateYtdlp() {
  const button = $("update-ytdlp");
  button.disabled = true;
  try {
    const result = await api("/api/ytdlp/update", { method: "POST" });
    setNotice(
      "config-result",
      result.error
        ? `yt-dlp update failed: ${result.error}`
        : `yt-dlp: ${result.previous || "?"} → ${result.current || "?"}.`,
      result.error ? "error" : "ok",
    );
    await loadAll();
  } catch (err) {
    setNotice("config-result", err.message, "error");
  } finally {
    button.disabled = false;
  }
}

document.addEventListener("DOMContentLoaded", boot);

