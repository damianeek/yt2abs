/* Thin JSON client for the yt2abs HTTP API.
 *
 * The server does not send CORS headers: the extension relies on its host
 * permission instead, which is why every call goes through this helper.
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) {
    module.exports = factory(require("./lib.js"));
  } else {
    root.Yt2absApi = factory(root.Yt2abs);
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function (lib) {
  "use strict";

  const DEFAULT_TIMEOUT = 15000;

  class ApiError extends Error {
    constructor(message, options) {
      super(message);
      this.name = "ApiError";
      const opts = options || {};
      this.status = opts.status || 0;
      this.code = opts.code || "error";
      this.retryAfter = opts.retryAfter || 0;
    }
  }

  function describeFailure(error) {
    if (!error) return "Unknown error.";
    if (error.retryAfter > 0) return `${error.message} (retry in ${error.retryAfter} s)`;
    return error.message || "Unknown error.";
  }

  async function request(settings, path, options) {
    const opts = options || {};
    const base = lib.normalizeServerUrl(settings && settings.serverUrl);
    if (!base) throw new ApiError("Server address is not configured.", { code: "no_server" });
    const fetchImpl =
      opts.fetchImpl ||
      (typeof globalThis !== "undefined" && typeof globalThis.fetch === "function"
        ? globalThis.fetch.bind(globalThis)
        : null);
    if (typeof fetchImpl !== "function") {
      throw new ApiError("This browser does not support fetch.", { code: "no_fetch" });
    }

    const timeout = opts.timeout || DEFAULT_TIMEOUT;
    const controller = typeof AbortController === "function" ? new AbortController() : null;
    const timer = controller ? setTimeout(() => controller.abort(), timeout) : null;
    const headers = { Accept: "application/json" };
    let body;
    if (opts.body !== undefined && opts.body !== null) {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(opts.body);
    }

    let response;
    try {
      response = await fetchImpl(`${base}${path}`, {
        method: opts.method || "GET",
        headers,
        body,
        credentials: "omit",
        redirect: "follow",
        ...(controller ? { signal: controller.signal } : {}),
      });
    } catch (err) {
      const aborted = controller && controller.signal.aborted;
      throw new ApiError(
        aborted ? "The server did not respond in time." : "No connection to the yt2abs server.",
        { code: aborted ? "timeout" : "network" }
      );
    } finally {
      if (timer) clearTimeout(timer);
    }

    const text = await response.text();
    let payload = null;
    if (text) {
      try {
        payload = JSON.parse(text);
      } catch (err) {
        payload = text;
      }
    }

    if (!response.ok) {
      throw new ApiError(lib.describeHttpError(response.status, payload), {
        status: response.status,
        code: "http",
        retryAfter: lib.parseRetryAfter(
          response.headers && typeof response.headers.get === "function"
            ? response.headers.get("Retry-After")
            : null
        ),
      });
    }
    return payload;
  }

  function postJob(settings, videoId, mode, options) {
    let payload;
    try {
      payload = lib.buildJobPayload(videoId, mode);
    } catch (err) {
      return Promise.reject(new ApiError(err.message, { code: "bad_video" }));
    }
    return request(settings, "/api/jobs", { ...(options || {}), method: "POST", body: payload });
  }

  async function getJob(settings, videoId, options) {
    try {
      return await request(settings, `/api/jobs/${encodeURIComponent(videoId)}`, options);
    } catch (err) {
      if (err.status === 404) return null;
      throw err;
    }
  }

  function getPolicy(settings, page, options) {
    return request(settings, `/api/policy${lib.policyQuery(page || {})}`, options);
  }

  function getSystem(settings, options) {
    return request(settings, "/api/system", { timeout: 8000, ...(options || {}) });
  }

  return { ApiError, request, postJob, getJob, getPolicy, getSystem, describeFailure };
});
