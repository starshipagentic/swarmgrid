const state = {
  snapshot: null,
  selectedKey: null,
  mode: "summary",
  observerSession: null,
  observerOutputBySession: {},
  liveWs: null,
  liveIssueKey: null,
};

const byId = (id) => document.getElementById(id);

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    cache: "no-store",
    ...options,
  });
  if (!response.ok) {
    throw new Error((await response.text()) || `${response.status} ${response.statusText}`);
  }
  return response.json();
}

function selectedTicket() {
  return state.snapshot?.columns.flatMap((col) => col.tickets).find((row) => row.key === state.selectedKey) || null;
}

async function refresh() {
  state.snapshot = await api("/api/snapshot");
  if (!selectedTicket()) {
    state.selectedKey = state.snapshot.columns.flatMap((col) => col.tickets)[0]?.key || null;
  }
  render();
  await refreshObserver();
}

function render() {
  const snapshot = state.snapshot;
  if (!snapshot) return;

  byId("headline").textContent = `${snapshot.config.project_key} · board ${snapshot.config.board_id || "-"} · workdir ${snapshot.config.workdir || "-"}`;
  byId("counts").textContent = `live ${snapshot.counts.live_rows} · active ${snapshot.counts.active_rows}/${snapshot.config.max_parallel} · next ${timeFrom(snapshot.controller.next_run_at)}`;
  byId("board-meta").textContent = snapshot.counts.capped_not_shown
    ? `showing first ${snapshot.counts.visible_rows} tickets · ${snapshot.counts.capped_not_shown} not shown`
    : `showing ${snapshot.counts.visible_rows} tickets`;

  renderColumns(snapshot.columns);
  renderSelectedTicket();
}

function renderColumns(columns) {
  const root = byId("columns");
  root.innerHTML = "";
  for (const column of columns) {
    const section = document.createElement("section");
    section.className = "column";
    const tickets = column.tickets.length ? column.tickets.map(renderTicket).join("") : `<div class="ticket"><div class="summary">No tickets</div></div>`;
    section.innerHTML = `<h3>${column.status} (${column.count})</h3>${tickets}`;
    root.appendChild(section);
  }
  root.querySelectorAll(".ticket[data-key]").forEach((node) => {
    node.addEventListener("click", () => {
      state.selectedKey = node.dataset.key;
      state.observerSession = null;
      disconnectLive();
      render();
    });
  });
}

function renderTicket(ticket) {
  const selected = ticket.key === state.selectedKey ? " selected" : "";
  const mode = ticket.local_mode && ticket.local_mode !== "none" ? ` ${ticket.local_mode}` : "";
  return `
    <article class="ticket${selected}${mode}" data-key="${ticket.key}">
      <div class="key">${ticket.key}</div>
      <div class="meta">${escapeHtml(ticket.issue_type)} · ${escapeHtml(ticket.status_name)}</div>
      <div class="summary">${escapeHtml(ticket.summary)}</div>
    </article>
  `;
}

function renderSelectedTicket() {
  const ticket = selectedTicket();
  byId("open-iterm").disabled = !ticket?.session_name;
  byId("run-now").disabled = !ticket;
  byId("load-observer").disabled = !ticket?.session_name;

  if (!ticket) {
    byId("ticket-meta").textContent = "Select a ticket.";
    byId("summary-view").textContent = "";
    byId("output-view").textContent = "";
    byId("observer-output").textContent = 'Observer not loaded yet. Click "Observe".';
    return;
  }

  byId("ticket-meta").textContent = `${ticket.key} · ${ticket.issue_type} · ${ticket.status_name} · mode ${ticket.local_mode} · session ${ticket.session_name || "-"} · prompt ${ticket.prompt || "-"}`;

  byId("summary-view").textContent = [
    ticket.summary,
    "",
    `Issue: ${ticket.key}`,
    `Type: ${ticket.issue_type}`,
    `Status: ${ticket.status_name}`,
    `Mode: ${ticket.local_mode}`,
    `Session: ${ticket.session_name || "-"}`,
    `Prompt: ${ticket.prompt || "-"}`,
  ].join("\n");

  const output = byId("output-view");
  output.textContent = ticket.latest_output || "No local output yet.";
  output.scrollTop = output.scrollHeight;

  for (const el of document.querySelectorAll(".mode")) {
    el.classList.toggle("active", el.dataset.mode === state.mode);
  }
  byId("summary-view").classList.toggle("hidden", state.mode !== "summary");
  byId("output-view").classList.toggle("hidden", state.mode !== "output");
  byId("observer-view").classList.toggle("hidden", state.mode !== "observer");
  byId("live-view").classList.toggle("hidden", state.mode !== "live");

  if (state.mode === "observer") {
    const observeBtn = byId("load-observer");
    observeBtn.textContent = state.observerSession === ticket.session_name ? "Observing" : "Observe";
    const observerOutput = byId("observer-output");
    observerOutput.textContent = state.observerOutputBySession[ticket.session_name] || 'Observer not loaded yet. Click "Observe".';
  }

  if (state.mode === "live") {
    if (ticket?.key && state.liveIssueKey !== ticket.key) {
      connectLive(ticket.key);
    }
    const liveOutput = byId("live-output");
    if (liveOutput && document.activeElement !== liveOutput) {
      liveOutput.focus();
    }
  } else {
    disconnectLive();
  }
}

