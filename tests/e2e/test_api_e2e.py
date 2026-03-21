"""E2E tests for the SwarmGrid Cloud API.

These hit the REAL production API on swarmgrid-api.fly.dev.
They create, read, update, and delete real data, then clean up.
"""
import requests
import pytest
import yaml
from pathlib import Path


def _api(auth_token, api_url, path, method="GET", json=None):
    resp = requests.request(
        method, f"{api_url}{path}",
        headers={"Authorization": f"Bearer {auth_token}", "Content-Type": "application/json"},
        json=json,
        timeout=15,
    )
    return resp


# ── Health ──────────────────────────────────────────────────

class TestHealth:
    def test_health_endpoint(self, api_url):
        resp = requests.get(f"{api_url}/health", timeout=10)
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_root_endpoint(self, api_url):
        resp = requests.get(f"{api_url}/", timeout=10)
        assert resp.status_code == 200
        assert "SwarmGrid" in resp.json()["message"]


# ── Auth ────────────────────────────────────────────────────

class TestAuth:
    def test_me_returns_user(self, auth_token, api_url):
        resp = _api(auth_token, api_url, "/api/auth/me")
        assert resp.status_code == 200
        data = resp.json()
        assert data["github_login"] == "starshipagentic"
        assert data["id"] == 1

    def test_invalid_token_rejected(self, api_url):
        resp = _api("garbage-token", api_url, "/api/auth/me")
        assert resp.status_code == 401


# ── Boards ──────────────────────────────────────────────────

class TestBoards:
    def test_list_boards(self, auth_token, api_url):
        resp = _api(auth_token, api_url, "/api/boards")
        assert resp.status_code == 200
        boards = resp.json()["boards"]
        assert len(boards) >= 1
        assert any(b["name"] == "LMSV3" for b in boards)

    def test_get_board(self, auth_token, api_url):
        resp = _api(auth_token, api_url, "/api/boards/1")
        assert resp.status_code == 200
        board = resp.json()["board"]
        assert board["name"] == "LMSV3"
        assert board["jira_token"] == "\u2022\u2022\u2022\u2022"  # masked

    def test_board_snapshot_has_columns(self, auth_token, api_url):
        resp = _api(auth_token, api_url, "/api/boards/1/snapshot")
        assert resp.status_code == 200
        data = resp.json()
        columns = data["columns"]
        assert len(columns) >= 5
        col_names = [c["name"] for c in columns]
        assert "Droid-Do" in col_names
        assert "PRD" in col_names
        assert "In Progress" in col_names

    def test_snapshot_has_real_tickets(self, auth_token, api_url):
        resp = _api(auth_token, api_url, "/api/boards/1/snapshot")
        data = resp.json()
        all_tickets = []
        for col in data["columns"]:
            all_tickets.extend(col.get("tickets", []))
        assert len(all_tickets) > 0, "Snapshot should have real Jira tickets"
        assert all(t["key"].startswith("LMSV3-") for t in all_tickets)


# ── Routes CRUD ─────────────────────────────────────────────

