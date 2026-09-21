/**
 * Live WebSocket updates for the run detail page.
 * Control buttons remain plain HTML forms (works without JS).
 */
(function () {
  const cfg = window.BROWSER_USE_RUN;
  if (!cfg || !cfg.id) {
    return;
  }

  const list = document.getElementById("event-list");
  const wsState = document.getElementById("ws-state");
  const statusEl = document.getElementById("run-status");
  const costEl = document.getElementById("run-cost");
  const decisionEl = document.getElementById("current-decision");
  let lastSeq = 0;
  let socket = null;
  let reconnectTimer = null;

  if (list) {
    const items = list.querySelectorAll("li[data-seq]");
    items.forEach(function (li) {
      const seq = Number(li.getAttribute("data-seq") || 0);
      if (seq > lastSeq) {
        lastSeq = seq;
      }
    });
  }

  function setWsState(text) {
    if (wsState) {
      wsState.textContent = text;
    }
  }

  function formatPayload(payload) {
    if (!payload || typeof payload !== "object") {
      return "";
    }
    try {
      return JSON.stringify(payload, null, 2);
    } catch (_err) {
      return String(payload);
    }
  }

  function prependEvent(msg) {
    if (!list || msg.type !== "event") {
      return;
    }
    if (msg.seq != null && msg.seq <= lastSeq) {
      return;
    }
    if (msg.seq != null) {
      lastSeq = msg.seq;
    }

    const li = document.createElement("li");
    if (msg.seq != null) {
      li.setAttribute("data-seq", String(msg.seq));
    }
    const seq = document.createElement("span");
    seq.className = "seq";
    seq.textContent = msg.seq != null ? "#" + msg.seq : "";
    const etype = document.createElement("span");
    etype.className = "etype";
    etype.textContent = msg.event_type || "event";
    const when = document.createElement("span");
    when.className = "muted";
    when.textContent = " " + (msg.occurred_at || "");
    li.appendChild(seq);
    li.appendChild(etype);
    li.appendChild(when);

    const payloadText = formatPayload(msg.payload);
    if (payloadText) {
      const pre = document.createElement("pre");
      pre.className = "payload";
      pre.textContent = payloadText;
      li.appendChild(pre);
    }
    list.insertBefore(li, list.firstChild);

    if (msg.event_type === "decision" || msg.event_type === "action_selected") {
      if (decisionEl) {
        decisionEl.hidden = false;
        decisionEl.textContent =
          "Latest: " + (msg.event_type || "decision") + " — " + payloadText.slice(0, 240);
      }
    }

    if (msg.event_type === "approval_requested") {
      // Server-rendered approval panel is authoritative; reload to show forms.
      window.location.reload();
    }

    if (
      msg.event_type === "run_finished" ||
      msg.event_type === "run_cancelled" ||
      msg.event_type === "approval_granted" ||
      msg.event_type === "approval_denied" ||
      msg.event_type === "approval_timeout" ||
      msg.event_type === "takeover_started" ||
      msg.event_type === "takeover_ended" ||
      msg.event_type === "run_paused" ||
      msg.event_type === "run_resumed"
    ) {
      window.setTimeout(function () {
        window.location.reload();
      }, 400);
    }
  }

  function connect() {
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const url =
      proto +
      "//" +
      window.location.host +
      "/api/runs/" +
      encodeURIComponent(cfg.id) +
      "/events?after_seq=" +
      encodeURIComponent(String(lastSeq));

    setWsState("Connecting…");
    socket = new WebSocket(url);

    socket.onopen = function () {
      setWsState("Live");
    };

    socket.onmessage = function (ev) {
      let msg;
      try {
        msg = JSON.parse(ev.data);
      } catch (_err) {
        return;
      }
      if (msg.type === "replay_end") {
        setWsState("Live");
        return;
      }
      if (msg.type === "error") {
        setWsState("Error: " + (msg.detail || "unknown"));
        return;
      }
      prependEvent(msg);
    };

    socket.onclose = function () {
      setWsState("Disconnected — retrying…");
      if (!cfg.isActive) {
        setWsState("Disconnected (run finished)");
        return;
      }
      if (reconnectTimer) {
        window.clearTimeout(reconnectTimer);
      }
      reconnectTimer = window.setTimeout(connect, 1500);
    };

    socket.onerror = function () {
      setWsState("WebSocket error");
    };
  }

  // Poll status so pause/approval buttons refresh even if WS is quiet.
  function pollStatus() {
    if (!cfg.isActive) {
      return;
    }
    fetch("/api/runs/" + encodeURIComponent(cfg.id), { credentials: "same-origin" })
      .then(function (res) {
        return res.ok ? res.json() : null;
      })
      .then(function (body) {
        if (!body || !statusEl) {
          return;
        }
        if (costEl && body.cost != null) {
          costEl.textContent = "$" + body.cost + " USD";
        }
        if (body.status && body.status !== statusEl.textContent) {
          statusEl.textContent = body.status;
          statusEl.className = "status status-" + body.status;
          window.location.reload();
        }
      })
      .catch(function () {
        /* ignore transient poll failures */
      });
  }

  connect();
  window.setInterval(pollStatus, 5000);
})();
