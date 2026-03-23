# SwarmGrid Two-Agent Architecture

Status: PLAN — not yet implemented.
Written: 2026-03-22

## Core Principle

The cloud (swarmgrid.org) is a bulletin board, config store, and setup
assistant. It is NEVER a GitHub user. It is NEVER a teammate. It connects
to your machine via a separate authorized-key mechanism — completely
isolated from the GitHub user trust circle that protects real sessions.

If the cloud is breached, no real session can be reached. The attacker
would need to compromise a teammate's GitHub SSH key — at which point
they've compromised GitHub itself, not SwarmGrid.

---

## The Two Agents

### Agent 1: Phonebook (cloud-facing)

**Purpose:** Let the cloud check on you and send limited commands.

**Upterm config:**
- `--authorized-keys` with the cloud's SSH public key ONLY
- `--force-command phonebook-worker.py`
- NO `--github-user` — the cloud is never a GitHub user

**What it can do (force-command limits):**
- `ping` — is this machine alive?
- `status` — what boards are active, how many sessions, last heartbeat
- `sessions_summary` — partial info: board, ticket key, session state,
  truncated/opaque session IDs. NOT real connect strings.
- `open_local` — tell the Mac to open an iTerm2 window for a session
  that already exists (pops open locally, no remote access granted)
- `refresh_config` — tell the agent to pull fresh config from the cloud now

**What it CANNOT do:**
- Get real session connect strings
- Get full session IDs
- Get prompts, output, or any session content
- Anything outside the force-command list

**What the cloud knows via this agent:**
- That you're online
- What boards you're working on
- How many sessions are running and their ticket keys
- Partial/opaque session identifiers

**Also pushes data UP via HTTPS:**
- Heartbeat POSTs to swarmgrid.org with the same partial info
- This is belt-and-suspenders — cloud gets data even if it doesn't
  actively SSH in to ask

### Agent 2: Front Desk (team-facing)

**Purpose:** Let verified GitHub teammates discover and connect to
real sessions.

**Upterm config:**
- `--github-user` for ALL teammates across ALL boards/clients
- `--force-command frontdesk-worker.py`
- NO `--authorized-keys` for the cloud — the cloud cannot reach this agent

**Why the full pool of GitHub users across all clients:**
This is the outer gate. It answers "can you talk to the front desk at all?"
Board-level scoping happens INSIDE the worker — the front desk checks
which board a ticket belongs to and whether the requesting user is on
that board's team before giving out any session info.

**What it can do:**
- `list_sessions` — full session list for boards the caller has access to
- `get_session_connect` — returns the REAL upterm connect string for a
  specific ticket's session, BUT ONLY if the caller's GitHub user is on
  that board's team
- `attach` — tell the Mac to open iTerm2 for a session (same as phonebook
  but with GitHub identity verification)
- `status` — full status for boards the caller has access to

**Board-level access check (inside the worker):**
```
Request: {"cmd": "get_session_connect", "ticket": "LMSV3-857"}
Caller: karthik (verified by upterm SSH handshake)

1. Look up LMSV3-857 → board LMSV3
2. Look up LMSV3 team → [starshipagentic, karthik, pree]
3. Is karthik in that list? → YES
4. Return real connect string for LMSV3-857's upterm session
```

If karthik asks for ACME-123 and he's not on the ACME board team,
the front desk refuses. He can reach the front desk (he's in the
global github-user list) but can't access sessions on boards he's
not part of.

### The Sessions Themselves

Each Claude/tmux session runs its own upterm session:
- `--github-user` scoped to THAT BOARD's team only (not the global list)
- `--force-command` optional — can be full shell for pair programming,
  or read-only, or force-command for structured interaction
- The connect string is only given out by the front desk after verifying
  GitHub identity and board membership

---

## The Flows

### Flow 1: Dashboard shows team status

1. Travis's phonebook agent pushes heartbeat data to swarmgrid.org
   via HTTPS POST (outbound, no ports opened)
2. Dashboard shows: "Travis online, 3 sessions running on LMSV3"
3. Partial info only — ticket keys and states, no connect strings,
   no prompts, no output

### Flow 2: Open iTerm2 from swarmgrid.org