class TestRoutesCRUD:
    """Create, read, update, toggle, delete a route on a REAL board."""

    def test_route_lifecycle(self, auth_token, api_url):
        board_id = 1
        status = "SGTEST-Route-Column"

        # Clean up any leftover from prior failed runs
        _api(auth_token, api_url, f"/api/boards/{board_id}/routes/{status}", "DELETE")

        # 1. Create
        resp = _api(auth_token, api_url, f"/api/boards/{board_id}/routes", "POST", {
            "status": status,
            "action": "/testgen",
            "prompt_template": "SGTEST: Generate tests for {issue_key}",
            "enabled": True,
        })
        assert resp.status_code == 201, f"Create failed: {resp.text}"
        route = resp.json()["route"]
        assert route["status"] == status
        assert route["action"] == "/testgen"
        assert route["enabled"] is True

        # 2. List — verify it's there
        resp = _api(auth_token, api_url, f"/api/boards/{board_id}/routes")
        assert resp.status_code == 200
        routes = resp.json()["routes"]
        assert any(r["status"] == status for r in routes)

        # 3. Update — set transitions
        resp = _api(auth_token, api_url, f"/api/boards/{board_id}/routes/{status}", "PUT", {
            "prompt_template": "SGTEST UPDATED: Generate tests for {issue_key}",
            "transition_on_launch": "In Progress",
            "transition_on_success": "REVIEW",
            "transition_on_failure": "Blocked",
        })
        assert resp.status_code == 200
        updated = resp.json()["route"]
        assert "UPDATED" in updated["prompt_template"]
        assert updated["transition_on_launch"] == "In Progress"
        assert updated["transition_on_success"] == "REVIEW"
        assert updated["transition_on_failure"] == "Blocked"

        # 4. Toggle — disable
        resp = _api(auth_token, api_url, f"/api/boards/{board_id}/routes/{status}/toggle", "POST")
        assert resp.status_code == 200
        assert resp.json()["enabled"] is False

        # 5. Toggle — re-enable
        resp = _api(auth_token, api_url, f"/api/boards/{board_id}/routes/{status}/toggle", "POST")
        assert resp.status_code == 200
        assert resp.json()["enabled"] is True

        # 6. Delete
        resp = _api(auth_token, api_url, f"/api/boards/{board_id}/routes/{status}", "DELETE")
        assert resp.status_code == 200

        # 7. Verify gone
        resp = _api(auth_token, api_url, f"/api/boards/{board_id}/routes")
        routes = resp.json()["routes"]
        assert not any(r["status"] == status for r in routes)

    def test_existing_droid_do_route(self, auth_token, api_url):
        """Verify the real Droid-Do route persists."""
        resp = _api(auth_token, api_url, "/api/boards/1/routes")
        routes = resp.json()["routes"]
        droid = next((r for r in routes if r["status"] == "Droid-Do"), None)
        assert droid is not None, "Droid-Do route should exist"
        assert droid["action"] == "/solve"
        assert droid["enabled"] is True


# ── Templates ───────────────────────────────────────────────

class TestTemplates:
    def test_global_templates_seeded(self, auth_token, api_url):
        resp = _api(auth_token, api_url, "/api/templates")
        assert resp.status_code == 200
        templates = resp.json()["templates"]
        names = {t["name"] for t in templates}
        assert "/solve" in names
        assert "/prd2epic" in names
        assert "/epic2stories" in names
        assert "/testgen" in names
        assert "/migrate" in names

    def test_template_has_content(self, auth_token, api_url):
        resp = _api(auth_token, api_url, "/api/templates")
        templates = resp.json()["templates"]
        solve = next(t for t in templates if t["name"] == "/solve")
        assert "Solve ticket" in solve["prompt_template"]
        assert solve["scope"] == "global"

    def test_template_crud(self, auth_token, api_url):
        # Create
        resp = _api(auth_token, api_url, "/api/templates", "POST", {
            "name": "/sgtest-template",
            "description": "SGTEST: temporary test template",
            "prompt_template": "SGTEST: do nothing for {issue_key}",
        })
        assert resp.status_code == 201
        tpl = resp.json()["template"]
        tpl_id = tpl["id"]

        # Read
        resp = _api(auth_token, api_url, f"/api/templates/{tpl_id}")
        assert resp.status_code == 200
        assert resp.json()["template"]["name"] == "/sgtest-template"

        # Update
        resp = _api(auth_token, api_url, f"/api/templates/{tpl_id}", "PUT", {
            "description": "SGTEST UPDATED",
        })
        assert resp.status_code == 200
        assert resp.json()["template"]["description"] == "SGTEST UPDATED"

        # Delete
        resp = _api(auth_token, api_url, f"/api/templates/{tpl_id}", "DELETE")
        assert resp.status_code == 200

        # Verify gone
        resp = _api(auth_token, api_url, f"/api/templates/{tpl_id}")
        assert resp.status_code == 404


# ── Edge / Team ─────────────────────────────────────────────

