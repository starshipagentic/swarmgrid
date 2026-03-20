# SwarmGrid Platform Plan: Cloud-Orchestrated, Edge-Powered

## Philosophy

SwarmGrid is NOT a compute provider. It's an **orchestration platform**.

The value proposition: "Stop thinking about your agentic harness. Configure it once in the browser. Your machine does the work. We handle everything else."

Dev teams shouldn't have to:
- Figure out heartbeat polling and Jira integration
- Write YAML configs and debug setup scripts
- Manage tmux sessions and agent lifecycle
- Think about how to share sessions or coordinate across the team
- Remember what slash commands work for which codebase

SwarmGrid remembers all of it, runs the coordination, and sends work to their machines.

## What SwarmGrid.org IS

A web application that:

### 1. Manages identity and teams
- GitHub OAuth sign-in (free, no payment required)
- Create/join a team (org-level)
- Invite teammates by GitHub username
- Each person has their own "edge node" (their laptop)

### 2. Manages board connections
- Paste your Jira board URL → auto-parses site/project/board
- Store Jira credentials securely (encrypted at rest)
- Future: built-in kanban board (no Jira needed)
- Multiple boards per user, multiple users per board

### 3. Manages route configuration
- Same Routes UI we already built, but stored in the cloud
- When you change a route, the change propagates to your edge node
- Routes are per-board, but templates can be shared across boards/teams

### 4. Manages a command template library
- **Global templates**: /solve, /prd2epic, /epic2stories, /testgen, /migrate
  - These are generic starting points that work for most codebases
  - Maintained by SwarmGrid (us)
- **Team templates**: your team's custom commands
  - e.g., "/fix-prisma-migration" — specific to your ORM
  - e.g., "/update-api-docs" — knows your doc format
  - Stored per-team, shared across all boards in that team
- **Project templates**: commands specific to one codebase
  - e.g., "/deploy-staging" — knows your deploy script
  - Stored per-board
- When a route fires, it picks the right template based on this hierarchy:
  project > team > global

### 5. Runs the heartbeat (centrally)
- One heartbeat loop per active board, running in the cloud
- Uses the stored Jira credentials to poll
- Determines which tickets need agent sessions
- Sends launch commands to the user's edge node
- This means: even if your laptop is closed, the heartbeat still knows what's pending. When you open your laptop, queued work starts immediately.

### 6. Relays status and terminal output
- Edge node reports back: session started, output lines, completion status
- Cloud stores this and pushes to the dashboard via WebSocket
- Team members can see each other's session status in the Team view
- Future: live terminal streaming (edge → cloud relay → browser)

### 7. Provides team visibility
- Same Team page concept but powered by the cloud
- Every team member's edge node reports in
- You see: who's online, what tickets are running, what finished
- No hub needed — the cloud IS the hub

## What the edge node IS

A tiny daemon running on the user's machine. It:

### Connects outbound via upterm
```
upterm host --force-command "swarmgrid-worker" --server ssh://relay
```
- Single outbound SSH connection to the relay
- No ports open on user's machine
- Works behind any NAT/VPN/corporate firewall
- On startup, registers its SSH connect string with swarmgrid.org

### Receives commands via CGI-over-SSH
The cloud sends JSON commands by SSHing to the user's upterm session:
```
echo '{"cmd":"launch","ticket":"GRID-142","prompt":"/solve {issue_key}"}' | ssh <token>@relay
```

The worker script handles:

| Command | What it does |
|---------|-------------|
| `launch` | Spawn Claude in tmux with the given prompt. Return session ID. |
| `status` | Is session X still running? Return state + last N lines. |
| `capture` | Return terminal output for session X. |
| `kill` | Terminate session X. |
| `list` | List all active sessions. |
| `ping` | Health check — is the edge node alive? |
| `config` | Receive updated route config from cloud. |

### Manages local sessions
- Claude CLI runs with user's Max plan (they pay Anthropic directly)
- Each ticket gets its own tmux session (existing pattern)
- The worker can run multiple sessions in parallel
- Session output is captured and sent back to the cloud on `status`/`capture` calls

### Reports back to the cloud
After each command, the worker responds with JSON:
```json
{"ok": true, "session_id": "swarmgrid-grid-142-...", "state": "running"}
```
The cloud polls with `status` commands periodically to get output and check completion.

## The cloud heartbeat loop (per board)