async function refreshObserver() {
  if (state.mode !== "observer" || !state.observerSession) return;
  try {
    const response = await api("/api/observer", {
      method: "POST",
      body: JSON.stringify({ session_name: state.observerSession, lines: 120 }),
    });
    const previous = state.observerOutputBySession[state.observerSession] || "";
    state.observerOutputBySession[state.observerSession] = response.output || "";
    const output = byId("observer-output");
    if (output) {
      const nearBottom = output.scrollHeight - output.scrollTop - output.clientHeight < 32;
      output.textContent = state.observerOutputBySession[state.observerSession] || "No observed output.";
      if (nearBottom || previous !== state.observerOutputBySession[state.observerSession]) {
        output.scrollTop = output.scrollHeight;
      }
    }
  } catch (error) {
    const output = byId("observer-output");
    if (output) {
      output.textContent = `Observer failed: ${String(error)}`;
    }
  }
}

function connectLive(issueKey) {
  if (state.liveWs && state.liveIssueKey === issueKey) return;
  disconnectLive();

  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  const url = `${protocol}//${location.host}/ws/tickets/${issueKey}/terminal`;
  const ws = new WebSocket(url);
  state.liveWs = ws;
  state.liveIssueKey = issueKey;

  const output = byId("live-output");
  const status = byId("live-status");

  ws.onopen = () => {
    status.textContent = `connected to ${issueKey}`;
    status.className = "live-status connected";
    output.textContent = "Connected. Waiting for output...";
    output.focus();
  };

  ws.onmessage = (event) => {
    const data = JSON.parse(event.data);
    if (data.type === "snapshot" && data.screen) {
      const nearBottom = output.scrollHeight - output.scrollTop - output.clientHeight < 40;
      renderLiveScreen(output, data.screen, data.cursor);
      if (nearBottom) {
        output.scrollTop = output.scrollHeight;
      }
    } else if (data.type === "error") {
      output.innerHTML = `Error: ${escapeHtml(data.message)}`;
    }
  };

  ws.onclose = () => {
    status.textContent = "disconnected";
    status.className = "live-status";
    if (state.liveWs === ws) {
      state.liveWs = null;
      state.liveIssueKey = null;
    }
  };

  ws.onerror = () => {
    status.textContent = "connection error";
    status.className = "live-status";
  };
}

function disconnectLive() {
  if (state.liveWs) {
    state.liveWs.close();
    state.liveWs = null;
    state.liveIssueKey = null;
  }
}

function renderLiveScreen(element, screen, cursor) {
  const lines = screen.split("\n");
  if (!cursor || cursor.y == null || cursor.x == null) {
    element.textContent = screen;
    return;
  }
  const cy = cursor.y;
  const cx = cursor.x;
  const parts = [];
  for (let i = 0; i < lines.length; i++) {
    if (i > 0) parts.push("\n");
    const line = lines[i];
    if (i === cy) {
      const before = line.substring(0, cx);
      const at = line[cx] || " ";
      const after = line.substring(cx + 1);
      parts.push(escapeHtml(before));
      parts.push(`<span class="live-cursor">${escapeHtml(at)}</span>`);
      parts.push(escapeHtml(after));
    } else {
      parts.push(escapeHtml(line));
    }
  }
  element.innerHTML = parts.join("");
}