class TestEdgeAndTeam:
    def test_list_edge_nodes(self, auth_token, api_url):
        resp = _api(auth_token, api_url, "/api/edge/nodes")
        assert resp.status_code == 200

    def test_list_teams(self, auth_token, api_url):
        resp = _api(auth_token, api_url, "/api/teams")
        assert resp.status_code == 200
        teams = resp.json()["teams"]
        assert len(teams) >= 1


# ── Data Persistence ────────────────────────────────────────

class TestPersistence:
    """Verify that data created in one request is readable in the next."""

    def test_route_persists_across_requests(self, auth_token, api_url):
        board_id = 1
        status = "SGTEST-Persist-Check"

        # Create
        _api(auth_token, api_url, f"/api/boards/{board_id}/routes", "POST", {
            "status": status, "action": "/solve", "prompt_template": "persist test", "enabled": True,
        })

        # Fresh request — verify it's there
        resp = _api(auth_token, api_url, f"/api/boards/{board_id}/routes")
        routes = resp.json()["routes"]
        found = next((r for r in routes if r["status"] == status), None)
        assert found is not None, "Route should persist across requests"
        assert found["prompt_template"] == "persist test"

        # Clean up
        _api(auth_token, api_url, f"/api/boards/{board_id}/routes/{status}", "DELETE")


# ── Pipeline Reality Tests ─────────────────────────────────
# These tests verify the REAL pipeline configuration, not just UI rendering.
# Some are EXPECTED TO FAIL — they expose gaps between config and reality.

BOARD_ID = 1
YAML_PATH = Path(__file__).resolve().parent.parent.parent / "board-routes.yaml"


class TestHeartbeatDetectsTicket:
    """Verify the heartbeat can actually detect tickets in trigger columns."""

    def test_droid_do_route_exists_and_enabled(self, auth_token, api_url):
        """The Droid-Do route must exist and be enabled for heartbeat to fire."""
        resp = _api(auth_token, api_url, f"/api/boards/{BOARD_ID}/routes")
        assert resp.status_code == 200
        routes = resp.json()["routes"]
        droid = next((r for r in routes if r["status"] == "Droid-Do"), None)
        assert droid is not None, "Droid-Do route must exist for heartbeat to detect tickets"
        assert droid["enabled"] is True, "Droid-Do route must be enabled"

    def test_droid_do_column_exists_in_snapshot(self, auth_token, api_url):
        """The Droid-Do column must exist on the actual Jira board."""
        resp = _api(auth_token, api_url, f"/api/boards/{BOARD_ID}/snapshot")
        assert resp.status_code == 200
        columns = resp.json()["columns"]
        col_names = [c["name"] for c in columns]
        assert "Droid-Do" in col_names, "Droid-Do column must exist on the Jira board"


class TestRouteHasCompleteConfiguration:
    """A route without transitions won't move tickets through the pipeline.

    This test SHOULD FAIL if the Droid-Do route in the cloud has no
    transitions set — exposing a real gap in pipeline configuration.
    """

    def test_droid_do_has_transition_on_launch(self, auth_token, api_url):
        resp = _api(auth_token, api_url, f"/api/boards/{BOARD_ID}/routes")
        routes = resp.json()["routes"]
        droid = next((r for r in routes if r["status"] == "Droid-Do"), None)
        assert droid is not None
        val = droid.get("transition_on_launch")
        assert val and val.strip(), (
            "Droid-Do route has no transition_on_launch — tickets won't move when work starts"
        )

    def test_droid_do_has_transition_on_success(self, auth_token, api_url):
        resp = _api(auth_token, api_url, f"/api/boards/{BOARD_ID}/routes")
        routes = resp.json()["routes"]
        droid = next((r for r in routes if r["status"] == "Droid-Do"), None)
        assert droid is not None
        val = droid.get("transition_on_success")
        assert val and val.strip(), (
            "Droid-Do route has no transition_on_success — tickets won't move after completion"
        )

    def test_droid_do_has_transition_on_failure(self, auth_token, api_url):
        resp = _api(auth_token, api_url, f"/api/boards/{BOARD_ID}/routes")
        routes = resp.json()["routes"]
        droid = next((r for r in routes if r["status"] == "Droid-Do"), None)
        assert droid is not None
        val = droid.get("transition_on_failure")
        assert val and val.strip(), (
            "Droid-Do route has no transition_on_failure — failed tickets will be stuck"
        )