```
every N minutes:
  1. Poll Jira for tickets in trigger columns
  2. Compare against known state in Postgres
  3. For each new ticket:
     a. Find the matching route (trigger column → command template)
     b. Resolve the template: project > team > global
     c. Send "launch" command via SSH to the user's edge node
     d. Record session in Postgres
  4. For each running session:
     a. Send "status" command via SSH to edge node
     b. If completed → transition Jira ticket, update dashboard
     c. If failed → transition to failure status, notify user
  5. Push updated state to all connected dashboard WebSockets
```

## The registration flow

### When the edge node starts:
1. upterm establishes connection, gets a session ID + SSH connect string
2. Worker sends a registration to swarmgrid.org:
   ```
   POST https://swarmgrid.org/api/edge/register
   Authorization: Bearer <user-api-key>
   Body: { "ssh_connect": "ssh abc@relay", "hostname": "Travis-MBP" }
   ```
3. Cloud stores this, marks the edge node as "online"
4. Cloud starts (or resumes) the heartbeat for this user's boards

### When the edge node stops:
- Cloud's next `ping` command fails → marks edge as "offline"
- Heartbeat continues (Jira polling still runs) but launch commands queue
- When edge comes back online, queued commands execute

### Token rotation:
- upterm token changes on restart
- The worker re-registers on every startup (idempotent)
- Cloud always uses the latest registered connect string

## The dashboard experience

User visits swarmgrid.org (logged in):

### Board tab
- Live board showing tickets in columns (same UI we built)
- Status indicators: ✹ active, ◌ idle, ◇ stale
- Click ticket → see terminal output (captured from edge node)
- "Run Now" button → cloud sends launch command to edge

### Routes tab
- Same column strip + route editor
- Changes save to Postgres → propagated to edge on next command
- Command template picker: browse global/team/project templates
- "Test this route" → dry-run on edge node

### Team tab
- Every team member's edge node status: online/offline
- Their active sessions across all boards
- Pipeline view: all tickets that went through trigger columns
- No hub needed — cloud IS the hub

### Templates tab (new)
- Browse global command templates with descriptions and examples
- Create team templates (shared across your boards)
- Create project templates (specific to one board/codebase)
- Each template: name, description, prompt template, recommended transitions
- Version history — see what changed

### Setup tab
- Connect your Jira board (paste URL)
- Edge node status: online/offline, last seen, hostname
- Edge install command: `curl -fsSL swarmgrid.org/install | sh`
- API key management

## The template library (deep dive)

This is a key differentiator. Templates are reusable agentic recipes.

### Global templates (we maintain):
```yaml
- name: /solve
  description: "General-purpose bug/task solver"
  prompt: "Solve ticket {issue_key}: {summary}. Read the codebase, implement the fix, run tests."
  recommended_transitions:
    on_launch: "In Progress"
    on_success: "Review"
    on_failure: "Blocked"

- name: /prd2epic
  description: "Convert a PRD document to an Epic with Stories"
  prompt: "/prd2epic {issue_key}"

- name: /testgen
  description: "Generate tests for uncovered code"
  prompt: "Generate comprehensive tests for the changes in {issue_key}: {summary}"

- name: /migrate
  description: "Database migration implementation"
  prompt: "Implement the database migration described in {issue_key}: {summary}"
```

### Team templates (team creates):
```yaml
- name: /fix-prisma
  team: acme-corp
  description: "Fix Prisma schema issues — knows our ORM patterns"
  prompt: "Fix the Prisma-related issue in {issue_key}. Our schema is in prisma/schema.prisma. We use soft deletes. Always generate a migration."

- name: /update-docs
  team: acme-corp
  description: "Update API docs in our format"
  prompt: "Update the API documentation for the changes in {issue_key}. We use OpenAPI 3.1 in docs/api/. Include request/response examples."
```

### Project templates (board-specific):
```yaml
- name: /deploy-check
  board: GRID (board 42)
  description: "Pre-deploy validation for the GRID project"
  prompt: "Run the pre-deploy checklist for {issue_key}: lint, test, build, check migrations. Our CI config is in .github/workflows/deploy.yml."
```

### Template resolution:
When a route fires for board "GRID" in team "acme-corp":
1. Check project templates for GRID → match? use it
2. Check team templates for acme-corp → match? use it
3. Check global templates → match? use it
4. Fallback: use the raw prompt_template from the route

## What we DON'T build (yet)

