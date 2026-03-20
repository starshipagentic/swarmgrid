const state = {
  snapshot: null,
  selectedKey: null,
  page: normalizePage(window.location.pathname),
  outputPinned: true,
  outputStateByKey: {},
  // Terminal state
  terminalMode: "output", // live | observer | output
  liveWs: null,
  liveIssueKey: null,
  liveScratchSession: null, // scratch session name for live terminal
  observerSession: null,
  observerOutputBySession: {},
  // Scratch terminal state
  scratchSession: null, // currently selected scratch session name
  // Countdown timer state
  countdownSeconds: null, // seconds until next heartbeat
  heartbeatRunning: false, // true while heartbeat API is in flight
  pulseStripBuilt: false, // true once the pulse-strip DOM has been built
  intervalEditing: false, // true when the interval input is visible
  // Timeline state — cached per issue key, NOT refetched on every 3-sec cycle
  timelineByKey: {}, // { [issueKey]: { transitions: [...], fetchedAt: timestamp } }
  timelineCollapsed: true, // whether the timeline section is collapsed
  // Search state
  searchQuery: "",
  searchResults: null, // null = no search, [] = no results, [...] = results
  searchDebounce: null,
  searchSelectedTicket: null, // holds search result data for non-board tickets
  // Board switcher state
  boards: null, // cached board list from /api/boards
  addBoardVisible: false, // whether the add-board form is open
};

const byId = (id) => document.getElementById(id);

function normalizePage(pathname) {
  if (pathname === "/" || pathname === "") return "board";
  const page = pathname.replace(/^\/+/, "");
  return ["board", "routes", "setup", "sharing", "team"].includes(page) ? page : "board";
}

async function api(path, options = {}) {
  const method = options.method || "GET";
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    cache: method === "GET" ? "no-store" : "default",
    ...options,
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || `${response.status} ${response.statusText}`);
  }
  return response.json();
}

function selectedTicket() {
  return state.snapshot?.columns.flatMap((col) => col.tickets).find((item) => item.key === state.selectedKey) || null;
}

function selectedSearchTicket() {
  if (!state.searchSelectedTicket) return null;
  if (state.selectedKey !== state.searchSelectedTicket.key) return null;
  return state.searchSelectedTicket;
}

function setPage(page, replace = false) {
  state.page = page;
  const target = page === "board" ? "/board" : `/${page}`;
  if (window.location.pathname !== target) {
    if (replace) {
      history.replaceState({ page }, "", target);
    } else {
      history.pushState({ page }, "", target);
    }
  }
  renderPage();
}

function renderPage() {
  for (const page of ["board", "routes", "setup", "sharing", "team"]) {
    byId(`view-${page}`)?.classList.toggle("hidden", state.page !== page);
    document.querySelector(`[data-page="${page}"]`)?.classList.toggle("active", state.page === page);
  }
  if (state.page !== "board") {
    disconnectLive();
  }
  if (state.page === "team") {
    renderTeamPage();
  }
}

async function refresh() {
  try {
  state.snapshot = await api("/api/snapshot");
  const boardTicket = selectedTicket();
  const searchTicket = selectedSearchTicket();
  if (boardTicket && searchTicket && boardTicket.key === searchTicket.key) {
    state.searchSelectedTicket = null;
  }
  if (!boardTicket && !selectedSearchTicket() && !state.scratchSession) {
    const first = state.snapshot.columns.flatMap((col) => col.tickets)[0];
    state.selectedKey = first?.key || null;
  }
  recalcCountdown();
  render();
  } catch (e) {
    console.error("refresh failed:", e);
  }
}

function recalcCountdown() {
  const snap = state.snapshot;
  if (!snap) return;
  const intervalSec = (snap.config.poll_interval_minutes || 5) * 60;
  const dagster = snap.dagster || {};
  if (dagster.active && dagster.sentinel_age_seconds != null) {
    // Dagster driving: next tick = interval - age of sentinel
    state.countdownSeconds = Math.max(0, Math.round(intervalSec - dagster.sentinel_age_seconds));
  } else if (snap.controller.next_run_at) {
    // Web server driving: use next_run_at
    const ms = new Date(snap.controller.next_run_at).getTime() - Date.now();
    state.countdownSeconds = Math.max(0, Math.round(ms / 1000));
  } else {
    state.countdownSeconds = null;
  }
}

// --- Board switcher ---

async function fetchBoards() {
  try {
    const data = await api("/api/boards");
    state.boards = data.boards || [];
  } catch (e) {
    state.boards = [];
  }
  renderBoardSwitcher();
}

function renderBoardSwitcher() {
  const sel = byId("board-switcher");
  if (!sel || !state.boards) return;

  const boards = state.boards;
  // Hide switcher if only one board and add-board form is not visible
  if (boards.length <= 1 && !state.addBoardVisible) {
    sel.style.display = boards.length === 0 ? "none" : "";
  } else {
    sel.style.display = "";
  }

  const activeIndex = boards.findIndex((b) => b.active);
  let html = boards.map((b) => {
    const label = `${b.project_key || b.name}${b.board_id ? ` (board ${b.board_id})` : ""}`;
    return `<option value="${b.index}"${b.active ? " selected" : ""}>${escapeHtml(label)}</option>`;
  }).join("");
  html += `<option value="__add__">+ Add Board...</option>`;
  sel.innerHTML = html;
}

function showAddBoardForm() {
  state.addBoardVisible = true;
  // Remove existing form if any
  let existing = document.querySelector(".add-board-overlay");
  if (existing) existing.remove();

  const overlay = document.createElement("div");
  overlay.className = "add-board-overlay";
  overlay.innerHTML = `
    <div class="add-board-form">
      <h3>Add Board</h3>
      <p style="color:var(--muted);font-size:13px;margin:0 0 12px">Paste your Jira board URL and the fields fill automatically.</p>
      <label for="ab-board-url">Board URL</label>
      <input id="ab-board-url" type="text" placeholder="https://myteam.atlassian.net/jira/software/projects/PROJ/boards/1234" />
      <div style="border-top:1px solid var(--border);margin:12px 0 8px;padding-top:8px">
        <p style="color:var(--muted);font-size:12px;margin:0 0 8px">Or fill in manually:</p>
        <label for="ab-site-url">Site URL <span style="color:var(--muted);font-size:11px">e.g. https://myteam.atlassian.net</span></label>
        <input id="ab-site-url" type="text" placeholder="https://yourorg.atlassian.net" />
        <label for="ab-project-key">Project Key <span style="color:var(--muted);font-size:11px">the short code in ticket IDs, like PROJ in PROJ-123</span></label>
        <input id="ab-project-key" type="text" placeholder="PROJ" />
        <label for="ab-board-id">Board ID <span style="color:var(--muted);font-size:11px">the number at the end of the board URL</span></label>
        <input id="ab-board-id" type="text" placeholder="1234" />
        <label for="ab-workdir">Working directory <span style="color:var(--muted);font-size:11px">where Claude sessions run for this board</span></label>
        <input id="ab-workdir" type="text" placeholder="/path/to/your/project" />
      </div>
      <div class="add-board-actions">
        <button id="ab-submit" class="route-save">Create Board</button>
        <button id="ab-cancel">Cancel</button>
      </div>
      <p id="ab-error" class="add-board-error"></p>
    </div>
  `;
  document.body.appendChild(overlay);

  // Pre-fill site_url from current board if available
  if (state.snapshot?.config?.site_url) {
    document.getElementById("ab-site-url").value = state.snapshot.config.site_url;
  }

  // Parse board URL on paste/input — auto-fill the 3 fields
  document.getElementById("ab-board-url").addEventListener("input", (e) => {
    const url = e.target.value.trim();
    const m = url.match(/^(https?:\/\/[^/]+)\/.*\/projects\/([A-Z0-9]+)\/boards?\/(\d+)/i);
    if (m) {
      document.getElementById("ab-site-url").value = m[1];
      document.getElementById("ab-project-key").value = m[2].toUpperCase();
      document.getElementById("ab-board-id").value = m[3];
    } else {
      const m2 = url.match(/^(https?:\/\/[^/]+)\/.*\/projects\/([A-Z0-9]+)/i);
      if (m2) {
        document.getElementById("ab-site-url").value = m2[1];
        document.getElementById("ab-project-key").value = m2[2].toUpperCase();
      }
    }
  });

  overlay.addEventListener("click", (e) => {
    if (e.target === overlay) closeAddBoardForm();
  });

  document.getElementById("ab-cancel").addEventListener("click", closeAddBoardForm);

  document.getElementById("ab-submit").addEventListener("click", async () => {
    const siteUrl = document.getElementById("ab-site-url").value.trim();
    const projectKey = document.getElementById("ab-project-key").value.trim();
    const boardId = document.getElementById("ab-board-id").value.trim();
    const workdir = document.getElementById("ab-workdir").value.trim();
    const errEl = document.getElementById("ab-error");

    if (!siteUrl || !projectKey || !boardId) {
      errEl.textContent = "Site URL, Project Key, and Board ID are required.";
      return;
    }

    const btn = document.getElementById("ab-submit");
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-inline"></span> Creating...';
    errEl.textContent = "";

    try {
      const result = await api("/api/boards", {
        method: "POST",
        body: JSON.stringify({ site_url: siteUrl, project_key: projectKey, board_id: boardId, working_dir: workdir || undefined }),
      });
      closeAddBoardForm();

      // Refresh board list and switch to the new board
      await fetchBoards();
      if (result.index != null) {
        await api(`/api/boards/${result.index}/switch`, { method: "POST" });
        // Reset UI state for new board
        routesBuilt = false;
        columnsCache = null;
        setupFormBuilt = false;
        await fetchBoards();
        await refresh();
      }
    } catch (e) {
      errEl.textContent = String(e).replace("Error: ", "");
      btn.disabled = false;
      btn.textContent = "Create Board";
    }
  });

  // Focus first empty field
  const firstInput = document.getElementById("ab-site-url");
  if (firstInput && !firstInput.value) {
    firstInput.focus();
  } else {
    document.getElementById("ab-project-key").focus();
  }
}

function closeAddBoardForm() {
  state.addBoardVisible = false;
  const overlay = document.querySelector(".add-board-overlay");
  if (overlay) overlay.remove();
  // Reset switcher to active board
  renderBoardSwitcher();
}

