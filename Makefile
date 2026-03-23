.PHONY: help test test-api test-browser heartbeat heartbeat-once start stop status logs deploy build-app

help: ## Show this help
	@echo "SwarmGrid — AI Agent Swarm Orchestrator"
	@echo ""
	@echo "Quick start:"
	@echo "  make start     Start agent (upterm + heartbeat + incoming commands)"
	@echo "  make status    Show current state and cloud routes"
	@echo "  make logs      Watch agent output live"
	@echo "  make stop      Stop agent"
	@echo ""
	@echo "Testing:"
	@echo "  make test      Run all E2E tests (API + browser)"
	@echo "  make test-api  Run API tests only (fast)"
	@echo ""
	@echo "Other:"
	@echo "  make deploy    Deploy to Fly.io"
	@echo "  make build-app Build macOS menu bar app"
	@echo ""

# Run all E2E tests (API + browser)
test:
	.venv/bin/pytest tests/e2e/ -v --tb=short --browser chromium

# Run API tests only (fast, no browser)
test-api:
	.venv/bin/pytest tests/e2e/test_api_e2e.py -v --tb=short

# Run browser tests only
test-browser:
	.venv/bin/pytest tests/e2e/test_dashboard_e2e.py -v --tb=short --browser chromium

# Run heartbeat once (single Jira poll)
heartbeat-once:
	.venv/bin/swarmgrid heartbeat-once | python3 -m json.tool

# Run continuous heartbeat (foreground)
heartbeat:
	.venv/bin/swarmgrid heartbeat

# Start agent (upterm + heartbeat + incoming commands)
start:
	.venv/bin/swarmgrid agent --background

# Stop background heartbeat
stop:
	.venv/bin/swarmgrid stop

# Show current status
status:
	.venv/bin/swarmgrid status

# Watch agent output live
logs:
	tmux attach -t swarmgrid-agent-bg

# Deploy to Fly.io
deploy: check-dashboard
	~/.fly/bin/flyctl deploy --app swarmgrid-api

check-dashboard:
	@bash tests/test_dashboard_syntax.sh

# Build menu bar app
build-app:
	.venv/bin/python -m swarmgrid.menubar.build