- Payment/billing (everything is free for now)
- Built-in kanban board (use Jira for now)
- Compute marketplace (Mac Mini farm)
- Live terminal streaming (use polling-based capture for now)
- Mobile app

## Technical stack for the cloud

- **FastAPI** (same as current — just add multi-tenant layer)
- **Postgres** (config, users, teams, sessions, templates)
- **Redis** (WebSocket pub/sub for dashboard updates, session queue)
- **Hosting**: Fly.io or Railway (we already know Fly.io)
- **Auth**: GitHub OAuth (no payment flow needed)

## Credential architecture (critical design decision)

### The problem
- Users want: set up Jira token once, roam across machines
- Karthik has 3 machines. All should do work. All need the token.
- Team lead's token may need to be on the Mac Mini worker too.
- But: central cloud storing everyone's Jira API tokens is a liability. A breach = mass compromise of every user's codebase.

### The solution: edge-to-edge credential propagation (zero-knowledge cloud)

The cloud **never stores raw credentials**. Instead:

1. **User enters Jira token on their first edge node** (their laptop)
2. **Edge node encrypts it** with a key derived from the user's GitHub identity + a device secret
3. **Cloud stores only the encrypted blob** — it cannot decrypt it
4. **When user registers a second machine**, the new edge node requests the credential:
   - New machine → cloud → "Karthik's laptop, please send me the Jira token"
   - Cloud relays the request via SSH to Karthik's laptop
   - Karthik's laptop prompts (or auto-approves if same GitHub user)
   - Token is sent edge-to-edge via the SSH tunnel, never through cloud in plaintext
5. **Team-shared credentials** (team lead's Jira token on the Mac Mini):
   - Team lead approves "share my Jira token with machine X"
   - Transmitted edge-to-edge via SSH
   - Cloud facilitates the connection but never sees the plaintext

### What the cloud knows:
- User identities (GitHub)
- Which machines belong to which user
- Board configuration (URLs, project keys — not secrets)
- Route templates (prompts, transitions)
- Session state (which tickets are running where)
- **Encrypted credential blobs it cannot read**

### What the cloud does NOT know:
- Jira API tokens (encrypted, only edge nodes have the key)
- Claude API keys (same pattern)
- Repository contents
- Terminal output content (optional — can be encrypted or edge-only)

### If the cloud gets breached:
- Attacker gets: usernames, board URLs, route configs, encrypted blobs
- Attacker does NOT get: any credential that can access Jira, Claude, or code
- Damage: limited to metadata. No code access, no ticket manipulation.

### Multi-machine flow for Karthik (3 machines):

```
Machine 1 (laptop — first setup):
  - Enters Jira token → stored locally in keychain
  - Encrypted blob sent to cloud for sync

Machine 2 (desktop — second setup):
  - Runs swarmgrid agent → registers with cloud
  - Cloud asks Machine 1 via SSH: "send token to Machine 2"
  - Machine 1 sends token through SSH tunnel → Machine 2 stores in keychain
  - Cloud never saw the plaintext

Machine 3 (Mac Mini — team worker):
  - Team lead approves: "allow Mac Mini to use my Jira token"
  - Edge-to-edge transfer via SSH, same pattern
```

### Fallback: if no other machine is online
- Cloud can store a user-encrypted blob (encrypted with a passphrase the user sets)
- New machine asks the user for the passphrase to decrypt
- Cloud still never has the key

## Heartbeat location

Given the zero-knowledge design, the **heartbeat runs on edge nodes, not the cloud**.

Why:
- Cloud doesn't have Jira credentials (can't poll)
- Edge nodes have the tokens and the compute
- The cloud's role: tell edge nodes WHAT to do (config), not HOW (execution)

### How it works with multiple machines:
- Cloud assigns one machine as the "primary heartbeat" for each board
- If primary goes offline, cloud promotes another online machine
- Heartbeat results reported back to cloud for dashboard display
- If ALL machines are offline → heartbeat pauses, queues resume on reconnect

### The cloud's coordination role:
```
Cloud sees: Board GRID has 3 online edge nodes (laptop, desktop, Mac Mini)
Cloud assigns:
  - Laptop: primary heartbeat for GRID
  - Desktop: overflow compute (if laptop is at max_parallel)
  - Mac Mini: overflow compute

Laptop goes offline:
  - Cloud promotes Desktop to primary heartbeat
  - Mac Mini stays as overflow
  - Queued work redistributes automatically
```