function render() {
  try {
    const snapshot = state.snapshot;
    if (!snapshot) return;

    byId("headline").textContent = `${snapshot.config.project_key} · board ${snapshot.config.board_id || "-"} · workdir ${snapshot.config.workdir || "-"}`;

    // Build pulse-strip DOM only once; after that, update in-place
    if (!state.pulseStripBuilt) {
      buildPulseStrip();
      state.pulseStripBuilt = true;
    }
    updatePulseStrip();

    byId("board-stats").innerHTML = `
      <span class="chip"><strong>live</strong> ${snapshot.counts.live_rows}</span>
      <span class="chip"><strong>active</strong> ${snapshot.counts.active_rows}/${snapshot.config.max_parallel}</span>
    `;
    byId("board-meta").textContent = snapshot.counts.capped_not_shown
      ? `showing first ${snapshot.counts.visible_rows} tickets · ${snapshot.counts.capped_not_shown} not shown`
      : `showing ${snapshot.counts.visible_rows} tickets`;

    byId("legend").innerHTML = Object.entries(snapshot.session_legend)
      .map(([name, icon]) => `<span class="legend-item"><strong>${icon}</strong> ${name}</span>`)
      .join("");

    renderColumns(snapshot.columns);
    renderDetail();
    renderRoutes();
    renderSetupForm();
    if (state.page === "sharing") renderSharingPage();
    renderPage();

    // Flag Setup tab if something needs attention
    const setupLink = document.querySelector('[data-page="setup"]');
    if (setupLink) {
      const health = snapshot.health || {};
      const needsAttention = health.working_dir_valid === false || !health.jira_connected;
      setupLink.textContent = needsAttention ? "Setup !" : "Setup";
      setupLink.classList.toggle("nav-warn", needsAttention);
    }
  } catch (e) {
    console.error("render failed:", e);
  }
}

// --- Pulse strip (topbar status) ---

function buildPulseStrip() {
  const strip = byId("pulse-strip");
  strip.innerHTML = `
    <span id="ps-dot" class="ps-dot" title="Health indicator"></span>
    <span id="ps-countdown" class="ps-countdown"></span>
    <span id="ps-interval" class="ps-interval" title="Click to change interval"></span>
    <button id="ps-heartbeat" class="ps-heartbeat" title="Trigger heartbeat now"></button>
  `;

  // Heartbeat button
  byId("ps-heartbeat").addEventListener("click", async () => {
    if (state.heartbeatRunning) return;
    state.heartbeatRunning = true;
    updatePulseStrip();
    try {
      await api("/api/heartbeat", { method: "POST" });
      await refresh();
    } catch (e) {
      // ignore, refresh on next cycle
    } finally {
      state.heartbeatRunning = false;
      updatePulseStrip();
    }
  });

  // Interval: click to edit
  byId("ps-interval").addEventListener("click", () => {
    if (state.intervalEditing) return;
    startIntervalEdit();
  });
}

function updatePulseStrip() {
  const snap = state.snapshot;
  if (!snap) return;

  // Dot color
  const dot = byId("ps-dot");
  const dagsterActive = snap.dagster?.active;
  dot.textContent = "\u25CF"; // filled circle
  dot.className = dagsterActive ? "ps-dot ps-dot-green" : "ps-dot ps-dot-gray";

  // Countdown text
  const cdEl = byId("ps-countdown");
  if (state.countdownSeconds != null && state.countdownSeconds > 0) {
    const min = Math.floor(state.countdownSeconds / 60);
    const sec = String(state.countdownSeconds % 60).padStart(2, "0");
    cdEl.textContent = `${min}:${sec}`;
  } else if (state.countdownSeconds === 0) {
    cdEl.textContent = "now\u2026";
  } else {
    cdEl.textContent = "--:--";
  }

  // Interval label (only update if not editing)
  if (!state.intervalEditing) {
    const intEl = byId("ps-interval");
    intEl.textContent = `every ${snap.config.poll_interval_minutes}m`;
  }

  // Heartbeat button
  const hbBtn = byId("ps-heartbeat");
  if (state.heartbeatRunning) {
    hbBtn.textContent = "running\u2026";
    hbBtn.disabled = true;
  } else {
    hbBtn.innerHTML = "\u25B6 Heartbeat";
    hbBtn.disabled = false;
  }
}

function startIntervalEdit() {
  state.intervalEditing = true;
  const intEl = byId("ps-interval");
  const current = state.snapshot?.config?.poll_interval_minutes || 5;
  intEl.innerHTML = `every <input id="ps-interval-input" type="number" min="1" max="60" value="${current}" />m`;
  const input = byId("ps-interval-input");
  input.focus();
  input.select();

  const commit = async () => {
    const val = Math.max(1, Math.min(60, parseInt(input.value, 10) || current));
    state.intervalEditing = false;
    intEl.textContent = `every ${val}m`;
    if (val !== current) {
      try {
        await api("/api/setup", {
          method: "POST",
          body: JSON.stringify({ poll_interval_minutes: val }),
        });
        await refresh();
      } catch (e) {
        // revert display on failure
        intEl.textContent = `every ${current}m`;
      }
    }
  };

  const cancel = () => {
    state.intervalEditing = false;
    intEl.textContent = `every ${current}m`;
  };

  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); commit(); }
    if (e.key === "Escape") { e.preventDefault(); cancel(); }
  });
  input.addEventListener("blur", () => {
    // Small delay so keydown can fire first
    setTimeout(() => { if (state.intervalEditing) commit(); }, 100);
  });
}

function renderColumns(columns) {
  const root = byId("columns");

  // Save scroll positions before rebuilding
  const rootScrollLeft = root.scrollLeft;
  const columnScrollTops = {};
  root.querySelectorAll(".column").forEach((col) => {
    const status = col.dataset.status;
    if (status) columnScrollTops[status] = col.scrollTop;
  });

  root.innerHTML = "";
  for (const column of columns) {
    const node = document.createElement("section");
    node.className = "column";
    node.dataset.status = column.status;
    const tickets = column.tickets.length
      ? `<div class="tickets">${column.tickets.map(renderTicket).join("")}</div>`
      : `<p class="meta">No tickets</p>`;
    node.innerHTML = `<h3>${column.status} (${column.count})</h3>${tickets}`;
    root.appendChild(node);
  }

  // Render scratch column if present
  const scratchCol = state.snapshot?.scratch_column;
  if (scratchCol) {
    const node = document.createElement("section");
    node.className = "column scratch-column";
    node.dataset.status = "Scratch";
    const cards = scratchCol.sessions.map((s) => {
      const selected = state.scratchSession === s.session_name && !state.selectedKey ? " selected" : "";
      const modeClass = s.mode ? ` ${s.mode}` : "";
      const marker = markerFor(s.mode);
      const idleTxt = s.idle_seconds < 60 ? `${s.idle_seconds}s` : `${Math.floor(s.idle_seconds / 60)}m`;
      return `
        <article class="ticket scratch-card${selected}${modeClass}" data-scratch="${escapeHtml(s.session_name)}">
          <div class="key">${escapeHtml(s.session_name)}${marker ? ` ${marker}` : ""}</div>
          <div class="meta">scratch · ${s.mode} · ${idleTxt} ago</div>
        </article>
      `;
    }).join("");
    node.innerHTML = `<h3>Scratch (${scratchCol.count})</h3><div class="tickets">${cards}</div>`;
    root.appendChild(node);
  }

  // Restore scroll positions
  root.scrollLeft = rootScrollLeft;
  root.querySelectorAll(".column").forEach((col) => {
    const saved = columnScrollTops[col.dataset.status];
    if (saved) col.scrollTop = saved;
  });
  root.querySelectorAll(".ticket:not(.scratch-card)").forEach((node) => {
    node.addEventListener("click", () => {
      const newKey = node.dataset.key;
      if (newKey !== state.selectedKey) {
        state.selectedKey = newKey;
        state.scratchSession = null;
        state.observerSession = null;
        disconnectLive();
        const ticket = selectedTicket();
        state.terminalMode = ticket?.session_name ? "live" : "output";
      }
      renderColumns(state.snapshot.columns);
      renderDetail();
    });
    node.addEventListener("dblclick", async () => {
      const key = node.dataset.key;
      state.selectedKey = key;
      state.scratchSession = null;
      renderColumns(state.snapshot.columns);
      const ticket = selectedTicket();
      if (ticket?.session_name) {
        // Already has a session — just go live
        state.terminalMode = "live";
        renderDetail();
      } else {
        // No session — launch immediately
        renderDetail();
        try {
          await api(`/api/tickets/${key}/run-now`, { method: "POST" });
          await refresh();
        } catch (e) {
          console.error("Quick launch failed:", e);
        }
      }
    });
  });
  root.querySelectorAll(".scratch-card").forEach((node) => {
    node.addEventListener("click", () => {
      const name = node.dataset.scratch;
      state.selectedKey = null;
      state.searchSelectedTicket = null;
      state.scratchSession = name;
      state.terminalMode = "live";
      disconnectLive();
      renderColumns(state.snapshot.columns);
      renderDetail();
    });
  });
}

// --- Search ---

function handleSearch(query) {
  state.searchQuery = query;
  if (state.searchDebounce) clearTimeout(state.searchDebounce);

  if (!query.trim()) {
    state.searchResults = null;
    renderSearchResults();
    return;
  }

  state.searchDebounce = setTimeout(async () => {
    try {
      const data = await api(`/api/search?q=${encodeURIComponent(query.trim())}`);
      state.searchResults = data.results || [];
    } catch (e) {
      state.searchResults = [];
    }
    renderSearchResults();
  }, 300);
}

function renderSearchResults() {
  const container = byId("search-results");
  if (!container) return;

  if (!state.searchResults || state.searchResults.length === 0) {
    if (state.searchQuery.trim() && state.searchResults !== null) {
      container.classList.remove("hidden");
      container.innerHTML = `
        <div class="search-results-header">
          <h3>Search Results</h3>
          <button class="search-results-close" id="search-close">Clear</button>
        </div>
        <p style="color:var(--muted);font-size:12px;margin:4px 0">No results for "${escapeHtml(state.searchQuery)}"</p>
      `;
      byId("search-close").addEventListener("click", clearSearch);
    } else {
      container.classList.add("hidden");
      container.innerHTML = "";
    }
    return;
  }

  container.classList.remove("hidden");
  let lastHadTmux = null;
  const items = state.searchResults.map((r) => {
    const modeMarker = markerFor(r.local_mode);
    const dimClass = r.has_tmux ? "" : " sr-no-tmux";
    // Insert separator between tmux and non-tmux results
    let separator = "";
    if (lastHadTmux === true && !r.has_tmux) {
      separator = `<div class="sr-separator">Other Jira tickets</div>`;
    }
    lastHadTmux = r.has_tmux;
    return `${separator}
      <div class="search-result-item${dimClass}" data-key="${escapeHtml(r.key)}">
        <span class="sr-key">${escapeHtml(r.key)}${modeMarker ? ` ${modeMarker}` : ""}</span>
        <span class="sr-status">${escapeHtml(r.status_name || "-")}</span>
        <span class="sr-mode">${escapeHtml(r.local_mode || "-")}</span>
        <span class="sr-summary">${escapeHtml(r.summary || "")}</span>
      </div>
    `;
  }).join("");

  container.innerHTML = `
    <div class="search-results-header">
      <h3>Search Results (${state.searchResults.length})</h3>
      <button class="search-results-close" id="search-close">Clear</button>
    </div>
    ${items}
  `;

  byId("search-close").addEventListener("click", clearSearch);

  container.querySelectorAll(".search-result-item").forEach((el) => {
    el.addEventListener("click", () => {
      const key = el.dataset.key;
      const searchResult = state.searchResults?.find((r) => r.key === key) || null;
      if (!searchResult) return;
      state.selectedKey = key;
      state.scratchSession = null;
      state.observerSession = null;
      disconnectLive();
      // Copy the clicked result so async search refreshes cannot mutate the active detail source.
      state.searchSelectedTicket = { ...searchResult };
      const ticket = selectedTicket();
      state.terminalMode = ticket?.session_name || searchResult.session_name ? "live" : "output";
      // Highlight selected result but keep search open
      container.querySelectorAll(".search-result-item").forEach((r) => r.classList.remove("selected"));
      el.classList.add("selected");
      render();
    });
  });
}

