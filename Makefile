.PHONY: test test-api test-browser heartbeat heartbeat-once start stop status deploy build-app

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

# Start background heartbeat daemon
start:
	.venv/bin/swarmgrid heartbeat --background

# Stop background heartbeat
stop:
	.venv/bin/swarmgrid stop

# Show current status
status:
	.venv/bin/swarmgrid status | python3 -m json.tool

# Deploy to Fly.io
deploy:
	~/.fly/bin/flyctl deploy --app swarmgrid-api

# Build menu bar app
build-app:
	.venv/bin/python -m swarmgrid.menubar.build