class TestCloudRoutesMatchYaml:
    """Cloud routes should mirror what's in board-routes.yaml.

    This test SHOULD FAIL if the cloud route has drifted from the YAML —
    meaning the local config and the cloud API are out of sync.
    """

    @pytest.fixture()
    def yaml_routes(self):
        assert YAML_PATH.exists(), f"board-routes.yaml not found at {YAML_PATH}"
        data = yaml.safe_load(YAML_PATH.read_text())
        return {r["status"]: r for r in data.get("routes", [])}

    @pytest.fixture()
    def cloud_routes(self, auth_token, api_url):
        resp = _api(auth_token, api_url, f"/api/boards/{BOARD_ID}/routes")
        assert resp.status_code == 200
        return {r["status"]: r for r in resp.json()["routes"]}

    def test_droid_do_action_matches(self, yaml_routes, cloud_routes):
        assert "Droid-Do" in yaml_routes, "Droid-Do missing from YAML"
        assert "Droid-Do" in cloud_routes, "Droid-Do missing from cloud"
        yaml_action = yaml_routes["Droid-Do"].get("action", "")
        cloud_action = cloud_routes["Droid-Do"].get("action", "")
        assert yaml_action == cloud_action, (
            f"Action mismatch — YAML: {yaml_action!r}, cloud: {cloud_action!r}"
        )

    def test_droid_do_prompt_matches(self, yaml_routes, cloud_routes):
        assert "Droid-Do" in yaml_routes and "Droid-Do" in cloud_routes
        yaml_prompt = yaml_routes["Droid-Do"].get("prompt_template", "")
        cloud_prompt = cloud_routes["Droid-Do"].get("prompt_template", "")
        assert yaml_prompt == cloud_prompt, (
            f"Prompt mismatch — YAML: {yaml_prompt!r}, cloud: {cloud_prompt!r}"
        )

    def test_droid_do_transitions_match(self, yaml_routes, cloud_routes):
        assert "Droid-Do" in yaml_routes and "Droid-Do" in cloud_routes
        for field in ("transition_on_launch", "transition_on_success", "transition_on_failure"):
            yaml_val = yaml_routes["Droid-Do"].get(field, "")
            cloud_val = cloud_routes["Droid-Do"].get(field, "")
            assert yaml_val == cloud_val, (
                f"{field} mismatch — YAML: {yaml_val!r}, cloud: {cloud_val!r}"
            )

    def test_all_yaml_routes_exist_in_cloud(self, yaml_routes, cloud_routes):
        missing = set(yaml_routes.keys()) - set(cloud_routes.keys())
        assert not missing, f"Routes in YAML but not in cloud: {missing}"


class TestTemplateResolution:
    """Verify the template resolution endpoint returns usable prompts."""

class TestHeartbeatCloudIntegration:
    """Verify cloud routes are consumable by the local heartbeat."""

    def test_cloud_config_module_exists(self):
        from swarmgrid.cloud_config import fetch_cloud_routes
        assert callable(fetch_cloud_routes)

    def test_cloud_routes_have_required_fields(self, auth_token, api_url):
        resp = _api(auth_token, api_url, f"/api/boards/{BOARD_ID}/routes")
        routes = resp.json()["routes"]
        for r in routes:
            assert r.get("status"), "Route missing status"
            assert r.get("action"), "Route missing action"
            assert r.get("prompt_template"), f"Route '{r['status']}' has empty prompt_template"
            assert "enabled" in r

    def test_api_key_endpoint(self, auth_token, api_url):
        resp = _api(auth_token, api_url, "/api/auth/api-key")
        assert resp.status_code == 200
        data = resp.json()
        assert data["api_key"].startswith("ey")
        assert data["expires_in_days"] == 30

    def test_snapshot_includes_agent_status(self, auth_token, api_url):
        resp = _api(auth_token, api_url, f"/api/boards/{BOARD_ID}/snapshot")
        assert resp.status_code == 200
        all_tickets = [t for col in resp.json()["columns"] for t in col.get("tickets", [])]
        assert len(all_tickets) > 0
        for t in all_tickets:
            assert "agent_status" in t, f"Ticket {t['key']} missing agent_status field"


