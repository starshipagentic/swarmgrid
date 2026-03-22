# SwarmGrid Security — How It Works Right Now

Written 2026-03-22. This is a plain-English walkthrough of what's protected,
what's not, and what an attacker would need to do to cause damage.

---

## The Big Picture

Your SwarmGrid system has two parts:

- **Your Mac** — runs the agent, talks to Jira, launches Claude sessions
- **Fly.io cloud** — a website/API that stores your config and relays commands to your Mac

The cloud has a database (your routes, team members, Jira creds) and three
secret values stored as environment variables (not in the database).

---

## The Three Secrets

These live as Fly.io environment variables. They are NOT in the database.
To get these, someone would need access to your Fly.io account itself.

### Secret 1: JWT_SECRET

**What it does — two jobs:**
1. Signs login tokens. When you log in with GitHub, this secret creates a
   token that proves "this is Travis." Every API call checks this token.
2. Encrypts your Jira credentials in the database. Your Jira email and API
   token are scrambled using this secret before being saved.

**Why this is a problem:**
One secret does two jobs. If someone gets JWT_SECRET, they can:
- Step 1: Create a fake login token pretending to be you
- Step 2: Call any API endpoint as you
- Step 3: Also unscramble your Jira email and token from the database
- Step 4: Now they have your Jira access

**This is the single point of failure right now.**

### Secret 2: CLOUD_SSH_PRIVATE_KEY

**What it does:**
This is the cloud server's SSH key. When you click "open session" on the
dashboard, the cloud uses this key to SSH into your Mac through upterm.

Your Mac only allows SSH connections from keys listed in
`~/.swarmgrid/authorized_keys`. Right now that file has one key: the
cloud's public key. So the cloud can connect. Random people cannot.

**If someone gets this key:**
- They need the key AND your connect string (also in the database)
- With both, they can SSH into your Mac's upterm session
- But --force-command means they can ONLY run worker.py commands
- They could: list your sessions, see terminal output, open iTerm2, kill a session
- They could NOT: get a shell, read your files, run arbitrary commands

### Secret 3: GITHUB_CLIENT_SECRET

**What it does:**
Part of the GitHub OAuth login flow. When you click "Login with GitHub" on
the dashboard, this proves to GitHub that SwarmGrid is a real app.

**If someone gets this:**
- They could make a fake SwarmGrid login page
- But they'd still need a user to visit it and click "Authorize"
- Low risk on its own

---

## What's In The Database

If someone got a copy of the SQLite database file (but NOT the env vars):

| Data | What they'd see | Can they use it? |
|---|---|---|
| Jira email | Scrambled gibberish | No — need JWT_SECRET to unscramble |
| Jira API token | Scrambled gibberish | No — need JWT_SECRET to unscramble |
| SSH connect string | Plain text like `ssh abc123@uptermd.upterm.dev` | No — need the cloud's SSH private key to connect |
| Route configs | Plain text "Droid-Do -> /solve" | No — this just describes your workflow |
| Session records | Plain text ticket keys and prompts | No — just shows what you worked on |
| GitHub usernames | Plain text | No — these are public info anyway |

**A database-only leak is actually pretty safe.** The two dangerous things
(Jira creds and connect strings) are both protected by a second factor
that only exists in the Fly.io environment variables.

---

## Attack Scenarios — What Could Actually Happen

### Scenario 1: Someone finds your upterm connect string

Maybe they saw it in a log, or you accidentally pasted it in Slack.

- They try to SSH in: `ssh abc123@uptermd.upterm.dev`
- Upterm checks: "Is this person's SSH key in the authorized_keys file?"
- It's not. Connection rejected. **Nothing happens.**

### Scenario 2: Someone gets your database file

- They see scrambled Jira creds — can't read them
- They see connect strings — can't use them without the SSH key
- They see your routes and session history — boring, not dangerous
- **Nothing happens.**

### Scenario 3: Someone gets into your Fly.io account

This is the bad one. They get everything:
- JWT_SECRET → can unscramble Jira creds → full Jira access
- CLOUD_SSH_PRIVATE_KEY + connect strings from DB → can SSH into your Mac
  (but only worker.py commands, not a shell)
- GITHUB_CLIENT_SECRET → can make fake login pages

**This is the real threat.** But it requires compromising your Fly.io
account, which means they need your Fly.io email + password (or a stolen
session token).

### Scenario 4: Someone intercepts network traffic

- All connections use SSH (encrypted) or HTTPS (encrypted)
- Even if they capture the traffic, they see encrypted noise
- **Nothing happens.**

---

## The One Thing I'd Fix

Right now JWT_SECRET does two jobs: authentication AND encryption.
If we split those into two separate secrets:

```
JWT_SECRET          → only signs login tokens
ENCRYPTION_KEY      → only encrypts Jira creds
```

Then if JWT_SECRET leaks:
- Attacker can mint fake tokens and call APIs
- But they CANNOT decrypt Jira creds (need ENCRYPTION_KEY for that)
- The blast radius is smaller — they can see your routes and sessions
  through the API, but can't get your Jira access

And if ENCRYPTION_KEY leaks:
- Attacker can decrypt Jira creds in the database
- But they CANNOT call any API (need JWT_SECRET for tokens)
- They'd also need the database itself

**Both would need to leak for full compromise.** Right now, only one
needs to leak. That's the difference.

### How hard is this fix?

Easy. Change `crypto.py` to read from `ENCRYPTION_KEY` env var instead
of deriving from `JWT_SECRET`. Set a new Fly.io secret. Re-encrypt
existing Jira creds once. Done.

---

## Summary For a 10-Year-Old

Your Mac is a house. The cloud is a phone book that knows your address.

- **The front door has a special lock** (authorized_keys). Only the cloud's
  key fits. Random people can't get in even if they know your address.

- **Inside the house, there's only one room** (force-command). Even if
  someone gets through the door, they can only sit in one chair and push
  specific buttons. They can't wander around.

- **The phone book** (database) has your address written in invisible ink
  (encryption). Someone who steals the phone book can't read it without
  the magic flashlight (JWT_SECRET).

- **Right now, the magic flashlight also unlocks a separate safe** where
  your Jira keys are kept. That's not great — one flashlight shouldn't do
  two things. We should have two flashlights.
