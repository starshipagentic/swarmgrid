"""E2E tests for the SwarmGrid Cloud API.

These hit the REAL production API on swarmgrid-api.fly.dev.
They create, read, update, and delete real data, then clean up.
"""
import requests
import pytest


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