1. Travis clicks "open LMSV3-857" on the dashboard (maybe from his phone)
2. Cloud SSHs into Travis's phonebook agent (authorized-keys, force-command)
3. Sends: `{"cmd": "open_local", "ticket": "LMSV3-857"}`
4. Phonebook worker calls `open_session_in_terminal()` on Travis's Mac
5. iTerm2 pops open with the tmux session
6. Cloud never had the session connect string

### Flow 3: Karthik pair programs on LMSV3-857

1. Karthik logs into swarmgrid.org with GitHub
2. Dashboard shows: "Travis online, LMSV3-857 running"
3. Dashboard provides Travis's front desk connect string
4. Karthik's machine SSHs into Travis's front desk
5. Upterm verifies: karthik is in --github-user list → allowed in
6. Force-command → frontdesk-worker.py
7. Karthik sends: `{"cmd": "get_session_connect", "ticket": "LMSV3-857"}`
8. Front desk checks: karthik is on LMSV3 board → yes
9. Front desk returns: real session connect string
10. Karthik SSHs into the real session
11. Upterm verifies: karthik is in --github-user for this session → in
12. Pair programming. Full shell (or read-only, Travis's choice).

### Flow 4: New machine setup

1. Travis gets a new Mac
2. Goes to swarmgrid.org, logs in with GitHub
3. Dashboard says "Welcome back. Here's your install script."
4. Script is pre-configured with Travis's API key, board configs, routes
5. Runs the script → installs swarmgrid agent
6. Agent starts both phonebook and front desk upterm sessions
7. Registers phonebook connect string with the cloud
8. Front desk connect string pushed to cloud for teammate discovery
9. Heartbeat starts, pulls routes from cloud, talks to Jira
10. Back to full operation

---

## Breach Scenarios

### Cloud DB breached (no env vars)

Attacker gets: encrypted gibberish. Nothing usable.

### Cloud DB + ENCRYPTION_KEY breached

Attacker gets: phonebook connect string, front desk connect string,
partial session info, routes, teammate GitHub usernames.

- Phonebook: needs cloud's SSH private key to connect. Don't have it.
- Front desk: needs to be a GitHub user on the team. They're not.
- Sessions: don't even have connect strings (not stored in cloud).
- **Result: nothing.**

### Cloud DB + ENCRYPTION_KEY + CLOUD_SSH_PRIVATE_KEY breached

Attacker gets: access to the phonebook agent only.

- Can ping, get status, see partial session info
- Can send `open_local` — iTerm2 windows pop open on Travis's Mac. Annoying.
- CANNOT reach front desk (requires GitHub user, not authorized-key)
- CANNOT reach any real session
- CANNOT get real session connect strings (phonebook doesn't serve them)
- **Result: nuisance only. No data exposure. No session access.**

### All cloud secrets + a compromised GitHub SSH key

At this point, the attacker has compromised GitHub's SSH infrastructure
or stolen a teammate's private key from their machine. This is beyond
SwarmGrid's threat model — it's a GitHub/personal machine compromise.

---

## Critical Rule

**The cloud is NEVER a GitHub user.**

The cloud connects via `--authorized-keys` to the phonebook ONLY.
The cloud's SSH key is NEVER added to any `--github-user` list.
There is no `swarmgrid-cloud-bot` GitHub account.

If someone accidentally adds the cloud as a GitHub user on a team,
it would collapse the two-agent separation — the cloud could reach
the front desk and get real session connect strings. This must never
happen.

The two agents enforce this by using completely different auth mechanisms:
- Phonebook: `--authorized-keys` (machine key, no GitHub identity)
- Front desk: `--github-user` (human identity, no machine keys)

These two mechanisms are mutually exclusive in upterm. One agent cannot
accidentally become the other.

---

## Auth Clarification: OAuth vs SSH Keys

Two completely separate identity systems are in play:

**GitHub OAuth (web login):**
- Used when you click "Login with GitHub" on swarmgrid.org
- GitHub asks "authorize SwarmGrid?" → you click yes
- SwarmGrid gets your github_login, creates a JWT
- This is how you log into the dashboard. Nothing to do with SSH.

**GitHub SSH keys (upterm connections):**
- Used when someone SSHs into an upterm session with `--github-user`
- Upterm checks `github.com/{username}.keys` for matching public keys
- The user must have uploaded an SSH public key to their GitHub account
  (GitHub → Settings → SSH and GPG keys → New SSH key)
- This is how teammates connect to the front desk and real sessions

**You can have one without the other:**
- A user can log into swarmgrid.org (OAuth) without SSH keys on GitHub
- A user can SSH into upterm sessions (SSH keys) without a SwarmGrid account
- For the full dashboard-click-to-pair-program flow, you need both

**The board owner (starshipagentic) uses the phonebook path for dashboard
actions (open iTerm2, refresh config) — this goes through authorized-keys,
not GitHub SSH. So the owner doesn't need SSH keys on GitHub for basic
dashboard functionality.**

---

## Flow 5: Teammate Pair Programs via Dashboard Click (NOT YET BUILT)

This is the end-to-end flow where tsomerville2 clicks a session on
swarmgrid.org and gets pair-programmed into the tmux session on
starshipagentic's Mac.

### Prerequisites
- tsomerville2 has a GitHub account with an SSH key uploaded
- tsomerville2 is added to the LMSV3 board on swarmgrid.org
- tsomerville2 has swarmgrid agent running on their own Mac
- starshipagentic has LMSV3-857 running in a tmux session with
  its own upterm share (--github-user tsomerville2)

### The Click Flow

1. tsomerville2 logs into swarmgrid.org with GitHub OAuth
2. Dashboard shows LMSV3 board (tsomerville2 is a board member)
3. LMSV3-857 shows a "running" session indicator on starshipagentic's node
4. tsomerville2 clicks "Join session" on LMSV3-857
5. Dashboard calls cloud API: POST /api/edge/join-session
   Body: {ticket_key: "LMSV3-857", target_node_id: 1}
6. Cloud returns starshipagentic's front desk connect string
   (decrypted from DB, returned to tsomerville2's browser)
7. Dashboard tells tsomerville2's LOCAL agent to connect:
   POST http://localhost:{agent_port}/connect
   Body: {frontdesk_connect: "ssh TOKEN@uptermd.upterm.dev",
          ticket_key: "LMSV3-857",
          github_user: "tsomerville2"}
8. tsomerville2's agent SSHs into starshipagentic's front desk
9. Front desk verifies: tsomerville2 on LMSV3? Yes.
   Returns real session connect string.
10. tsomerville2's agent SSHs into the real session
11. iTerm2 pops open on tsomerville2's Mac, attached to LMSV3-857

### What needs to be built

**A. Local agent HTTP endpoint** (`/connect` or similar)
   - The dashboard needs to talk to the LOCAL agent (not the cloud)
   - Agent runs a small HTTP server on localhost (e.g., port 19222)
   - Receives: frontdesk connect string + ticket key + github user
   - Executes: SSH into front desk → get session connect → SSH into session → open iTerm2
   - This is the `swarmgrid connect` command wrapped in an HTTP endpoint

**B. CLI command: `swarmgrid connect`**
   - `swarmgrid connect --frontdesk "ssh TOKEN@uptermd.upterm.dev" --ticket LMSV3-857`
   - SSHs into the front desk (using the user's own SSH key / GitHub identity)
   - Sends get_session_connect command
   - Gets the real session connect string
   - Opens iTerm2 with: tmux attach via SSH to the real session
   - This is the CLI version of what the dashboard click does

**C. Dashboard UI changes**
   - Board view: session indicators show which node has the session
   - Click on a session-active ticket → "Join session" button
   - Join button calls localhost agent endpoint (not cloud relay)
   - Fallback: if local agent not running, show the CLI command to copy/paste

**D. Per-ticket upterm sharing**
   - When heartbeat launches a Claude session in tmux, also start an upterm
     share for that tmux session with --github-user scoped to that board's team
   - This creates the connect string that the front desk hands out
   - Currently sessions are tmux-only (no upterm share per session)

### Without building anything new, tsomerville2 can already:
   - `ssh TOKEN@uptermd.upterm.dev` → reaches the front desk
   - Send `{"cmd": "get_session_connect", "ticket_key": "LMSV3-857", "github_user": "tsomerville2"}`
   - Get the real session connect string back
   - SSH into the real session
   - This is the CLI-only path — works today, just not pretty