function setupLiveKeyboard() {
  const output = byId("live-output");

  output.addEventListener("keydown", (event) => {
    if (!state.liveWs || state.liveWs.readyState !== WebSocket.OPEN) return;

    // Let browser handle Cmd+C (copy), Cmd+V (paste), Cmd+A, etc.
    if (event.metaKey || (event.ctrlKey && event.shiftKey)) return;

    event.preventDefault();

    let data = "";

    if (event.ctrlKey) {
      // Ctrl+C, Ctrl+D, Ctrl+Z, etc.
      const code = event.key.toLowerCase().charCodeAt(0) - 96;
      if (code > 0 && code < 27) {
        data = String.fromCharCode(code);
      }
    } else {
      switch (event.key) {
        case "Enter": data = "\r"; break;
        case "Backspace": data = "\x7f"; break;
        case "Tab": data = "\t"; break;
        case "Escape": data = "\x1b"; break;
        case "ArrowUp": data = "\x1b[A"; break;
        case "ArrowDown": data = "\x1b[B"; break;
        case "ArrowRight": data = "\x1b[C"; break;
        case "ArrowLeft": data = "\x1b[D"; break;
        default:
          if (event.key.length === 1) {
            data = event.key;
          }
      }
    }

    if (data) {
      state.liveWs.send(JSON.stringify({ type: "input", data }));
    }
  });

  // Handle paste
  output.addEventListener("paste", (event) => {
    if (!state.liveWs || state.liveWs.readyState !== WebSocket.OPEN) return;
    event.preventDefault();
    const text = event.clipboardData.getData("text");
    if (text) {
      state.liveWs.send(JSON.stringify({ type: "input", data: text }));
    }
  });
}

function bind() {
  byId("refresh-now").addEventListener("click", refresh);
  byId("heartbeat-now").addEventListener("click", async () => {
    await api("/api/heartbeat", { method: "POST" });
    await refresh();
  });
  byId("open-iterm").addEventListener("click", async () => {
    const ticket = selectedTicket();
    if (!ticket?.session_name) return;
    await api(`/api/tickets/${ticket.key}/open`, { method: "POST" });
  });
  byId("run-now").addEventListener("click", async () => {
    const ticket = selectedTicket();
    if (!ticket) return;
    await api(`/api/tickets/${ticket.key}/run-now`, { method: "POST" });
    await refresh();
  });
  byId("load-observer").addEventListener("click", async () => {
    const ticket = selectedTicket();
    if (!ticket?.session_name) return;
    state.mode = "observer";
    state.observerSession = ticket.session_name;
    renderSelectedTicket();
    await refreshObserver();
  });
  document.querySelectorAll(".mode").forEach((node) => {
    node.addEventListener("click", async () => {
      state.mode = node.dataset.mode;
      renderSelectedTicket();
      if (state.mode === "observer") {
        const ticket = selectedTicket();
        if (ticket?.session_name) {
          state.observerSession = ticket.session_name;
          await refreshObserver();
        }
      }
      if (state.mode === "live") {
        const ticket = selectedTicket();
        if (ticket?.key) {
          connectLive(ticket.key);
        }
      }
    });
  });
  byId("observer-input-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const ticket = selectedTicket();
    const input = byId("observer-input");
    const text = input.value;
    if (!ticket?.session_name || !text.trim()) return;
    state.observerSession = ticket.session_name;
    await api("/api/observer/input", {
      method: "POST",
      body: JSON.stringify({ session_name: ticket.session_name, text, press_enter: true }),
    });
    input.value = "";
    await refreshObserver();
  });
  byId("debug-plain-shell").addEventListener("click", () => {
    byId("phase-note").textContent = "Plain ttyd shell debug is disabled in observer mode.";
  });
  byId("debug-tmux-shell").addEventListener("click", async () => {
    const response = await api("/api/debug/ttyd/tmux-shell", { method: "POST" });
    byId("phase-note").textContent = `tmux shell ready at ${response.url} (observer mode does not embed it).`;
  });
}

function timeFrom(iso) {
  if (!iso) return "-";
  const ms = new Date(iso).getTime() - Date.now();
  const sec = Math.max(0, Math.floor(ms / 1000));
  return `${String(Math.floor(sec / 60)).padStart(2, "0")}:${String(sec % 60).padStart(2, "0")}`;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

bind();
setupLiveKeyboard();
refresh();
setInterval(refresh, 10000);
setInterval(refreshObserver, 2000);
