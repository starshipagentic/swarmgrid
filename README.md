# SwarmGrid

AI agent swarm orchestrator for Jira. Poll your board, launch Claude agents in tmux, watch them work, share sessions with your team.

## Quick Start

```bash
git clone https://github.com/swarmgrid/swarmgrid.git
cd swarmgrid && ./setup.sh
```

After first setup:
```bash
swarmgrid
```

## What it does

1. **Heartbeat** — polls your Jira board every few minutes
2. **Routes** — when a ticket enters a trigger column, launches a Claude agent with your prompt
3. **Sessions** — each ticket gets its own tmux session, fully isolated
4. **Dashboard** — web UI at `http://127.0.0.1:8787` with live board, terminal, and team view
5. **Sharing** — share any session with a teammate over SSH (via upterm)
6. **Team Hub** — see what everyone is working on across the squad

## The Progression

- **One agent** solving a ticket
- **A team of agents** — Claude Opus 4.6 team lead coordinating specialised workers
- **A squad of engineers** — each running their own agent constellation
- **A swarm of squads** — multiple Jira boards, one dashboard

## Stack

- Python + FastAPI (dashboard + heartbeat)
- tmux (session isolation)
- Claude CLI (AI agent)
- upterm (SSH sharing + hub transport)
- SQLite (local state + hub)
- Jira REST API (ticket management)

No cloud. No Docker. No servers. Just your laptop.

## License

MIT