function clearSearch() {
  state.searchQuery = "";
  state.searchResults = null;
  state.searchSelectedTicket = null;
  const input = byId("board-search");
  if (input) input.value = "";
  renderSearchResults();
}

function renderTicket(ticket) {
  const selected = ticket.key === state.selectedKey ? " selected" : "";
  const mode = ticket.local_mode && ticket.local_mode !== "none" ? ` ${ticket.local_mode}` : "";
  const marker = markerFor(ticket.local_mode);
  let shareIcon = "";
  if (ticket.shared) {
    const n = ticket.share_clients || 0;
    const active = ticket.local_mode === "active";
    const dots = n >= 2 ? "◉◉◉" : n === 1 ? "◉◉" : "◉";
    const cls = "share-dot" + (active ? " typing" : n > 0 ? " connected" : "");
    const parts = [n > 0 ? `${n} viewer${n > 1 ? "s" : ""}` : "no viewers", active ? "active" : "idle"];
    shareIcon = ` <span class="${cls}" title="Shared · ${parts.join(" · ")}">${dots}</span>`;
  }
  return `
    <article class="ticket${selected}${mode}" data-key="${ticket.key}">
      <div class="key">${ticket.key}${marker ? ` ${marker}` : ""}${shareIcon}</div>
      <div class="meta">${ticket.issue_type} · ${ticket.status_name}</div>
      <div class="summary">${escapeHtml(ticket.summary)}</div>
    </article>
  `;
}

function renderSearchDetail(panel, result) {
  const sigKey = `search|${result.key}`;
  if (panel.dataset.signature === sigKey) return; // already showing this

  disconnectLive();
  const modeMarker = markerFor(result.local_mode);
  const hasTmux = result.has_tmux;
  const modeLabel = result.local_mode && result.local_mode !== "none" ? result.local_mode : "no session";

  let actionArea = "";
  if (hasTmux) {
    actionArea = `<p style="color:var(--muted);margin:12px 0">Ticket has an archived tmux session. It may appear on the board after a heartbeat.</p>`;
  } else {
    actionArea = `
      <p style="color:var(--muted);margin:12px 0">No terminal session for this ticket.</p>
      <button id="search-launch-btn" class="launch-btn">Create tmux Session</button>
    `;
  }

  panel.innerHTML = `
    <div class="ticket-headline">
      <h2>${escapeHtml(result.summary || result.key)}</h2>
    </div>
    <div class="ticket-meta-strip">
      <span><strong>${escapeHtml(result.key)}</strong>${modeMarker ? ` ${modeMarker}` : ""}</span>
      <span>${escapeHtml(result.issue_type || "")}</span>
      <span>${escapeHtml(result.status_name || "")}</span>
      <span><strong>Mode:</strong> ${escapeHtml(modeLabel)}</span>
    </div>
    ${actionArea}
  `;
  panel.dataset.key = result.key;
  panel.dataset.signature = sigKey;

  const launchBtn = byId("search-launch-btn");
  if (launchBtn) {
    launchBtn.addEventListener("click", async () => {
      launchBtn.disabled = true;
      launchBtn.innerHTML = '<span class="spinner-inline"></span> Launching...';
      try {
        const data = await api(`/api/tickets/${result.key}/run-now`, { method: "POST" });
        state.searchSelectedTicket = {
          ...result,
          has_tmux: true,
          local_mode: "idle",
          session_name: data?.launch?.session_name || result.session_name || null,
        };
        state.terminalMode = "live";
        await refresh();
      } catch (e) {
        const msg = String(e).replace("Error: ", "");
        launchBtn.insertAdjacentHTML("afterend", `<p style="color:var(--danger,red);font-size:12px;margin:6px 0">${escapeHtml(msg)}</p>`);
        launchBtn.disabled = false;
        launchBtn.textContent = "Create tmux Session";
      }
    });
  }
}

function renderScratchDetail(panel, sessionName) {
  const sigKey = `scratch|${sessionName}|${state.terminalMode}`;
  if (panel.dataset.signature === sigKey) {
    // Already showing — just update live connection if needed
    if (state.terminalMode === "live" && state.liveScratchSession !== sessionName) {
      connectScratchLive(sessionName);
    }
    if (state.terminalMode === "observer" && state.observerSession !== sessionName) {
      state.observerSession = sessionName;
      refreshObserver();
    }
    return;
  }

  disconnectLive();

  const modeTabs = `<div class="terminal-modes">
    <button class="mode-tab${state.terminalMode === "live" ? " active" : ""}" data-tmode="live">Live</button>
    <button class="mode-tab${state.terminalMode === "observer" ? " active" : ""}" data-tmode="observer">Observer</button>
    <button class="mode-tab${state.terminalMode === "output" ? " active" : ""}" data-tmode="output">Output</button>
  </div>`;

  let terminalArea = "";
  if (state.terminalMode === "live") {
    terminalArea = `
      <div class="terminal-shell">
        <pre id="live-output" class="live-output" tabindex="0"><span class="spinner-inline"></span> Connecting...</pre>
      </div>`;
  } else if (state.terminalMode === "observer") {
    terminalArea = `
      <div class="terminal-shell">
        <pre id="observer-output" class="observer-output"><span class="spinner-inline"></span> Loading observer...</pre>
        <form id="observer-input-form" class="observer-input-form">
          <input id="observer-input" type="text" placeholder="Send text to session..." autocomplete="off" />
          <button type="submit">Send</button>
        </form>
      </div>`;
  } else {
    terminalArea = `<pre class="output">Scratch terminal — use Live or Observer mode to interact.</pre>`;
  }

  panel.innerHTML = `
    <div class="ticket-headline">
      <h2>Scratch Terminal</h2>
      <div class="detail-actions">
        <button data-action="attach-ticket">Attach to Ticket</button>
        <button data-action="kill-scratch">Kill Session</button>
      </div>
    </div>
    <div class="ticket-meta-strip">
      <span><strong>Session:</strong> ${escapeHtml(sessionName)}</span>
      <span class="scratch-badge">scratch</span>
    </div>
    <div class="terminal-toolbar">
      ${modeTabs}
    </div>
    ${terminalArea}
  `;
  panel.dataset.key = "";
  panel.dataset.signature = sigKey;

  // Bind action buttons
  panel.querySelectorAll("[data-action]").forEach((button) => {
    button.addEventListener("click", async () => {
      const action = button.dataset.action;
      if (action === "kill-scratch") {
        try {
          await api(`/api/scratch-terminals/${encodeURIComponent(sessionName)}`, { method: "DELETE" });
          state.scratchSession = null;
          disconnectLive();
          await refresh();
        } catch (error) {
          alert(String(error));
        }
      } else if (action === "attach-ticket") {
        const projectKey = state.snapshot?.config?.project_key || "PROJ";
        const issueKey = prompt(`Enter issue key (e.g. ${projectKey}-123):`);
        if (!issueKey || !issueKey.trim()) return;
        button.disabled = true;
        button.innerHTML = '<span class="spinner-inline"></span> Attaching...';
        try {
          const result = await api(`/api/scratch-terminals/${encodeURIComponent(sessionName)}/attach`, {
            method: "POST",
            body: JSON.stringify({ issue_key: issueKey.trim() }),
          });
          state.scratchSession = null;
          state.selectedKey = result.issue_key;
          state.terminalMode = "live";
          disconnectLive();
          await refresh();
        } catch (error) {
          button.disabled = false;
          button.textContent = "Attach to Ticket";
          alert(String(error));
        }
      }
    });
  });

  // Bind terminal mode tabs
  panel.querySelectorAll(".mode-tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      const newMode = tab.dataset.tmode;
      if (newMode === state.terminalMode) return;
      disconnectLive();
      state.terminalMode = newMode;
      renderScratchDetail(panel, sessionName);
    });
  });

  // Bind observer form
  const observerForm = byId("observer-input-form");
  if (observerForm) {
    observerForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const input = byId("observer-input");
      const text = input.value;
      if (!text.trim()) return;
      await api("/api/observer/input", {
        method: "POST",
        body: JSON.stringify({ session_name: sessionName, text, press_enter: true }),
      });
      input.value = "";
      await refreshObserver();
    });
  }

  // Set up live keyboard handler
  const liveOutput = byId("live-output");
  if (liveOutput) {
    setupLiveKeyboard(liveOutput);
  }

  // Connect live or observer
  if (state.terminalMode === "live") {
    connectScratchLive(sessionName);
  } else if (state.terminalMode === "observer") {
    state.observerSession = sessionName;
    refreshObserver();
  }
}