## Session viewing across machines

Karthik wants to "peek at progress from any machine":

- Terminal output captured by the running edge node
- Sent to cloud as encrypted snapshots (or plaintext if user opts in)
- Any of Karthik's authenticated edge nodes can request the output
- Dashboard in browser also shows it (via WebSocket from cloud)
- If Karthik opens the dashboard on his phone: same view, read-only

## Built-in kanban board (future-proofing)

The architecture should not be Jira-specific:

- Board config in Postgres: columns, statuses, transitions
- Currently populated FROM Jira (via heartbeat polling)
- Future: populated natively (SwarmGrid IS the board)
- The edge node doesn't care — it receives "launch ticket X with prompt Y"
- The cloud doesn't care — it stores board state regardless of source
- The switch from "Jira-backed" to "native board" is a cloud-side change, zero edge changes

## Revised architecture diagram

```
swarmgrid.org (cloud)                    Edge nodes (user machines)
┌──────────────────────────┐            ┌──────────────────────────┐
│                          │            │  Edge 1 (laptop)          │
│  Auth: GitHub OAuth      │            │  ├─ upterm host           │
│  Config: Postgres        │            │  ├─ Jira token (keychain) │
│  ├─ Users, teams         │  SSH cmds  │  ├─ Claude CLI (Max plan) │
│  ├─ Boards, routes       │◄──────────►│  ├─ Heartbeat (primary)   │
│  ├─ Templates            │            │  ├─ tmux sessions         │
│  ├─ Session state        │            │  └─ Reports status back   │
│  ├─ Encrypted cred blobs │            │                          │
│  │  (CANNOT decrypt)     │            ├──────────────────────────┤
│  │                       │            │  Edge 2 (desktop)         │
│  Dashboard relay:        │            │  ├─ upterm host           │
│  ├─ WebSocket to browser │  SSH cmds  │  ├─ Overflow compute      │
│  ├─ Terminal snapshots   │◄──────────►│  ├─ Can become primary    │
│  ├─ Board state          │            │  └─ Same token (synced)   │
│  │                       │            │                          │
│  Coordination:           │            ├──────────────────────────┤
│  ├─ Assign primary HB    │            │  Edge 3 (Mac Mini)        │
│  ├─ Failover promotion   │  SSH cmds  │  ├─ Always-on worker      │
│  ├─ Load distribution    │◄──────────►│  ├─ Team lead's token     │
│  └─ Queue management     │            │  └─ Overflow compute      │
└──────────────────────────┘            └──────────────────────────┘
```

## Summary of responsibilities

| Concern | Who handles it |
|---------|---------------|
| User identity | Cloud (GitHub OAuth) |
| Board/route config | Cloud (Postgres) |
| Template library | Cloud (Postgres) |
| Jira credentials | Edge (keychain, edge-to-edge sync) |
| Claude API key | Edge (same pattern) |
| Heartbeat polling | Edge (primary node, cloud assigns) |
| Agent execution | Edge (Claude + tmux) |
| Session state | Cloud (reported by edge) |
| Terminal output | Edge captures, cloud relays |
| Team visibility | Cloud (aggregates from all edges) |
| Failover | Cloud (promotes new primary) |
| Credential sync | Edge-to-edge via SSH (cloud is relay only) |

## Resolved decisions

### Terminal streaming: polling first
- Cloud sends `capture` via SSH every 5-10 seconds
- Good enough for v1 — most users check in periodically, not watching live
- WebSocket streaming added later as a premium/polish feature

### Edge daemon: terminal command + menu bar app (both in v1)
- **Terminal**: `swarmgrid agent` for debugging and Mac Mini workers
- **Menu bar app**: macOS status bar icon (like Ollama, Macs Fan Control)
  - Tiny icon in the top bar — green dot when connected, gray when offline
  - Click: dropdown showing agents running, tickets active, edge status
  - "Open Dashboard" button → opens swarmgrid.org in browser
  - "Pause / Resume" toggle
  - "Quit" to stop the daemon
  - Built with `rumps` (Python macOS menu bar framework) or SwiftUI
  - Wraps the same daemon — menu bar app IS the agent, just with a UI shell
  - Installed via the `curl install | sh` script (creates .app in /Applications)
  - Auto-starts on login (LaunchAgent)
- **Linux**: system tray via `pystray` or headless daemon with systemd

