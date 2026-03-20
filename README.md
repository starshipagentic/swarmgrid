# SwarmGrid

AI agent swarm orchestrator for kanban boards. Poll your board, launch Claude agents in tmux, watch them work, share sessions with your team.

Works with Jira, and designed to extend to other kanban providers.

## Quick Start

```bash
git clone https://github.com/starshipagentic/swarmgrid.git
cd swarmgrid && ./setup.sh
```

After first setup:
```bash
swarmgrid
```

Or install from PyPI:
```bash
pip install swarmgrid
```

## What it does

1. **Heartbeat** — polls your kanban board every few minutes
2. **Routes** — when a ticket enters a trigger column, launches a Claude agent with your prompt
3. **Sessions** — each ticket gets its own tmux session, fully isolated
4. **Dashboard** — web UI at `http://127.0.0.1:8787` with live board, terminal, and team view
5. **Sharing** — share any session with a teammate over SSH (via upterm)
6. **Team Hub** — see what everyone is working on across the squad

## The Progression

- **One agent** solving a ticket
- **A team of agents** — Claude Opus 4.6 team lead coordinating specialised workers
- **A squad of engineers** — each running their own agent constellation
- **A swarm of squads** — multiple boards, one dashboard

## Stack

- Python + FastAPI (dashboard + heartbeat)
- tmux (session isolation)
- Claude CLI (AI agent)
- upterm (SSH sharing + hub transport)
- SQLite (local state + hub)
- Kanban board API (Jira supported, extensible)

No cloud. No Docker. No servers. Just your laptop.

## Links

- **Website**: [swarmgrid.org](https://swarmgrid.org)
- **PyPI**: [pypi.org/project/swarmgrid](https://pypi.org/project/swarmgrid/)
- **GitHub**: [github.com/starshipagentic/swarmgrid](https://github.com/starshipagentic/swarmgrid)

## License

MIT