function renderDetail() {
  const searchTicket = selectedSearchTicket();
  const ticket = selectedTicket();
  const panel = byId("ticket-detail");

  // Scratch terminal detail view
  if (state.scratchSession && !state.selectedKey) {
    renderScratchDetail(panel, state.scratchSession);
    return;
  }

  // Fallback: if ticket not on board but we have search result data, show search detail view
  if (!ticket && searchTicket) {
    renderSearchDetail(panel, searchTicket);
    return;
  }

  if (!ticket) {
    panel.innerHTML = `<h2>Selected Ticket</h2><p>Select a ticket.</p>`;
    panel.dataset.key = "";
    panel.dataset.signature = "";
    disconnectLive();
    return;
  }

  // Clear search selection once we have a real board ticket
  if (searchTicket && ticket.key === searchTicket.key) state.searchSelectedTicket = null;

  const hasSession = Boolean(ticket.session_name);

  // If ticket changed, auto-select mode
  if (panel.dataset.key !== ticket.key) {
    state.terminalMode = hasSession ? "live" : "output";
  }

  const actions = [];
  const sharingAvailable = state.snapshot?.sharing_available;
  if (hasSession) {
    actions.push(`<button data-action="open">Open iTerm</button>`);
    if (sharingAvailable) {
      if (ticket.shared) {
        actions.push(`<button data-action="unshare" class="shared-active" title="${escapeHtml(ticket.ssh_connect || "")}">Sharing ●</button>`);
      } else {
        actions.push(`<button data-action="share">Share</button>`);
      }
    }
    actions.push(`<button data-action="kill">Kill Session</button>`);
  } else {
    actions.push(`<button data-action="run">Run Now</button>`);
  }

  const previousOutputState = state.outputStateByKey[ticket.key] || {
    text: "",
    scrollTop: 0,
    distanceFromBottom: 0,
    pinned: true,
  };
  const nextOutputText = ticket.latest_output || "No local output yet.";

  // Only rebuild DOM when the structural identity changes (ticket, session, mode switch)
  // NOT when volatile fields like local_mode/status change — those update in-place
  const structuralSignature = `${ticket.key}|${ticket.session_name || ""}|${hasSession ? "session" : "run"}|${state.terminalMode}|${ticket.shared ? "shared" : ""}`;
  const needsFullRender =
    panel.dataset.key !== ticket.key || panel.dataset.signature !== structuralSignature;

  if (needsFullRender) {
    // Build terminal mode tabs (only show if session exists)
    const modeTabs = hasSession
      ? `<div class="terminal-modes">
           <button class="mode-tab${state.terminalMode === "live" ? " active" : ""}" data-tmode="live">Live</button>
           <button class="mode-tab${state.terminalMode === "observer" ? " active" : ""}" data-tmode="observer">Observer</button>
           <button class="mode-tab${state.terminalMode === "output" ? " active" : ""}" data-tmode="output">Output</button>
         </div>`
      : "";

    // Build the terminal/output area based on current mode
    let terminalArea = "";
    if (state.terminalMode === "live" && hasSession) {
      terminalArea = `
        <div class="terminal-shell">
          <pre id="live-output" class="live-output" tabindex="0"><span class="spinner-inline"></span> Connecting...</pre>
        </div>`;
    } else if (state.terminalMode === "observer" && hasSession) {
      terminalArea = `
        <div class="terminal-shell">
          <pre id="observer-output" class="observer-output"><span class="spinner-inline"></span> Loading observer...</pre>
          <form id="observer-input-form" class="observer-input-form">
            <input id="observer-input" type="text" placeholder="Send text to session..." autocomplete="off" />
            <button type="submit">Send</button>
          </form>
        </div>`;
    } else {
      terminalArea = `<pre class="output">${escapeHtml(nextOutputText)}</pre>`;
    }

    panel.innerHTML = `
      <div class="ticket-headline">
        <h2>${escapeHtml(ticket.summary)}</h2>
        <div class="detail-actions">
          ${actions.join("")}
        </div>
      </div>
      <div class="ticket-meta-strip">
        <span><strong>${ticket.key}</strong> · ${ticket.issue_type} · ${ticket.status_name}</span>
        <span><strong>Mode:</strong> ${ticket.local_mode}</span>
        <span><strong>Session:</strong> ${escapeHtml(ticket.session_name || "-")}</span>
        <span><strong>Prompt:</strong> ${escapeHtml(ticket.prompt || "-")}</span>
      </div>
      <div class="terminal-toolbar">
        ${modeTabs}
        ${ticket.shared && ticket.ssh_connect ? `<div class="share-connect-inline" id="share-connect-bar">
          <code id="ssh-connect-text">${escapeHtml(ticket.ssh_connect)}</code>
          <button id="copy-ssh-btn" class="copy-btn">Copy</button>
        </div>` : ""}
      </div>
      ${terminalArea}
      <div class="timeline-section">
        <div class="timeline-header" id="timeline-toggle">Timeline <span class="timeline-arrow">${state.timelineCollapsed ? "\u25B6" : "\u25BC"}</span></div>
        <div class="timeline-body${state.timelineCollapsed ? " hidden" : ""}" id="timeline-body">
          <div class="timeline-content" id="timeline-content"><span class="spinner-inline"></span> Loading timeline...</div>
        </div>
      </div>
    `;
    panel.dataset.key = ticket.key;
    panel.dataset.signature = structuralSignature;

    // Bind action buttons
    panel.querySelectorAll("[data-action]").forEach((button) => {
      button.addEventListener("click", async () => {
        const action = button.dataset.action;
        try {
          if (action === "open") await api(`/api/tickets/${ticket.key}/open`, { method: "POST" });
          if (action === "kill") await api(`/api/tickets/${ticket.key}/kill`, { method: "POST" });
          if (action === "run") await api(`/api/tickets/${ticket.key}/run-now`, { method: "POST" });
          if (action === "share") {
            const data = await api(`/api/tickets/${ticket.key}/share`, { method: "POST" });
            if (data.ssh_connect) navigator.clipboard?.writeText(data.ssh_connect);
          }
          if (action === "unshare") await api(`/api/tickets/${ticket.key}/unshare`, { method: "POST" });
          await refresh();
        } catch (error) {
          alert(String(error));
        }
      });
    });

    // Bind copy SSH button
    const copyBtn = byId("copy-ssh-btn");
    if (copyBtn) {
      copyBtn.addEventListener("click", () => {
        const text = byId("ssh-connect-text")?.textContent;
        if (text) {
          navigator.clipboard?.writeText(text);
          copyBtn.textContent = "Copied!";
          setTimeout(() => { copyBtn.textContent = "Copy"; }, 2000);
        }
      });
    }

    // Bind terminal mode tabs
    panel.querySelectorAll(".mode-tab").forEach((tab) => {
      tab.addEventListener("click", () => {
        const newMode = tab.dataset.tmode;
        if (newMode === state.terminalMode) return;
        disconnectLive();
        state.terminalMode = newMode;
        renderDetail();
      });
    });

    // Bind observer form
    const observerForm = byId("observer-input-form");
    if (observerForm) {
      observerForm.addEventListener("submit", async (event) => {
        event.preventDefault();
        const input = byId("observer-input");
        const text = input.value;
        if (!ticket.session_name || !text.trim()) return;
        await api("/api/observer/input", {
          method: "POST",
          body: JSON.stringify({ session_name: ticket.session_name, text, press_enter: true }),
        });
        input.value = "";
        await refreshObserver();
      });
    }

    // Set up live keyboard handler
    const liveOutput = byId("live-output");
    if (liveOutput) {
      setupLiveKeyboard(liveOutput);
    }

    // Timeline toggle handler
    const timelineToggle = byId("timeline-toggle");
    if (timelineToggle) {
      timelineToggle.addEventListener("click", () => {
        state.timelineCollapsed = !state.timelineCollapsed;
        const body = byId("timeline-body");
        const arrow = timelineToggle.querySelector(".timeline-arrow");
        if (body) body.classList.toggle("hidden", state.timelineCollapsed);
        if (arrow) arrow.textContent = state.timelineCollapsed ? "\u25B6" : "\u25BC";
        if (!state.timelineCollapsed) {
          fetchTimeline(ticket.key);
        }
      });
    }

    // Fetch timeline if open and ticket changed
    if (!state.timelineCollapsed) {
      fetchTimeline(ticket.key);
    }
  }

  // Update meta strip in-place without rebuilding DOM
  const metaStrip = panel.querySelector(".ticket-meta-strip");
  if (metaStrip) {
    metaStrip.innerHTML = `
      <span><strong>${ticket.key}</strong> · ${ticket.issue_type} · ${ticket.status_name}</span>
      <span><strong>Mode:</strong> ${ticket.local_mode}</span>
      <span><strong>Session:</strong> ${escapeHtml(ticket.session_name || "-")}</span>
      <span><strong>Prompt:</strong> ${escapeHtml(ticket.prompt || "-")}</span>
    `;
  }

  // Handle mode-specific updates
  if (state.terminalMode === "live" && hasSession) {
    if (state.liveIssueKey !== ticket.key) {
      connectLive(ticket.key);
    }
    // Don't steal focus on refresh — only focus on initial connect
  } else {
    disconnectLive();
  }

  if (state.terminalMode === "observer" && hasSession) {
    if (state.observerSession !== ticket.session_name) {
      state.observerSession = ticket.session_name;
    }
    refreshObserver();
  }

  // Update output view if in output mode
  if (state.terminalMode === "output") {
    const output = panel.querySelector(".output");
    if (output) {
      if (output.textContent !== nextOutputText) {
        output.textContent = nextOutputText;
      }
      output.onscroll = () => {
        const distanceFromBottom = output.scrollHeight - output.scrollTop - output.clientHeight;
        state.outputPinned = distanceFromBottom < 24;
        state.outputStateByKey[ticket.key] = {
          text: nextOutputText,
          scrollTop: output.scrollTop,
          distanceFromBottom,
          pinned: state.outputPinned,
        };
      };
      requestAnimationFrame(() => {
        const outputChanged = previousOutputState.text !== nextOutputText;
        if (outputChanged && previousOutputState.pinned) {
          output.scrollTop = output.scrollHeight;
        } else if (!outputChanged) {
          output.scrollTop = previousOutputState.scrollTop;
        } else if (!previousOutputState.pinned) {
          const target = output.scrollHeight - output.clientHeight - previousOutputState.distanceFromBottom;
          output.scrollTop = Math.max(0, target);
        }
        const distanceFromBottom = output.scrollHeight - output.scrollTop - output.clientHeight;
        state.outputPinned = distanceFromBottom < 24;
        state.outputStateByKey[ticket.key] = {
          text: nextOutputText,
          scrollTop: output.scrollTop,
          distanceFromBottom,
          pinned: state.outputPinned,
        };
      });
    }
  }
}

// --- Live terminal WebSocket ---

function connectLive(issueKey) {
  if (state.liveWs && state.liveIssueKey === issueKey) return;
  disconnectLive();

  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  const url = `${protocol}//${location.host}/ws/tickets/${issueKey}/terminal`;
  const ws = new WebSocket(url);
  state.liveWs = ws;
  state.liveIssueKey = issueKey;

  const output = byId("live-output");

  function setLiveIndicator(text, connected) {
    state.liveConnected = connected;
    state.liveStatusText = text;
    // Update inline in the meta strip
    const existing = document.querySelector(".live-indicator");
    if (existing) existing.remove();
    const strip = document.querySelector(".ticket-meta-strip");
    if (strip && text) {
      const el = document.createElement("span");
      el.className = `live-indicator${connected ? " live-on" : ""}`;
      el.textContent = text;
      strip.appendChild(el);
    }
  }

  ws.onopen = () => {
    setLiveIndicator(`live`, true);
    if (output) {
      output.textContent = "Connected. Waiting for output...";
      output.focus();
    }
  };

  ws.onmessage = (event) => {
    const data = JSON.parse(event.data);
    if (data.type === "snapshot" && data.screen) {
      const el = byId("live-output");
      if (el) {
        renderLiveScreen(el, data.screen, data.html);
        // Scroll to keep cursor visible
        const cursorEl = el.querySelector('.live-cursor');
        if (cursorEl) {
          const cursorTop = cursorEl.offsetTop - el.offsetTop;
          const cursorBottom = cursorTop + cursorEl.offsetHeight;
          const viewTop = el.scrollTop;
          const viewBottom = viewTop + el.clientHeight;
          if (cursorTop < viewTop || cursorBottom > viewBottom) {
            // Cursor is out of view — scroll to put it near the middle
            el.scrollTop = Math.max(0, cursorTop - el.clientHeight / 3);
          }
        }
      }
    } else if (data.type === "error") {
      const el = byId("live-output");
      if (el) {
        el.innerHTML = `Error: ${escapeHtml(data.message)}`;
      }
    }
  };

  ws.onclose = () => {
    setLiveIndicator("disconnected", false);
    if (state.liveWs === ws) {
      state.liveWs = null;
      state.liveIssueKey = null;
    }
  };

  ws.onerror = () => {
    setLiveIndicator("error", false);
  };
}

