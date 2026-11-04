/* Reads the current video/channel from the YouTube page.
 *
 * Only used to label the popup (channel name and its retention policy); the
 * video id itself is resolved from the tab URL in the background page.
 */
"use strict";

(function () {
  function metaContent(...selectors) {
    for (const selector of selectors) {
      const node = document.querySelector(selector);
      const value = node && (node.getAttribute("content") || node.textContent);
      if (value && value.trim()) return value.trim();
    }
    return "";
  }

  function videoIdFromLocation() {
    const match = location.pathname.match(/^\/(?:shorts|embed|live)\/([A-Za-z0-9_-]{11})/);
    if (match) return match[1];
    if (location.pathname === "/watch") {
      return new URLSearchParams(location.search).get("v") || "";
    }
    return "";
  }

  function pageInfo() {
    let channelId = metaContent('meta[itemprop="channelId"]');
    if (!channelId) {
      const link = document.querySelector(
        '#owner a[href^="/channel/"], ytd-watch-metadata a[href^="/channel/"], #channel-name a[href^="/channel/"]'
      );
      const href = (link && link.getAttribute("href")) || "";
      const match = href.match(/\/channel\/(UC[\w-]{20,})/);
      if (match) channelId = match[1];
    }
    let channelName = metaContent(
      'ytd-watch-metadata #owner #channel-name a',
      "#owner #channel-name a",
      "ytd-video-owner-renderer #channel-name a",
      "#channel-name a"
    );
    if (!channelName) {
      channelName = metaContent('meta[itemprop="author"]', 'link[itemprop="name"]');
    }
    const title = (document.title || "").replace(/\s*-\s*YouTube\s*$/, "").trim();
    return {
      videoId: videoIdFromLocation() || metaContent('meta[itemprop="videoId"]'),
      channelId: channelId || "",
      channelName: channelName || "",
      title,
      url: location.href,
    };
  }

  browser.runtime.onMessage.addListener((message) => {
    if (message && message.type === "page-info") return Promise.resolve(pageInfo());
    return undefined;
  });
})();