class TestCLICommands:
    """Verify CLI commands work correctly with cloud routes."""

    def test_status_shows_cloud_routes(self):
        import subprocess as _sp
        result = _sp.run(
            [".venv/bin/swarmgrid", "status"],
            cwd="/Users/t/clients/swarmgrid",
            capture_output=True, text=True, timeout=30,
        )
        # Status output has human-readable header then JSON starting with {
        stdout = result.stdout
        json_start = stdout.index("{")
        import json as _j
        data = _j.loads(stdout[json_start:])
        assert data.get("route_source") == "cloud"
        routes = data.get("routes", [])
        droid = next((r for r in routes if r["status"] == "Droid-Do"), None)
        assert droid is not None
        assert droid["enabled"] is True
        assert droid["transition_on_launch"] == "In Progress"
        # Also verify the human-readable header
        header = stdout[:json_start]
        assert "Heartbeat daemon" in header
        assert "Route source: cloud" in header

    def test_stop_command(self):
        """swarmgrid stop should work without error. Restarts heartbeat if it was running."""
        import subprocess as _sp
        # Check if heartbeat was running before test
        was_running = _sp.run(
            ["tmux", "has-session", "-t", "swarmgrid-heartbeat"],
            check=False, capture_output=True,
        ).returncode == 0

        result = _sp.run(
            [".venv/bin/swarmgrid", "stop"],
            cwd="/Users/t/clients/swarmgrid",
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0
        assert "stopped" in result.stdout.lower() or "no heartbeat" in result.stdout.lower()

        # Restore heartbeat if it was running
        if was_running:
            _sp.run(
                [".venv/bin/swarmgrid", "heartbeat", "--background"],
                cwd="/Users/t/clients/swarmgrid",
                capture_output=True, text=True, timeout=10,
            )

    def test_background_heartbeat(self):
        """swarmgrid heartbeat --background should launch a tmux session."""
        import subprocess as _sp
        # Start background
        result = _sp.run(
            [".venv/bin/swarmgrid", "heartbeat", "--background"],
            cwd="/Users/t/clients/swarmgrid",
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0
        assert "swarmgrid-heartbeat" in result.stdout
        import time; time.sleep(3)
        # Verify tmux session exists
        check = _sp.run(["tmux", "has-session", "-t", "swarmgrid-heartbeat"], check=False, capture_output=True)
        assert check.returncode == 0, "Background heartbeat tmux session should be running"

    def test_heartbeat_once_runs(self):
        """heartbeat-once should complete and use cloud routes."""
        import subprocess as _sp
        result = _sp.run(
            [".venv/bin/swarmgrid", "heartbeat-once"],
            cwd="/Users/t/clients/swarmgrid",
            capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0, f"heartbeat-once failed: {result.stderr}"
        import json as _j
        data = _j.loads(result.stdout)
        assert "Droid-Do" in data["watched_statuses"]
        assert "issue_count" in data
        assert "launched_count" in data


class TestTemplateResolution:
    def test_resolve_solve_template(self, auth_token, api_url):
        resp = _api(auth_token, api_url, f"/api/templates/resolve/{BOARD_ID}//solve")
        assert resp.status_code == 200, f"Template resolve failed: {resp.status_code} {resp.text}"
        data = resp.json()
        template = data.get("template", data)
        prompt = template.get("prompt_template", "")
        assert prompt and len(prompt.strip()) > 0, (
            "Resolved /solve template has empty prompt_template"
        )