function connectScratchLive(sessionName) {
  if (state.liveWs && state.liveScratchSession === sessionName) return;
  disconnectLive();

  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  const url = `${protocol}//${location.host}/ws/scratch/${encodeURIComponent(sessionName)}/terminal`;
  const ws = new WebSocket(url);
  state.liveWs = ws;
  state.liveScratchSession = sessionName;
  state.liveIssueKey = null;

  const output = byId("live-output");

  ws.onopen = () => {
    if (output) {
      output.textContent = "Connected. Waiting for output...";
      output.focus();
    }
  };

  ws.onmessage = (event) => {
    const data = JSON.parse(event.data);
    if (data.type === "snapshot" && data.screen) {
      const el = byId("live-output");
      if (el) {
        renderLiveScreen(el, data.screen, data.html);
        const cursorEl = el.querySelector('.live-cursor');
        if (cursorEl) {
          const cursorTop = cursorEl.offsetTop - el.offsetTop;
          const cursorBottom = cursorTop + cursorEl.offsetHeight;
          const viewTop = el.scrollTop;
          const viewBottom = viewTop + el.clientHeight;
          if (cursorTop < viewTop || cursorBottom > viewBottom) {
            el.scrollTop = Math.max(0, cursorTop - el.clientHeight / 3);
          }
        }
      }
    } else if (data.type === "error") {
      const el = byId("live-output");
      if (el) {
        el.innerHTML = `Error: ${escapeHtml(data.message)}`;
      }
    }
  };

  ws.onclose = () => {
    if (state.liveWs === ws) {
      state.liveWs = null;
      state.liveScratchSession = null;
    }
  };

  ws.onerror = () => {};
}

function disconnectLive() {
  if (state.liveWs) {
    state.liveWs.close();
    state.liveWs = null;
    state.liveIssueKey = null;
    state.liveScratchSession = null;
  }
}

function renderLiveScreen(element, screen, isHtml) {
  if (isHtml) {
    element.innerHTML = screen;
  } else {
    element.textContent = screen;
  }
}

