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

1. **Cloud Dashboard** — configure routes and view your board at [swarmgrid.org](https://swarmgrid.org)
2. **Heartbeat** — polls your kanban board, fetches routes from the cloud
3. **Routes** — when a ticket enters a trigger column, launches a Claude agent with your prompt
4. **Transitions** — automatically moves tickets through your workflow (e.g., Droid-Do → In Progress → Review)
5. **Sessions** — each ticket gets its own tmux session, fully isolated
6. **Templates** — reusable command library (/solve, /testgen, /migrate, etc.)
7. **Team visibility** — see who's online, what's running, edge node status

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

Cloud-orchestrated, edge-powered. Your machine does the compute.

## Links

- **Website**: [swarmgrid.org](https://swarmgrid.org)
- **PyPI**: [pypi.org/project/swarmgrid](https://pypi.org/project/swarmgrid/)
- **GitHub**: [github.com/starshipagentic/swarmgrid](https://github.com/starshipagentic/swarmgrid)

## License

MIT