### Built-in kanban: next sprint after Jira is working
- Architecture already supports it (edge doesn't care about ticket source)
- Eliminates the Jira API token friction entirely
- Jira integration stays as an option for teams already using it
- Native board = truly free tier with zero external dependencies

### Credential UX: minimize friction to ONE annoying step
The only unavoidable friction: each dev creates their own Jira API token (Atlassian requires per-user tokens so actions are attributed correctly — "Fabio moved this ticket").

The SwarmGrid flow makes it as painless as possible:

**Team lead (first setup):**
1. Sign in with GitHub ← one click
2. Paste Jira board URL ← one paste
3. Paste Jira API token ← one paste (direct link to Atlassian token page provided)
4. Configure routes ← in the dashboard
5. Run install script ← one command

**Teammate (joining):**
1. Sign in with GitHub ← one click
2. Accept team invite ← one click
3. Paste their Jira API token ← the ONE annoying step, with a direct link and clear instructions
4. Run install script ← one command
5. Done forever — token stored in keychain, syncs across their machines automatically

**Multi-machine (Karthik's 3 machines):**
- First machine: token stored in keychain during setup
- Second machine: logs in with GitHub, runs install script, token synced from first machine via SSH
- Third machine: same — zero token entry after the first time
- Cloud never sees the raw token

**When we ship the built-in kanban board:**
- Step 3 disappears entirely
- New flow: sign in → accept invite → install → done
- Three steps, zero annoying ones

---

## Implementation plan (parallelizable by agent team)

### Track 1: Cloud API (FastAPI + Postgres)
**Can be built independently — no edge dependency**

- `src/swarmgrid/cloud/app.py` — FastAPI app for swarmgrid.org
- `src/swarmgrid/cloud/auth.py` — GitHub OAuth (login, callback, JWT sessions)
- `src/swarmgrid/cloud/db.py` — Postgres models (users, teams, boards, routes, templates, edge_nodes, sessions)
- `src/swarmgrid/cloud/api_boards.py` — CRUD for boards and routes (same shape as existing `/api/snapshot`, `/api/routes`)
- `src/swarmgrid/cloud/api_teams.py` — team management (create, invite, list members)
- `src/swarmgrid/cloud/api_templates.py` — template library CRUD (global, team, project)
- `src/swarmgrid/cloud/api_edge.py` — edge node registration, status, command dispatch
- `src/swarmgrid/cloud/relay.py` — SSH command sender (sends JSON to edge nodes via their upterm connect string)
- `src/swarmgrid/cloud/heartbeat_coordinator.py` — assigns primary heartbeat node, failover logic
- `src/swarmgrid/cloud/ws.py` — WebSocket endpoint for dashboard (pushes board state, terminal snapshots)

### Track 2: Edge agent + worker
**Can be built independently — no cloud dependency for core logic**

- `src/swarmgrid/agent/daemon.py` — the agent process (upterm host + registration + heartbeat loop)
- `src/swarmgrid/agent/worker.py` — CGI handler (extends hub_handler.py pattern with launch/status/capture/kill/config commands)
- `src/swarmgrid/agent/registration.py` — POST connect string to cloud on startup, re-register on token rotation
- `src/swarmgrid/agent/credential_store.py` — keychain read/write for Jira token, Claude key
- `src/swarmgrid/agent/credential_sync.py` — edge-to-edge token transfer via SSH
- `src/swarmgrid/agent/heartbeat.py` — local heartbeat (reuses existing `service.py` + `jira.py`)
- `src/swarmgrid/agent/session_manager.py` — tmux session lifecycle (reuses existing `runner.py`)

### Track 3: Menu bar app (macOS)
**Can be built independently — wraps the agent daemon**

- `src/swarmgrid/menubar/app.py` — macOS status bar app using `rumps`
  - Icon: green/gray dot based on connection status
  - Dropdown menu:
    - Status: "Connected · 3 agents running"
    - List of active tickets with status
    - Separator
    - "Open Dashboard" → opens browser
    - "Pause" / "Resume"
    - "View Logs" → opens terminal with agent output
    - Separator
    - "Quit SwarmGrid"
  - Wraps `daemon.py` — starts it as a subprocess or in-process
- `src/swarmgrid/menubar/build.py` — py2app or PyInstaller script to create .app bundle
- `resources/icon.png` — menu bar icon (16x16, 32x32 @2x)
- `resources/SwarmGrid.app/` — packaged macOS application
- LaunchAgent plist for auto-start on login

### Track 4: Website + dashboard
**Can be built independently — static HTML + connects to cloud API**

- `docs/index.html` — update landing page with new messaging
- `docs/pricing.html` — pricing tiers (Free/Pro/Team)
- `docs/dashboard/` — the web dashboard (can be the existing `web_static/` adapted for multi-tenant)
  - Login page → GitHub OAuth
  - Board view → calls cloud API instead of local API
  - Routes view → same UI, cloud-backed
  - Team view → cloud-aggregated from all edge nodes
  - Templates view (new) → browse/create/edit command templates
  - Setup view → connect board, manage edge nodes, install instructions
- `docs/install.sh` — hosted install script that users curl

### Track 5: Install script
**Small but critical — the user's first experience**

- `scripts/install.sh` (hosted at swarmgrid.org/install)
  - Detects OS (macOS/Linux)
  - Installs Python if needed, creates venv
  - `pip install swarmgrid`
  - Prompts for API key (generated during signup on swarmgrid.org)
  - Writes `~/.swarmgrid/config.yaml` with API key
  - On macOS: copies .app to /Applications, sets up LaunchAgent
  - On Linux: sets up systemd service
  - Starts the agent
  - Prints: "SwarmGrid is running. Open swarmgrid.org to configure your board."

## Agent team assignment

These 5 tracks can run in parallel:

| Agent | Track | Dependencies |
|-------|-------|-------------|
| Agent 1 | Cloud API | None — define the API contract first, build against it |
| Agent 2 | Edge agent + worker | None — build against the API contract |
| Agent 3 | Menu bar app | Depends on agent daemon (Track 2) being defined |
| Agent 4 | Website + dashboard | Depends on cloud API shape (Track 1) |
| Agent 5 | Install script | Depends on agent package (Track 2) + menu bar app (Track 3) |

**Sequence**: Tracks 1+2 start first (define the API contract between cloud and edge). Track 3 wraps Track 2. Track 4 consumes Track 1. Track 5 packages Track 2+3.

## API contract (cloud ↔ edge)

This is the interface between Track 1 and Track 2. Define it first so both can build independently.

### Cloud → Edge (SSH commands)
```json
{"cmd": "launch", "ticket_key": "GRID-142", "prompt": "/solve ...", "session_config": {...}}
{"cmd": "status", "session_id": "swarmgrid-grid-142-..."}
{"cmd": "capture", "session_id": "...", "lines": 50}
{"cmd": "kill", "session_id": "..."}
{"cmd": "list"}
{"cmd": "ping"}
{"cmd": "config_update", "routes": [...], "templates": [...]}
{"cmd": "credential_request", "from_edge": "edge-abc", "credential_type": "jira_token"}
```

### Edge → Cloud (HTTPS POST)
```
POST /api/edge/register   — {"ssh_connect": "ssh abc@relay", "hostname": "Travis-MBP", "os": "macOS"}
POST /api/edge/heartbeat  — {"board_id": 42, "tickets_found": [...], "sessions_launched": [...]}
POST /api/edge/status     — {"sessions": [{"id": "...", "state": "running", "output_lines": 50}]}
POST /api/edge/completed  — {"session_id": "...", "result": "success", "output": "..."}
POST /api/edge/offline    — (sent on graceful shutdown)
```

### Cloud → Browser (WebSocket)
```json
{"type": "board_update", "columns": [...]}
{"type": "session_update", "session_id": "...", "state": "running", "output": "..."}
{"type": "edge_status", "edges": [{"hostname": "...", "online": true, "sessions": 3}]}
{"type": "team_update", "members": [...]}
```

## Verification (end-to-end)

1. Visit swarmgrid.org → sign in with GitHub → see empty dashboard
2. Paste Jira board URL → configure routes with templates
3. Run `curl -fsSL swarmgrid.org/install | sh` → agent starts, menu bar icon appears
4. Edge registers with cloud → dashboard shows "1 edge node online"
5. Ticket enters trigger column → cloud heartbeat detects it
6. Cloud sends `launch` via SSH → edge spawns Claude in tmux
7. Cloud polls `status` → terminal output appears in dashboard
8. Claude finishes → edge reports completion → Jira ticket transitions
9. Team member signs in → sees the ticket in Team view
10. Open SwarmGrid from menu bar → dashboard shows all activity