function setupLiveKeyboard(output) {
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

// --- Timeline ---

async function fetchTimeline(issueKey) {
  // Don't refetch if we already have data for this key (cache for the session)
  const cached = state.timelineByKey[issueKey];
  if (cached && cached.transitions) {
    renderTimelineContent(cached.transitions);
    return;
  }

  const content = byId("timeline-content");
  if (content) content.innerHTML = '<span class="spinner-inline"></span> Loading timeline...';

  try {
    const data = await api(`/api/tickets/${encodeURIComponent(issueKey)}/timeline`);
    state.timelineByKey[issueKey] = {
      transitions: data.transitions || [],
      fetchedAt: Date.now(),
    };
    renderTimelineContent(data.transitions || []);
  } catch (error) {
    if (content) content.textContent = `Failed to load timeline: ${String(error)}`;
  }
}

function renderTimelineContent(transitions) {
  const content = byId("timeline-content");
  if (!content) return;

  if (!transitions.length) {
    content.textContent = "No status transitions found.";
    return;
  }

  const lines = transitions.map((t) => {
    const ts = t.timestamp ? new Date(t.timestamp) : null;
    const timeStr = ts ? ts.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "??:??";
    const author = escapeHtml(t.author || "unknown");
    const toStatus = escapeHtml(t.to_status || "?");
    const fromStatus = escapeHtml(t.from_status || "?");
    const isBot = t.is_bot;
    const cls = isBot ? "timeline-entry bot" : "timeline-entry human";
    return `<div class="${cls}"><span class="timeline-time">${timeStr}</span> <span class="timeline-author">${author}</span> moved <span class="timeline-from">${fromStatus}</span> \u2192 <span class="timeline-to">${toStatus}</span></div>`;
  });

  content.innerHTML = lines.join("");
}

// --- Observer polling ---

async function refreshObserver() {
  if (state.terminalMode !== "observer" || !state.observerSession) return;
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

// --- Routes ---

let routesBuilt = false;
let routesRendering = false; // guard against concurrent async renders
let columnsCache = null; // cached response from /api/board/columns

async function fetchColumnsInfo() {
  try {
    const data = await api("/api/board/columns");
    if (data && data.columns) {
      columnsCache = data;
      return data;
    }
  } catch (e) {
    // endpoint unavailable or failed
  }
  columnsCache = null;
  return null;
}

function renderColumnStrip(root, colData, routes) {
  const configuredStatuses = new Set((colData.configured_routes || []).map((r) => r.status || r));
  const validStatuses = new Set(colData.valid_routes || []);
  const invalidStatuses = new Set(colData.invalid_routes || []);

  let html = '<div class="column-strip-section"><h3>Available Columns</h3>';

  if (!colData.columns || colData.columns.length === 0) {
    html += '<p class="route-hint">Could not load board columns. Check your board_id in config.</p>';
    html += '</div>';
    return html;
  }

  html += '<div class="column-strip">';
  for (const col of colData.columns) {
    const name = typeof col === "string" ? col : col.name || col.status || String(col);
    const isRouted = configuredStatuses.has(name);
    const cls = isRouted ? "column-label routed" : "column-label";
    html += `<span class="${cls}">${escapeHtml(name)}`;
    if (!isRouted) {
      html += ` <button class="add-route-btn" data-add-column="${escapeHtml(name)}" title="Add route for ${escapeHtml(name)}">+ Add</button>`;
    }
    html += `</span>`;
  }
  html += '</div>';

  // Show invalid routes warning
  if (invalidStatuses.size > 0) {
    html += '<div class="column-strip-warnings">';
    for (const inv of invalidStatuses) {
      html += `<span class="route-invalid-badge">Warning: route "${escapeHtml(inv)}" does not match any board column</span>`;
    }
    html += '</div>';
  }

  html += '</div>';
  return html;
}

function statusDropdown(field, currentValue, colData) {
  const statuses = (colData || columnsCache)?.columns?.flatMap(c => c.statuses.map(s => s.name)) || [];
  // Deduplicate
  const unique = [...new Set(statuses)];
  const opts = ['<option value="">— none —</option>'];
  for (const s of unique) {
    const sel = s === currentValue ? " selected" : "";
    opts.push(`<option value="${escapeHtml(s)}"${sel}>${escapeHtml(s)}</option>`);
  }
  // If current value not in list, add it
  if (currentValue && !unique.includes(currentValue)) {
    opts.push(`<option value="${escapeHtml(currentValue)}" selected>${escapeHtml(currentValue)} (not on board)</option>`);
  }
  return `<select class="route-input" data-field="${field}">${opts.join("")}</select>`;
}

function renderRouteBlock(route, validRoutes, colData) {
  const isInvalid = validRoutes && !validRoutes.has(route.status);
  const invalidClass = isInvalid ? " route-invalid" : "";
  const invalidBadge = isInvalid ? '<span class="route-invalid-badge">Column not found in Jira</span>' : "";

  return `
    <div class="route route-edit${invalidClass}" data-route-status="${escapeHtml(route.status)}">
      <div class="route-block">
        <strong>${escapeHtml(route.status)}${invalidBadge}</strong>
        <div class="route-meta">${escapeHtml(route.action)} · board ${route.board_count} · ${route.enabled ? "armed" : "off"}</div>
      </div>
      <div class="route-fields">
        <label>Prompt template</label>
        <div style="color:var(--muted);font-size:11px;margin:2px 0 4px">Variables: <code>{issue_key}</code> <code>{summary}</code> <code>{status}</code> <code>{issue_type}</code></div>
        <textarea class="route-input" data-field="prompt_template" rows="3" placeholder="e.g. Solve ticket {issue_key}: {summary}">${escapeHtml(route.prompt_template || "")}</textarea>

        <div class="route-field-row">
          <div>
            <label>Transition on launch</label>
            ${statusDropdown("transition_on_launch", route.transition_on_launch, colData)}
          </div>
          <div>
            <label>Transition on success</label>
            ${statusDropdown("transition_on_success", route.transition_on_success, colData)}
          </div>
          <div>
            <label>Transition on failure</label>
            ${statusDropdown("transition_on_failure", route.transition_on_failure, colData)}
          </div>
        </div>

        <label>Allowed issue types</label>
        <div class="issue-type-checks" data-field="allowed_issue_types">
          ${(colData?.issue_types || columnsCache?.issue_types || []).map(t => {
            const checked = (route.allowed_issue_types || []).includes(t) ? " checked" : "";
            return `<label class="issue-type-check"><input type="checkbox" value="${escapeHtml(t)}"${checked}/> ${escapeHtml(t)}</label>`;
          }).join("")}
        </div>
      </div>
      <div class="route-actions">
        <button class="route-toggle" data-status="${escapeHtml(route.status)}">${route.enabled ? "Disable" : "Enable"}</button>
        <button class="route-save" data-status="${escapeHtml(route.status)}">Save</button>
        <button class="btn-danger route-delete" data-status="${escapeHtml(route.status)}">Delete</button>
        <span class="route-save-status" data-save-status="${escapeHtml(route.status)}"></span>
      </div>
    </div>
  `;
}

async function renderRoutes() {
  if (routesBuilt) return; // Only build once — don't wipe user edits
  if (routesRendering) return; // Prevent concurrent async renders
  routesRendering = true;
  const root = byId("routes-page");

  // Show spinner while loading columns
  if (!columnsCache) {
    root.innerHTML = '<div class="spinner-wrap"><div class="spinner"></div><p>Loading routes...</p></div>';
  }

  // Fetch column info (non-blocking — gracefully degrades if endpoint missing)
  const colData = columnsCache || await fetchColumnsInfo();

  // Re-read routes from current snapshot AFTER async fetch
  // (snapshot may have refreshed during the await)
  const routes = state.snapshot?.routes || [];

  // Build valid routes set for marking invalid route blocks
  const validRoutes = colData ? new Set(colData.valid_routes || []) : null;

  let html = "";

  // Column strip at the top
  if (colData) {
    html += renderColumnStrip(root, colData, routes);
  } else {
    html += '<div class="column-strip-section"><p class="route-hint">Could not load board columns. Check your board_id in config.</p></div>';
  }

  // Route blocks
  html += routes.map((route) => renderRouteBlock(route, validRoutes, colData)).join("");

  root.innerHTML = html;

  // Bind "+ Add Route" buttons in column strip
  root.querySelectorAll(".add-route-btn").forEach((button) => {
    button.addEventListener("click", async () => {
      const colName = button.dataset.addColumn;
      button.disabled = true;
      button.innerHTML = '<span class="spinner-inline"></span> Adding...';
      try {
        await api("/api/routes", {
          method: "POST",
          body: JSON.stringify({ status: colName }),
        });
        // Refresh everything
        state.snapshot = await api("/api/snapshot");
        columnsCache = null;
        routesBuilt = false;
        routesRendering = false;
        await renderRoutes();
      } catch (error) {
        button.disabled = false;
        button.textContent = "+ Add";
        alert(String(error));
      }
    });
  });

  // Bind toggle buttons
  root.querySelectorAll(".route-toggle").forEach((button) => {
    button.addEventListener("click", async () => {
      try {
        await api(`/api/routes/${encodeURIComponent(button.dataset.status)}/toggle`, { method: "POST" });
        state.snapshot = await api("/api/snapshot");
        columnsCache = null;
        routesBuilt = false;
        routesRendering = false;
        await renderRoutes();
      } catch (error) {
        alert(String(error));
      }
    });
  });

  // Bind save buttons
  root.querySelectorAll(".route-save").forEach((button) => {
    button.addEventListener("click", async () => {
      const status = button.dataset.status;
      const routeEl = root.querySelector(`[data-route-status="${status}"]`);
      if (!routeEl) return;

      const body = {};
      routeEl.querySelectorAll("[data-field]").forEach((el) => {
        const field = el.dataset.field;
        if (field === "allowed_issue_types") {
          body[field] = [...el.querySelectorAll("input:checked")].map(cb => cb.value);
        } else {
          body[field] = (el.value || "").trim();
        }
      });

      const statusEl = root.querySelector(`[data-save-status="${status}"]`);
      button.disabled = true;
      if (statusEl) statusEl.innerHTML = '<span class="spinner-inline"></span> saving...';
      try {
        await api(`/api/routes/${encodeURIComponent(status)}`, {
          method: "PUT",
          body: JSON.stringify(body),
        });
        if (statusEl) {
          statusEl.textContent = "saved";
          statusEl.className = "route-save-status saved";
          setTimeout(() => { statusEl.textContent = ""; statusEl.className = "route-save-status"; }, 2000);
        }
        // Force rebuild: fetch fresh snapshot, then rebuild routes unconditionally
        state.snapshot = await api("/api/snapshot");
        columnsCache = null;
        routesBuilt = false;
        routesRendering = false;
        await renderRoutes();
      } catch (error) {
        if (statusEl) {
          statusEl.textContent = "error";
          statusEl.className = "route-save-status error";
        }
        alert(String(error));
      } finally {
        button.disabled = false;
      }
    });
  });

  // Bind delete buttons
  root.querySelectorAll(".route-delete").forEach((button) => {
    button.addEventListener("click", async () => {
      const status = button.dataset.status;
      if (!confirm(`Delete route for ${status}?`)) return;
      button.disabled = true;
      button.innerHTML = '<span class="spinner-inline"></span> Deleting...';
      try {
        await api(`/api/routes/${encodeURIComponent(status)}`, { method: "DELETE" });
        state.snapshot = await api("/api/snapshot");
        columnsCache = null;
        routesBuilt = false;
        routesRendering = false;
        await renderRoutes();
      } catch (error) {
        button.disabled = false;
        button.textContent = "Delete";
        alert(String(error));
      }
    });
  });

  routesBuilt = true;
  routesRendering = false;
}

// --- Setup ---

let setupFormBuilt = false;
let setupFormLoading = false;
async function renderSetupForm() {
  if (setupFormBuilt) return; // Only build once — don't wipe user edits
  if (setupFormLoading) return;
  setupFormLoading = true;
  const form = byId("setup-form");
  form.innerHTML = '<div class="spinner-wrap"><div class="spinner"></div><p>Loading setup...</p></div>';
  const setup = await api("/api/setup");
  const health = state.snapshot?.health || {};
  const workdirWarn = health.working_dir_valid === false
    ? "Directory does not exist on this machine. Claude sessions will fail until this is fixed."
    : "";
  form.innerHTML = `
    <div class="setup-grid">
      <div>${field("jira_email", "Jira email", setup.jira_email, "The email you use to log into Atlassian/Jira")}</div>
      <div>${field("token_file", "Token file", setup.token_file, "Path to file containing your Jira API token (usually ~/.atlassian-token)")}</div>
      <div>${field("claude_command", "Claude command", setup.claude_command, "CLI command to invoke Claude (usually 'claude')")}</div>
      <div>${field("claude_working_dir", "Claude workdir", setup.claude_working_dir, "The local project directory where Claude runs — must exist on THIS machine", workdirWarn)}</div>
      <div>${field("claude_max_parallel", "Max parallel", setup.claude_max_parallel, "Max simultaneous Claude sessions (1 is safe, 2+ if your machine can handle it)")}</div>
      <div>${field("poll_interval_minutes", "Heartbeat minutes", setup.poll_interval_minutes, "How often to poll Jira for new tickets in trigger columns")}</div>
      <div>${field("site_url", "Site URL", setup.site_url, "Your Atlassian site (e.g. https://yoursite.atlassian.net)")}</div>
      <div>${field("project_key", "Project key", setup.project_key, "Jira project key (e.g. PROJ, MYAPP)")}</div>
      <div>${field("board_id", "Board ID", setup.board_id, "The board number from your Jira board URL (e.g. 123)")}</div>
    </div>
    <div class="setup-actions">
      <button type="submit">Save Setup</button>
    </div>
  `;
  setupFormBuilt = true;
  setupFormLoading = false;
  form.onsubmit = async (event) => {
    event.preventDefault();
    const btn = form.querySelector('button[type="submit"]');
    const origText = btn.textContent;
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-inline"></span> Saving...';
    const formData = Object.fromEntries(new FormData(form).entries());
    formData.claude_max_parallel = Number(formData.claude_max_parallel);
    formData.poll_interval_minutes = Number(formData.poll_interval_minutes);
    try {
      await api("/api/setup", { method: "POST", body: JSON.stringify(formData) });
      btn.innerHTML = '&#10003; Saved';
      setTimeout(() => { btn.textContent = origText; btn.disabled = false; }, 2000);
      playCelebration();
      setupFormBuilt = false;
      await refresh();
    } catch (error) {
      btn.textContent = origText;
      btn.disabled = false;
      alert(String(error));
    }
  };
}

function field(name, label, value, hint = "", warn = "") {
  let html = `<label for="${name}">${label}</label>`;
  if (hint) html += `<div class="field-hint-text">${escapeHtml(hint)}</div>`;
  if (warn) html += `<div class="field-warn">${warn}</div>`;
  html += `<input id="${name}" name="${name}" value="${escapeHtml(String(value ?? ""))}" />`;
  return html;
}

// --- Sharing guide page ---

let sharingPageBuilt = false;

async function renderSharingPage() {
  if (sharingPageBuilt) return;
  const container = byId("sharing-page");
  if (!container) return;

  let sharesHtml = "";
  try {
    const data = await api("/api/shares");
    if (data.shares && data.shares.length > 0) {
      const rows = data.shares.map((s) => `
        <tr>
          <td><strong>${escapeHtml(s.issue_key)}</strong></td>
          <td><code>${escapeHtml(s.ssh_connect)}</code></td>
          <td>${s.read_only ? "read-only" : "read-write"}</td>
        </tr>
      `).join("");
      sharesHtml = `
        <h3>Active Shares</h3>
        <table class="guide-table">
          <thead><tr><th>Ticket</th><th>SSH Command</th><th>Mode</th></tr></thead>
          <tbody>${rows}</tbody>
        </table>
      `;
    } else {
      sharesHtml = `<p class="guide-muted">No active shares. Click <strong>Share</strong> on any ticket with a session to start sharing.</p>`;
    }
  } catch (e) {
    sharesHtml = `<p class="guide-muted">Could not load shares.</p>`;
  }

  let serverInfo = "";
  try {
    const data = await api("/api/upterm/status");
    const dot = data.running ? '<span class="ps-dot-green">●</span>' : '<span class="ps-dot-gray">●</span>';
    serverInfo = `
      <h3>Relay Server ${dot}</h3>
      <table class="guide-table">
        <tr><td><strong>Server</strong></td><td><code>${escapeHtml(data.server)}</code></td></tr>
        <tr><td><strong>Self-hosted</strong></td><td>${data.self_hosted ? "Yes" : "No (using public relay)"}</td></tr>
        ${data.running ? `<tr><td><strong>PID</strong></td><td>${data.pid}</td></tr>` : ""}
      </table>
    `;
  } catch (e) {
    serverInfo = "";
  }

  // Build the onboarding command from the relay server
  let relayServer = "ssh://uptermd.upterm.dev:22";
  try {
    const statusData = await api("/api/upterm/status");
    if (statusData.server) relayServer = statusData.server;
  } catch (e) { /* use default */ }
  const hasServer = relayServer !== "ssh://uptermd.upterm.dev:22";
  const relayFlag = hasServer ? ` --relay "${relayServer}"` : "";
  const onboardCmd = `git clone https://github.com/starshipagentic/swarmgrid.git && cd swarmgrid && ./setup.sh${relayFlag}`;

  container.innerHTML = `
    <h3>Invite a Teammate</h3>
    <div class="guide-section">
      <p><strong>First time</strong> — send this to a dev who hasn't set up yet:</p>
      <div class="onboard-cmd-bar">
        <code id="onboard-cmd-text">${escapeHtml(onboardCmd)}</code>
        <button id="copy-onboard-btn" class="copy-btn">Copy</button>
      </div>
      <p><strong>After first setup</strong> — they just type:</p>
      <pre><code>swarmgrid</code></pre>
      <p>That's it. Opens the board at <code>http://127.0.0.1:8787/board</code> — heartbeat runs automatically.</p>

      <details class="guide-collapse">
        <summary>Optional: dagster for production heartbeat</summary>
        <div class="guide-section">
          <p>The web UI drives the heartbeat on its own — polls Jira, launches sessions, reconciles.
          This works great while you have the browser open.</p>
          <p>For a more production setup (runs independently, survives browser closes, has its own scheduling UI
          with asset lineage and materialization history), use dagster instead:</p>
          <pre><code>./run-dagster.sh</code></pre>
          <p>This starts the dagster daemon + webserver on <code>:3000</code>. When dagster is running,
          the web UI detects it and defers heartbeat scheduling to dagster automatically.
          The board, sharing, and terminal features all work the same either way.</p>
        </div>
      </details>
    </div>

    <h3>Share a Session</h3>
    <div class="guide-section">
      <p>On the <strong>Board</strong> tab, click any ticket with a tmux session, then click <strong>Share</strong>.
      The SSH command appears — copy it and send to your teammate.
      They paste it in their terminal. Done.</p>
      <p>Board indicators:
        <span class="share-dot">◉</span> shared
        <span class="share-dot connected">◉◉</span> viewer connected
        <span class="share-dot typing">◉◉◉</span> active
      </p>
    </div>

    ${sharesHtml}

    <h3>Re-configure</h3>
    <div class="guide-section">
      <p>To change your Jira credentials, board, or other settings:</p>
      <pre><code>./setup.sh</code></pre>
      <p>It shows your current values — press Enter to keep, or type new ones.
      Or edit directly in the <a href="/setup" onclick="event.preventDefault(); setPage('setup')">Setup</a> page.</p>
    </div>

    <details class="guide-collapse">
      <summary>Advanced: self-host a relay server</summary>
      <div class="guide-section">
        <p>By default, session sharing uses the public relay at <code>uptermd.upterm.dev</code>.
        For full privacy, self-host one:</p>
        <pre><code>./scripts/deploy-uptermd.sh    # Fly.io (~$2/month)</code></pre>
      </div>
    </details>
  `;
  // Bind copy button for onboarding command
  const copyOnboard = byId("copy-onboard-btn");
  if (copyOnboard) {
    copyOnboard.addEventListener("click", () => {
      const text = byId("onboard-cmd-text")?.textContent;
      if (text) {
        navigator.clipboard?.writeText(text);
        copyOnboard.textContent = "Copied!";
        setTimeout(() => { copyOnboard.textContent = "Copy"; }, 2000);
      }
    });
  }

  sharingPageBuilt = true;
}

// --- Team page ---

let teamPageBuilt = false;
let teamPageLoading = false;

async function renderTeamPage() {
  if (teamPageBuilt) return;
  const container = byId("team-page");
  if (!container) return;
  if (teamPageLoading) return;
  teamPageLoading = true;

  container.innerHTML = '<div class="spinner-wrap"><div class="spinner"></div><p>Loading team tickets...</p></div>';

  try {
    const [jiraData, hubData, membersData] = await Promise.all([
      api("/api/team/tickets"),
      api("/api/hub/status").catch(() => ({ running: false })),
      api("/api/team/members").catch(() => ({ github_users: [] })),
    ]);

    const githubUsers = membersData.github_users || [];
    let html = "";

    // Team members editor
    html += `<div class="team-members-section">`;
    html += `<h3 class="team-section-title">Team Members</h3>`;
    html += `<p class="team-trigger-hint">GitHub usernames — used for SSH auth when sharing sessions and running the hub.</p>`;
    html += `<div class="team-members-editor">`;
    html += `<div class="team-members-list" id="team-members-list">`;
    for (const user of githubUsers) {
      html += `<span class="team-member-tag" data-user="${escapeHtml(user)}">@${escapeHtml(user)} <button class="team-member-remove" data-user="${escapeHtml(user)}">x</button></span>`;
    }
    html += `</div>`;
    html += `<div class="team-members-add">
      <input type="text" id="team-member-input" placeholder="github username" autocomplete="off" />
      <button id="team-member-add-btn">Add</button>
    </div>`;
    if (githubUsers.length === 0) {
      html += `<p class="team-members-hint">No team members configured. Hub and shares will use token-only auth (anyone with the connect string can connect).</p>`;
    } else {
      html += `<p class="team-members-hint">${githubUsers.length} member${githubUsers.length !== 1 ? "s" : ""} — only these GitHub accounts can connect to hub and shared sessions.</p>`;
    }
    html += `</div></div>`;

    // Hub controls — three modes:
    // 1. Local hub running → operator mode (stop, share connect string)
    // 2. Remote hub configured → member mode (connected to someone else's hub)
    // 3. Neither → unconfigured (choose: start hub OR paste connect string)
    const remoteConnect = membersData.hub_ssh_connect || "";
    const localDevId = membersData.hub_dev_id || "";

    html += `<div class="team-hub-controls">`;
    html += `<h3 class="team-section-title">Hub</h3>`;

    // Dev ID field (needed for both operator and member)
    html += `<div class="team-devid-row">
      <label>Your dev ID: </label>
      <input type="text" id="team-devid-input" value="${escapeHtml(localDevId)}" placeholder="e.g. travis" style="width:140px;margin:0;display:inline-block" />
      <button id="team-devid-save" style="margin-left:4px">Save</button>
      <span class="team-members-hint" style="margin-left:8px">Identifies you in hub checkins. Auto-checkin runs each heartbeat cycle.</span>
    </div>`;

    if (hubData.running) {
      // --- OPERATOR MODE: local hub is running ---
      html += `
        <div class="team-hub-status" style="margin-top:8px">
          <span class="ps-dot-green">●</span> <strong>You are the hub operator</strong>
          <button id="hub-stop-btn" class="btn-danger" style="margin-left:8px">Stop Hub</button>
        </div>`;
      if (hubData.ssh_connect) {
        html += `
        <div class="team-hub-share">
          <span class="team-members-hint">Share this with your team so they can connect:</span>
          <div class="onboard-cmd-bar" style="margin-top:4px">
            <code class="team-hub-connect">${escapeHtml(hubData.ssh_connect)}</code>
            <button class="copy-btn" id="copy-hub-connect">Copy</button>
          </div>
        </div>`;
      }
      html += `<div class="team-hub-stats" style="margin-top:6px">
          ${hubData.checkin_count} checkins from ${hubData.unique_devs} dev${hubData.unique_devs !== 1 ? "s" : ""}
          ${githubUsers.length > 0 ? ` · auth: ${githubUsers.map(u => "@" + u).join(", ")}` : " · auth: token only (open)"}
        </div>`;

    } else if (remoteConnect) {
      // --- MEMBER MODE: connected to someone else's hub ---
      html += `
        <div class="team-hub-status" style="margin-top:8px">
          <span class="ps-dot-green">●</span> Connected to remote hub
        </div>
        <div class="team-hub-connect-row" style="margin-top:4px">
          <code class="team-hub-connect">${escapeHtml(remoteConnect)}</code>
          <button id="hub-disconnect-btn" class="btn-danger" style="margin-left:8px">Disconnect</button>
        </div>
        <p class="team-members-hint">Auto-checkin sends your tickets to this hub each heartbeat cycle${localDevId ? ` as "${escapeHtml(localDevId)}"` : " (set your dev ID above)"}.</p>`;

    } else {
      // --- UNCONFIGURED: choose operator or member ---
      html += `
        <div class="team-hub-unconfigured" style="margin-top:8px">
          <div class="team-hub-option">
            <button id="hub-start-btn">Start Hub on this machine</button>
            ${githubUsers.length > 0 ? `<span class="team-hub-stats" style="margin-left:8px">will auth: ${githubUsers.map(u => "@" + u).join(", ")}</span>` : ""}
            <p class="team-members-hint">Makes this machine the hub operator. Share the connect string with your team.</p>
          </div>
          <div class="team-hub-option" style="margin-top:10px">
            <label>Or paste a connect string from your team lead:</label>
            <div class="team-members-add" style="margin-top:4px">
              <input type="text" id="hub-connect-input" placeholder="ssh abc123@uptermd.upterm.dev" style="width:360px" />
              <button id="hub-connect-save-btn">Connect</button>
            </div>
          </div>
        </div>`;
    }
    html += `</div>`;

    // Hub checkins (if any — from local DB for operator, skip for member for now)
    let hubCheckins = [];
    if (hubData.running) {
      try {
        const teamData = await api("/api/hub/team");
        hubCheckins = teamData.checkins || [];
      } catch (e) { /* ignore */ }
    }

    if (hubCheckins.length > 0) {
      html += `<h3 class="team-section-title">Hub Checkins</h3>`;
      html += `<table class="team-table"><thead><tr>
        <th>Dev</th><th>Ticket</th><th>Status</th><th>Checked In</th>
      </tr></thead><tbody>`;
      for (const c of hubCheckins.slice(0, 50)) {
        const when = new Date(c.checked_in_at * 1000).toLocaleString();
        html += `<tr>
          <td><strong>${escapeHtml(c.dev_id)}</strong></td>
          <td>${escapeHtml(c.ticket_key)}</td>
          <td>${escapeHtml(c.status)}</td>
          <td class="team-time">${when}</td>
        </tr>`;
      }
      html += `</tbody></table>`;
    }

    // Jira pipeline tickets
    const tickets = jiraData.tickets || [];
    if (jiraData.error) {
      html += `<p style="color:#f07070">Error: ${escapeHtml(jiraData.error)}</p>`;
    }

    html += `<h3 class="team-section-title">Pipeline Tickets (${tickets.length})</h3>`;
    if (jiraData.trigger_statuses?.length) {
      html += `<p class="team-trigger-hint">Trigger columns: ${jiraData.trigger_statuses.map(s => `<span class="chip">${escapeHtml(s)}</span>`).join(" ")}</p>`;
    }

    if (tickets.length === 0) {
      html += `<p class="guide-muted">No tickets have passed through trigger columns yet.</p>`;
    } else {
      html += `<table class="team-table"><thead><tr>
        <th>Ticket</th><th>Summary</th><th>Assignee</th><th>Current Status</th><th>Updated</th>
      </tr></thead><tbody>`;
      for (const t of tickets) {
        const updatedTs = t.updated ? new Date(t.updated).toLocaleString() : "-";
        html += `<tr>
          <td><a href="${escapeHtml(t.browse_url)}" target="_blank" class="team-ticket-link">${escapeHtml(t.key)}</a></td>
          <td class="team-summary">${escapeHtml(t.summary)}</td>
          <td>${t.assignee ? escapeHtml(t.assignee) : '<span class="team-unassigned">unassigned</span>'}</td>
          <td><span class="chip">${escapeHtml(t.status)}</span></td>
          <td class="team-time">${updatedTs}</td>
        </tr>`;
      }
      html += `</tbody></table>`;
    }

    html += `<div class="team-refresh"><button id="team-refresh-btn">Refresh</button></div>`;

    container.innerHTML = html;

    teamPageBuilt = true;

    // Bind events
    byId("team-refresh-btn")?.addEventListener("click", () => {
      teamPageBuilt = false;
      teamPageLoading = false;
      renderTeamPage();
    });
    byId("hub-start-btn")?.addEventListener("click", async () => {
      try {
        await api("/api/hub/start", { method: "POST" });
        teamPageBuilt = false;
        teamPageLoading = false;
        renderTeamPage();
      } catch (e) {
        alert("Failed to start hub: " + String(e));
      }
    });
    byId("hub-stop-btn")?.addEventListener("click", async () => {
      try {
        await api("/api/hub/stop", { method: "POST" });
        teamPageBuilt = false;
        teamPageLoading = false;
        renderTeamPage();
      } catch (e) {
        alert("Failed to stop hub: " + String(e));
      }
    });
    byId("copy-hub-connect")?.addEventListener("click", () => {
      const text = container.querySelector(".team-hub-connect")?.textContent;
      if (text) {
        navigator.clipboard?.writeText(text);
        byId("copy-hub-connect").textContent = "Copied!";
        setTimeout(() => { byId("copy-hub-connect").textContent = "Copy"; }, 2000);
      }
    });

    // Team members editor
    const addMember = async (username) => {
      username = username.trim().replace(/^@/, "").toLowerCase();
      if (!username) return;
      const current = [...(membersData.github_users || [])];
      if (current.includes(username)) return;
      current.push(username);
      await api("/api/team/members", { method: "POST", body: JSON.stringify({ github_users: current }) });
      teamPageBuilt = false;
      teamPageLoading = false;
      renderTeamPage();
    };
    byId("team-member-add-btn")?.addEventListener("click", () => {
      addMember(byId("team-member-input")?.value || "");
    });
    byId("team-member-input")?.addEventListener("keydown", (e) => {
      if (e.key === "Enter") addMember(byId("team-member-input")?.value || "");
    });
    for (const btn of container.querySelectorAll(".team-member-remove")) {
      btn.addEventListener("click", async () => {
        const user = btn.dataset.user;
        const current = (membersData.github_users || []).filter(u => u !== user);
        await api("/api/team/members", { method: "POST", body: JSON.stringify({ github_users: current }) });
        teamPageBuilt = false;
        teamPageLoading = false;
        renderTeamPage();
      });
    }

    // Dev ID save
    byId("team-devid-save")?.addEventListener("click", async () => {
      const devId = byId("team-devid-input")?.value || "";
      await api("/api/team/members", { method: "POST", body: JSON.stringify({ hub_dev_id: devId }) });
      const btn = byId("team-devid-save");
      if (btn) { btn.textContent = "Saved"; setTimeout(() => { btn.textContent = "Save"; }, 2000); }
    });

    // Connect to remote hub (member mode)
    byId("hub-connect-save-btn")?.addEventListener("click", async () => {
      const connectStr = byId("hub-connect-input")?.value || "";
      if (!connectStr.trim()) return;
      await api("/api/team/members", { method: "POST", body: JSON.stringify({ hub_ssh_connect: connectStr }) });
      teamPageBuilt = false;
      teamPageLoading = false;
      renderTeamPage();
    });

    // Disconnect from remote hub
    byId("hub-disconnect-btn")?.addEventListener("click", async () => {
      await api("/api/team/members", { method: "POST", body: JSON.stringify({ hub_ssh_connect: "" }) });
      teamPageBuilt = false;
      teamPageLoading = false;
      renderTeamPage();
    });
  } catch (e) {
    container.innerHTML = `<p style="color:#f07070">Failed to load team data: ${escapeHtml(String(e))}</p>`;
  } finally {
    teamPageLoading = false;
  }
}

// --- Celebration ---

function playCelebration() {
  let overlay = document.getElementById("celebration-overlay");
  if (!overlay) {
    overlay = document.createElement("div");
    overlay.id = "celebration-overlay";
    overlay.className = "celebration-overlay";
    overlay.innerHTML = '<video id="celebration-video" autoplay playsinline></video>';
    document.body.appendChild(overlay);
    overlay.addEventListener("click", () => { overlay.classList.add("hidden"); });
  }
  const video = document.getElementById("celebration-video");
  video.src = "/static/assets/kane-netrunners-task.mp4";
  overlay.classList.remove("hidden");
  video.currentTime = 0;
  video.play();
  video.onended = () => { overlay.classList.add("hidden"); };
}

// --- Utilities ---

function markerFor(mode) {
  const map = state.snapshot?.session_legend || {};
  return map[mode] || "";
}

function timeFrom(iso) {
  if (!iso) return "-";
  const ms = new Date(iso).getTime() - Date.now();
  const sec = Math.max(0, Math.floor(ms / 1000));
  const min = String(Math.floor(sec / 60)).padStart(2, "0");
  const rem = String(sec % 60).padStart(2, "0");
  return `${min}:${rem}`;
}

function escapeHtml(value) {
  return value
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

// --- Launch Terminal ---

byId("launch-terminal").addEventListener("click", async () => {
  const btn = byId("launch-terminal");
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-inline"></span> Launching...';
  try {
    const response = await api("/api/scratch-terminal", { method: "POST" });
    btn.textContent = "Terminal Ready";
    setTimeout(() => {
      btn.disabled = false;
      btn.textContent = "Terminal";
    }, 3000);
    // Select the scratch session in the detail view
    state.selectedKey = null;
    state.searchSelectedTicket = null;
    state.scratchSession = response.session_name;
    state.terminalMode = "live";
    render();
  } catch (error) {
    btn.disabled = false;
    btn.textContent = "Terminal";
    alert(`Failed to launch terminal: ${String(error)}`);
  }
});

// --- Panel divider drag ---

const DIVIDER_STORAGE_KEY = "swarmgrid-panel-split";

function applyPanelSplit(ticketHeight) {
  const panel = byId("ticket-detail");
  if (panel) panel.style.height = `${ticketHeight}px`;
}

function restorePanelSplit() {
  const saved = localStorage.getItem(DIVIDER_STORAGE_KEY);
  if (saved) {
    applyPanelSplit(parseInt(saved, 10));
  } else {
    // Default to ~50% of available space
    requestAnimationFrame(() => {
      const view = byId("view-board");
      if (view) applyPanelSplit(Math.floor(view.offsetHeight / 2));
    });
  }
}

restorePanelSplit();

(() => {
  const divider = byId("panel-divider");
  let dragging = false;
  let startY = 0;
  let startHeight = 0;

  divider.addEventListener("mousedown", (e) => {
    e.preventDefault();
    const panel = byId("ticket-detail");
    dragging = true;
    startY = e.clientY;
    startHeight = panel.offsetHeight;
    divider.classList.add("dragging");
    document.body.style.cursor = "row-resize";
    document.body.style.userSelect = "none";
  });

  document.addEventListener("mousemove", (e) => {
    if (!dragging) return;
    const delta = e.clientY - startY;
    const viewHeight = byId("view-board").offsetHeight;
    const maxHeight = viewHeight - 90; // leave room for divider + board header
    const newHeight = Math.max(120, Math.min(maxHeight, startHeight + delta));
    applyPanelSplit(newHeight);
  });

  document.addEventListener("mouseup", () => {
    if (!dragging) return;
    dragging = false;
    divider.classList.remove("dragging");
    document.body.style.cursor = "";
    document.body.style.userSelect = "";
    const panel = byId("ticket-detail");
    localStorage.setItem(DIVIDER_STORAGE_KEY, String(panel.offsetHeight));
  });
})();

// --- Page navigation ---

for (const link of document.querySelectorAll("#page-nav a")) {
  link.addEventListener("click", (event) => {
    event.preventDefault();
    setPage(link.dataset.page);
  });
}

window.addEventListener("popstate", () => {
  state.page = normalizePage(window.location.pathname);
  renderPage();
});

// --- Countdown timer (1-second tick) ---

setInterval(() => {
  if (state.countdownSeconds != null && state.countdownSeconds > 0) {
    state.countdownSeconds--;
  }
  // Update the countdown display in-place without full render
  const cdEl = byId("ps-countdown");
  if (!cdEl) return;
  if (state.countdownSeconds != null && state.countdownSeconds > 0) {
    const min = Math.floor(state.countdownSeconds / 60);
    const sec = String(state.countdownSeconds % 60).padStart(2, "0");
    cdEl.textContent = `${min}:${sec}`;
  } else if (state.countdownSeconds === 0) {
    cdEl.textContent = "now\u2026";
  }
}, 1000);

// --- Keyboard: Enter to run / focus terminal ---

document.addEventListener("keydown", (event) => {
  // Don't intercept if focus is in an input, textarea, form control, or contenteditable
  const tag = document.activeElement?.tagName?.toLowerCase();
  if (tag === "input" || tag === "textarea" || tag === "select") return;
  if (document.activeElement?.isContentEditable) return;
  // Don't intercept if live terminal has focus (it has its own Enter handler)
  if (document.activeElement?.id === "live-output") return;

  if (event.key !== "Enter") return;
  if (state.page !== "board") return;

  const ticket = selectedTicket();
  if (!ticket) return;

  event.preventDefault();

  if (ticket.session_name) {
    // Has session: focus the live terminal
    // Switch to live mode if not already
    if (state.terminalMode !== "live") {
      disconnectLive();
      state.terminalMode = "live";
      renderDetail();
    }
    setTimeout(() => {
      const liveEl = byId("live-output");
      if (liveEl) liveEl.focus();
    }, 50);
  } else {
    // No session: fire Run Now
    (async () => {
      try {
        await api(`/api/tickets/${ticket.key}/run-now`, { method: "POST" });
        await refresh();
      } catch (error) {
        alert(String(error));
      }
    })();
  }
});

// --- Search input binding ---

(() => {
  const searchInput = byId("board-search");
  if (searchInput) {
    searchInput.addEventListener("input", () => {
      handleSearch(searchInput.value);
    });
    searchInput.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        clearSearch();
        searchInput.blur();
      }
    });
  }

  // Cmd/Ctrl+K to focus search
  document.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "k") {
      e.preventDefault();
      if (searchInput) searchInput.focus();
    }
  });
})();

