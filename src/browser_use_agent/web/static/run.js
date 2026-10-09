/* Progressive enhancement for the server-rendered run workspace. */
(function () {
  "use strict";
  const seed = document.getElementById("run-data");
  if (!seed) return;
  const cfg = JSON.parse(seed.textContent);
  const byId = (id) => document.getElementById(id);
  const list = byId("event-list");
  const filter = byId("event-filter");
  const events = new Map(cfg.events.map((event) => [event.seq, event]));
  let artifacts = cfg.artifacts;
  let latestScreenshot = cfg.latestScreenshot;
  let lastSeq = Math.max(0, ...events.keys());
  let selectedSeq = null;
  let socket = null;
  let reconnectTimer = null;
  let pollTimer = null;
  let stopped = false;
  let previewing = false;

  function text(id, value) { byId(id).textContent = value; }
  function element(tag, value, className) {
    const node = document.createElement(tag);
    if (value != null) node.textContent = value;
    if (className) node.className = className;
    return node;
  }
  function duration(seconds) {
    if (seconds == null || !Number.isFinite(seconds)) return "Not started";
    const value = Math.max(0, Math.round(seconds));
    if (value < 60) return value + "s";
    return Math.floor(value / 60) + "m " + value % 60 + "s";
  }
  function localTime(value, short = false) {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return value || "Unknown time";
    return short ? date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : date.toLocaleString();
  }
  function formatTimes(root) {
    root.querySelectorAll("time[datetime]").forEach((node) => {
      node.textContent = localTime(node.dateTime, Boolean(node.closest(".event-select")));
      node.title = localTime(node.dateTime);
    });
  }
  function presentation(event) {
    return event.display || { title: "Recorded event", summary: event.event_type || "", category: "other", tone: "neutral", details: {} };
  }
  function filterEvents() {
    let visible = 0;
    Array.from(list.children).forEach((row) => {
      row.hidden = filter.value !== "all" && (filter.value === "error" ? row.dataset.tone !== "error" : row.dataset.category !== filter.value);
      if (!row.hidden) visible += 1;
    });
    byId("events-empty").hidden = visible > 0;
    text("events-empty", events.size ? "No events match this filter." : "No activity recorded yet.");
  }
  function addEvent(event) {
    if (!Number.isInteger(event.seq) || events.has(event.seq)) return false;
    events.set(event.seq, event);
    lastSeq = Math.max(lastSeq, event.seq);
    const display = presentation(event);
    const row = byId("event-template").content.firstElementChild.cloneNode(true);
    row.id = "event-" + event.seq;
    row.dataset.seq = event.seq;
    row.dataset.category = display.category;
    row.dataset.tone = display.tone;
    row.querySelector("button").dataset.eventSeq = event.seq;
    const icon = row.querySelector(".event-icon");
    icon.classList.add("tone-" + display.tone);
    icon.textContent = display.tone === "success" ? "✓" : display.tone === "error" ? "!" : display.category === "browser" ? "◎" : "·";
    row.querySelector("strong").textContent = display.title;
    row.querySelector(".event-copy > span").textContent = display.summary;
    row.querySelector("time").dateTime = event.occurred_at || "";
    row.querySelector(".seq").textContent = "#" + event.seq;
    row.querySelector(".payload").textContent = JSON.stringify(event.payload || {}, null, 2);
    row.querySelector(".truncation").hidden = !event.truncated;
    formatTimes(row);
    const next = Array.from(list.children).find((item) => Number(item.dataset.seq) > event.seq);
    list.insertBefore(row, next || null);
    // ponytail: retain the recent event window; add paging when full-history browsing is needed.
    while (events.size > cfg.eventLimit) {
      const oldest = Math.min(...events.keys());
      events.delete(oldest);
      byId("event-" + oldest)?.remove();
    }
    return true;
  }
  function renderTimeline() {
    const timeline = byId("timeline");
    const focusedSeq = timeline.contains(document.activeElement) ? document.activeElement.dataset.eventSeq : null;
    const scrollLeft = timeline.scrollLeft;
    const start = new Date(cfg.startedAt || cfg.events[0]?.occurred_at).getTime();
    timeline.replaceChildren();
    [...events.values()].sort((a, b) => a.seq - b.seq).forEach((event) => {
      const display = presentation(event);
      const link = element("a", null, "timeline-event tone-" + display.tone);
      link.href = "#event-" + event.seq;
      link.dataset.eventSeq = event.seq;
      link.setAttribute("aria-current", String(event.seq === selectedSeq));
      link.append(element("span", display.title));
      const offset = (new Date(event.occurred_at).getTime() - start) / 1000;
      link.append(element("small", "#" + event.seq + (Number.isFinite(offset) && offset >= 0 ? " · " + duration(offset) : "")));
      link.title = display.title + " · " + localTime(event.occurred_at);
      const item = element("li");
      item.append(link);
      timeline.append(item);
    });
    timeline.scrollLeft = scrollLeft;
    if (focusedSeq) timeline.querySelector('[data-event-seq="' + Number(focusedSeq) + '"]')?.focus({ preventScroll: true });
    byId("timeline-empty").hidden = events.size > 0;
  }
  function artifactLink(artifact, preview) {
    const link = element("a", preview ? "Preview screenshot" : "Download " + artifact.kind.replaceAll("_", " "));
    link.href = preview ? artifact.preview_url : artifact.url;
    if (preview) {
      link.className = "artifact-preview";
      link.dataset.artifactId = artifact.id;
    } else link.download = "";
    return link;
  }
  function selectEvent(seq, reveal = false) {
    const event = events.get(seq);
    if (!event) return;
    selectedSeq = seq;
    const display = presentation(event);
    byId("selection-empty").hidden = true;
    byId("selection-content").hidden = false;
    text("selection-title", display.title);
    text("selection-seq", "#" + event.seq);
    text("selection-summary", display.summary);
    const details = byId("selection-details");
    details.replaceChildren();
    const fields = { ...display.details, Time: localTime(event.occurred_at), Actor: event.actor || "Unknown", Event: event.event_type };
    Object.entries(fields).forEach(([label, value]) => {
      if (value != null) details.append(element("dt", label), element("dd", String(value)));
    });
    text("selection-payload", JSON.stringify(event.payload || {}, null, 2));
    byId("selection-truncated").hidden = !event.truncated;
    const links = byId("selection-artifacts");
    links.replaceChildren();
    artifacts.filter((artifact) => artifact.event_seq === seq || artifact.id === event.payload?.artifact_id || (event.step_id && artifact.step_id === event.step_id)).forEach((artifact) => {
      if (artifact.preview_url) links.append(artifactLink(artifact, true));
      links.append(artifactLink(artifact, false));
    });
    list.querySelectorAll(".event-select").forEach((button) => button.setAttribute("aria-pressed", String(Number(button.dataset.eventSeq) === seq)));
    byId("timeline").querySelectorAll("a").forEach((link) => link.setAttribute("aria-current", String(Number(link.dataset.eventSeq) === seq)));
    if (reveal) {
      const row = byId("event-" + seq);
      if (row.hidden) { filter.value = "all"; filterEvents(); }
      row.scrollIntoView({ block: "nearest" });
      row.querySelector("button").focus({ preventScroll: true });
    }
  }
  function renderArtifacts() {
    const container = byId("artifact-list");
    container.replaceChildren();
    artifacts.forEach((artifact) => {
      const row = element("li");
      row.dataset.artifactId = artifact.id;
      row.append(element("span", artifact.preview_url ? "▧" : "▤", "artifact-icon"));
      const info = element("div");
      info.append(element("strong", artifact.kind.replaceAll("_", " ")));
      info.append(element("small", artifact.media_type + " · " + artifact.size_bytes + " bytes"));
      const when = element("time");
      when.dateTime = artifact.created_at;
      info.append(when);
      row.append(info);
      if (artifact.preview_url) row.append(artifactLink(artifact, true));
      const download = artifactLink(artifact, false);
      download.textContent = "Download";
      row.append(download);
      container.append(row);
    });
    byId("artifacts-empty").hidden = artifacts.length > 0;
    formatTimes(container);
  }
  const screenshot = byId("browser-screenshot");
  const liveBrowser = byId("live-browser");
  const originalLocation = byId("browser-location").textContent;
  const originalCaption = byId("browser-caption").textContent;
  let screenshotCaption = "";
  function showScreenshot(artifact, selected = true) {
    if (!artifact?.preview_url) return;
    previewing = selected;
    if (liveBrowser) liveBrowser.hidden = true;
    screenshot.hidden = false;
    byId("browser-empty").hidden = true;
    text("browser-mode", "Recorded screenshot");
    text("browser-location", artifact.page_url || artifact.title || "Saved browser state for this run");
    screenshotCaption = "Recorded " + localTime(artifact.created_at) + " · " + (selected ? "Selected artifact" : "Latest screenshot");
    text("browser-caption", "Loading recorded screenshot…");
    screenshot.src = artifact.preview_url;
    byId("return-to-browser").hidden = !selected;
  }
  screenshot.addEventListener("error", () => {
    if (!previewing && cfg.isActive) return;
    screenshot.hidden = true;
    byId("browser-empty").hidden = false;
    text("browser-caption", "Screenshot unavailable");
    text("browser-empty", "This screenshot is unavailable. It may have expired or failed to load. Select Preview to retry.");
  });
  screenshot.addEventListener("load", () => {
    if (previewing || !cfg.isActive) {
      screenshot.hidden = false; byId("browser-empty").hidden = true;
      text("browser-caption", screenshotCaption);
    }
  });
  byId("return-to-browser").addEventListener("click", () => {
    previewing = false;
    byId("return-to-browser").hidden = true;
    if (liveBrowser) {
      liveBrowser.hidden = false;
      screenshot.hidden = true;
      byId("browser-empty").hidden = true;
      text("browser-mode", "Live session");
      text("browser-location", originalLocation);
      text("browser-caption", originalCaption);
    } else showScreenshot(latestScreenshot, false);
  });
  document.addEventListener("click", (event) => {
    const preview = event.target.closest(".artifact-preview");
    if (preview) {
      const artifact = artifacts.find((item) => item.id === preview.dataset.artifactId);
      if (artifact) { event.preventDefault(); showScreenshot(artifact); byId("browser-frame").scrollIntoView({ block: "nearest" }); }
    }
    const selection = event.target.closest("[data-event-seq]");
    if (selection) { event.preventDefault(); selectEvent(Number(selection.dataset.eventSeq), selection.closest("#timeline") !== null); }
  });
  const tabs = [...byId("history-tabs").querySelectorAll("[role=tab]")];
  function activateTab(tab) {
    tabs.forEach((item) => {
      const selected = item === tab;
      item.setAttribute("aria-selected", String(selected));
      item.tabIndex = selected ? 0 : -1;
      byId(item.getAttribute("aria-controls")).hidden = !selected;
    });
  }
  tabs.forEach((tab, index) => {
    tab.addEventListener("click", () => activateTab(tab));
    tab.addEventListener("keydown", (event) => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : (index + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
      activateTab(tabs[next]); tabs[next].focus();
    });
  });
  if (document.fullscreenEnabled) {
    byId("fullscreen").hidden = false;
    byId("fullscreen").addEventListener("click", () => {
      byId("browser-frame").requestFullscreen().catch(() => text("browser-caption", "Full screen is unavailable. Use Open live view to open the browser separately."));
    });
  }
  function updateSummary(summary, cost) {
    ["steps", "actions", "artifacts"].forEach((key) => text("summary-" + key, summary[key]));
    text("artifact-count", "(" + summary.artifacts + ")");
    document.querySelectorAll("[data-elapsed]").forEach((node) => { node.textContent = duration(summary.elapsed_seconds); });
    if (cost !== undefined) text("run-cost", cost == null ? "Not priced" : "$" + cost + " USD");
  }
  function updateEvents(incoming) {
    const atBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 40;
    let changed = false;
    incoming.forEach((event) => { if (addEvent(event)) changed = true; });
    if (!changed) return;
    renderTimeline(); filterEvents();
    const latest = presentation(events.get(Math.max(...events.keys())));
    text("activity-update", latest.title + ". " + latest.summary);
    if (!events.has(selectedSeq)) selectEvent(Math.max(...events.keys()));
    if (atBottom) list.scrollTop = list.scrollHeight;
  }
  async function pollActivity() {
    try {
      const response = await fetch("/runs/" + encodeURIComponent(cfg.id) + "/activity", { credentials: "same-origin", cache: "no-store" });
      if (!response.ok) throw new Error("HTTP " + response.status);
      const body = await response.json();
      if (body.status !== cfg.status || JSON.stringify(body.result) !== JSON.stringify(cfg.result) || JSON.stringify(body.evidence) !== JSON.stringify(cfg.evidence)) { window.location.reload(); return; }
      cfg.startedAt = body.started_at;
      cfg.finishedAt = body.finished_at;
      updateEvents(body.events);
      updateSummary(body.summary, body.cost);
      if (JSON.stringify(artifacts) !== JSON.stringify(body.artifacts)) {
        artifacts = body.artifacts; renderArtifacts();
        if (selectedSeq !== null) selectEvent(selectedSeq);
      }
      latestScreenshot = body.latest_screenshot;
      text("ws-state", socket?.readyState === WebSocket.OPEN ? "Live updates connected" : "Activity refreshed · reconnecting live updates…");
    } catch (_error) {
      text("ws-state", "Unable to refresh activity. Retrying…");
    } finally {
      if (!stopped) pollTimer = window.setTimeout(pollActivity, 5000);
    }
  }
  function connect() {
    if (stopped) return;
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    socket = new WebSocket(proto + "//" + window.location.host + "/api/runs/" + encodeURIComponent(cfg.id) + "/events?after_seq=" + lastSeq);
    socket.onopen = () => text("ws-state", "Live updates connected");
    socket.onmessage = (message) => {
      let event;
      try { event = JSON.parse(message.data); } catch (_error) { return; }
      if (event.type === "event") updateEvents([event]);
      else if (event.type === "error") text("ws-state", "Activity error: " + (event.detail || "Unable to load updates"));
    };
    socket.onerror = () => text("ws-state", "Live connection unavailable. Activity refresh continues.");
    socket.onclose = () => {
      if (stopped) return;
      text("ws-state", "Disconnected · reconnecting…");
      reconnectTimer = window.setTimeout(connect, 1500);
    };
  }
  filter.addEventListener("change", filterEvents);
  formatTimes(document);
  activateTab(tabs[0]);
  renderTimeline();
  filterEvents();
  updateSummary(cfg.summary);
  if (events.size) selectEvent(lastSeq);
  if (!cfg.isActive && latestScreenshot) showScreenshot(latestScreenshot, false);
  if (cfg.isActive) { connect(); pollTimer = window.setTimeout(pollActivity, 5000); }
  window.addEventListener("pagehide", () => {
    stopped = true;
    window.clearTimeout(reconnectTimer); window.clearTimeout(pollTimer);
    if (socket) socket.close();
  });
  window.addEventListener("pageshow", (event) => { if (event.persisted) window.location.reload(); });
})();