// --- Board switcher change handler ---

(() => {
  const switcher = byId("board-switcher");
  if (switcher) {
    switcher.addEventListener("change", async () => {
      const val = switcher.value;
      if (val === "__add__") {
        showAddBoardForm();
        return;
      }
      const index = parseInt(val, 10);
      if (isNaN(index)) return;
      // Check if already active
      const current = state.boards?.find((b) => b.active);
      if (current && current.index === index) return;
      try {
        await api(`/api/boards/${index}/switch`, { method: "POST" });
        // Reset UI state for new board
        routesBuilt = false;
        routesRendering = false;
        columnsCache = null;
        setupFormBuilt = false;
        setupFormLoading = false;
        sharingPageBuilt = false;
        teamPageBuilt = false;
        teamPageLoading = false;
        state.selectedKey = null;
        state.searchSelectedTicket = null;
        state.timelineByKey = {};
        // Show spinners on all panels while new board loads
        const rp = byId("routes-page");
        if (rp) rp.innerHTML = '<div class="spinner-wrap"><div class="spinner"></div><p>Loading routes...</p></div>';
        const cols = byId("columns");
        if (cols) cols.innerHTML = '<div class="spinner-wrap"><div class="spinner"></div><p>Loading board...</p></div>';
        const detail = byId("ticket-detail");
        if (detail) detail.innerHTML = '<div class="spinner-wrap"><div class="spinner"></div></div>';
        const sf = byId("setup-form");
        if (sf) sf.innerHTML = '<div class="spinner-wrap"><div class="spinner"></div><p>Loading setup...</p></div>';
        const tp = byId("team-page");
        if (tp) tp.innerHTML = '<div class="spinner-wrap"><div class="spinner"></div><p>Loading team...</p></div>';
        disconnectLive();
        await fetchBoards();
        await refresh();
      } catch (e) {
        alert(`Failed to switch board: ${String(e)}`);
        renderBoardSwitcher(); // Reset dropdown
      }
    });
  }
})();

// --- Init ---

setPage(state.page, true);
fetchBoards(); // Load board list once on startup (not every 3s)
refresh();
setInterval(refresh, 3000);
setInterval(refreshObserver, 2000);
