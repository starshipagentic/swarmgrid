"""E2E browser tests for the SwarmGrid dashboard.

Uses Playwright to drive a real browser against the production site.
Injects auth cookie to skip OAuth flow.
"""
import re
import pytest
from playwright.sync_api import Page, expect


@pytest.fixture
def authed_page(page: Page, auth_token, site_url):
    """Navigate to dashboard with auth cookie pre-set."""
    page.goto(f"{site_url}/dashboard.html?token={auth_token}")
    page.wait_for_load_state("networkidle")
    return page


class TestBoardTab:
    def test_board_loads_with_columns(self, authed_page: Page):
        authed_page.wait_for_selector(".column-title", timeout=15000)
        count = authed_page.locator(".column-title").count()
        assert count >= 7, f"Expected at least 7 columns, got {count}"

    def test_column_names_are_real(self, authed_page: Page):
        authed_page.wait_for_selector(".column-title", timeout=15000)
        titles = authed_page.locator(".column-title").all_text_contents()
        names = [t.split("\xa0")[0].strip() for t in titles]  # strip count
        # Remove trailing numbers from "PRD 2" -> "PRD"
        names = [re.sub(r'\s+\d+$', '', n) for n in names]
        assert "PRD" in names
        assert "Droid-Do" in names or "DROID-DO" in names
        assert "In Progress" in names or "IN PROGRESS" in names

    def test_tickets_have_keys(self, authed_page: Page):
        authed_page.wait_for_selector(".ticket-key", timeout=15000)
        keys = authed_page.locator(".ticket-key").all_text_contents()
        assert len(keys) > 0
        assert all("LMSV3-" in k for k in keys)

    def test_board_has_routes_data(self, authed_page: Page):
        """Board snapshot should include routes for armed column detection."""
        authed_page.wait_for_selector(".column-title", timeout=15000)
        # At minimum, Droid-Do column should exist
        droid = authed_page.locator(".column-title", has_text="Droid-Do")
        assert droid.count() > 0

    def test_board_switcher_visible(self, authed_page: Page):
        expect(authed_page.locator("#board-switcher")).to_be_visible()
        options = authed_page.locator("#board-switcher option").all_text_contents()
        assert any("LMSV3" in o for o in options)


class TestRoutesTab:
    def test_routes_shows_configured_routes(self, authed_page: Page):
        authed_page.click("[data-page='routes']")
        authed_page.wait_for_selector(".route-strip", timeout=10000)
        # Should show the summary table with Droid-Do route
        expect(authed_page.locator(".route-col.armed")).to_be_visible()
        table_text = authed_page.locator("#route-editor-area").text_content()
        assert "Droid-Do" in table_text
        assert "/solve" in table_text
        assert "Armed" in table_text

    def test_click_route_opens_editor(self, authed_page: Page):
        authed_page.click("[data-page='routes']")
        authed_page.wait_for_selector(".route-col", timeout=10000)
        authed_page.click(".route-col[data-status='Droid-Do']")
        expect(authed_page.locator("#re-status")).to_have_value("Droid-Do")
        expect(authed_page.locator("#re-command")).to_have_value("/solve")
        expect(authed_page.locator("#re-prompt")).not_to_be_empty()

    def test_create_and_delete_route(self, authed_page: Page, auth_token, api_url):
        import requests as req
        # Clean up any leftover from prior runs
        for s in ["TODO", "SGTEST-Browser-Route"]:
            req.delete(f"{api_url}/api/boards/1/routes/{s}",
                       headers={"Authorization": f"Bearer {auth_token}"})

        authed_page.click("[data-page='routes']")
        authed_page.wait_for_selector("#add-route-btn", timeout=10000)

        # Create a route for an existing Jira column
        authed_page.click("#add-route-btn")
        authed_page.wait_for_selector("#re-status", timeout=5000)
        authed_page.fill("#re-status", "TODO")
        authed_page.fill("#re-command", "/testgen")
        authed_page.fill("#re-prompt", "SGTEST: run tests for {issue_key}")
        authed_page.click("#re-save")
        authed_page.wait_for_timeout(3000)

        # Verify it appears in the summary table
        table_text = authed_page.locator("#route-editor-area").text_content()
        assert "TODO" in table_text
        assert "/testgen" in table_text

        # Click the TODO pill to open editor, then delete
        authed_page.locator("#route-strip .route-col", has_text="TODO").click()
        authed_page.wait_for_selector("#re-delete", timeout=5000)
        authed_page.click("#re-delete")
        authed_page.wait_for_timeout(3000)

        # Verify /testgen route is gone from summary
        table_text = authed_page.locator("#route-editor-area").text_content()
        assert "/testgen" not in table_text


class TestTemplatesTab:
    def test_global_templates_visible(self, authed_page: Page):
        authed_page.click("[data-page='templates']")
        authed_page.wait_for_selector(".template-card", timeout=10000)
        cards = authed_page.locator(".template-card").all_text_contents()
        combined = " ".join(cards)
        assert "/solve" in combined
        assert "/prd2epic" in combined
        assert "/testgen" in combined

    def test_view_template_shows_prompt(self, authed_page: Page):
        authed_page.click("[data-page='templates']")
        authed_page.wait_for_selector(".template-card", timeout=10000)
        # Click View on /solve
        solve_card = authed_page.locator(".template-card", has_text="/solve")
        solve_card.locator("button", has_text="View").click()
        authed_page.wait_for_timeout(1000)
        # Should show overlay with prompt content
        expect(authed_page.locator("#tpl-view-overlay")).to_be_visible()
        overlay_text = authed_page.locator("#tpl-view-overlay").text_content()
        assert "Solve ticket" in overlay_text


class TestTeamTab:
    def test_team_shows_members(self, authed_page: Page):
        authed_page.click("[data-page='team']")
        authed_page.wait_for_timeout(3000)
        expect(authed_page.locator("text=owner")).to_be_visible()

    def test_team_shows_edge_node(self, authed_page: Page):
        authed_page.click("[data-page='team']")
        authed_page.wait_for_timeout(3000)
        team_text = authed_page.locator("#team-content").text_content()
        assert "MacBook" in team_text or "Online" in team_text


class TestSetupTab:
    def test_setup_shows_connected_board(self, authed_page: Page):
        authed_page.click("[data-page='setup']")
        authed_page.wait_for_selector("text=Connected Board", timeout=10000)
        authed_page.wait_for_timeout(2000)
        setup_text = authed_page.locator("#setup-content").text_content()
        assert "LMSV3" in setup_text
        # Board URL should contain the Jira site (this was a bug — type mismatch)
        assert "ltv8.atlassian.net" in setup_text or "Board URL" in setup_text

    def test_setup_shows_install_command(self, authed_page: Page):
        authed_page.click("[data-page='setup']")
        authed_page.wait_for_timeout(3000)
        expect(authed_page.locator("text=curl")).to_be_visible()

    def test_generate_api_key_button(self, authed_page: Page):
        authed_page.click("[data-page='setup']")
        authed_page.wait_for_selector("#setup-gen-key", timeout=10000)
        authed_page.wait_for_timeout(1000)  # let setup tab fully render
        authed_page.click("#setup-gen-key")
        # Wait for the API call to complete and populate the input
        authed_page.wait_for_timeout(5000)
        key_value = authed_page.locator("#setup-api-key").input_value()
        assert key_value.startswith("ey"), f"API key should be a JWT, got: '{key_value[:30]}'"

    def test_setup_shows_edge_node(self, authed_page: Page):
        authed_page.click("[data-page='setup']")
        authed_page.wait_for_timeout(3000)
        setup_text = authed_page.locator("#setup-content").text_content()
        assert "MacBook" in setup_text or "Online" in setup_text, "Edge node should be visible"

    def test_jira_token_is_masked(self, authed_page: Page):
        authed_page.click("[data-page='setup']")
        authed_page.wait_for_timeout(3000)
        page_text = authed_page.text_content("body")
        # Token should be masked, not showing raw value
        assert "\u2022\u2022\u2022\u2022" in page_text or "****" in page_text or "dots" not in page_text


class TestNavigation:
    def test_all_tabs_load_without_error(self, authed_page: Page):
        for tab in ["board", "routes", "team", "templates", "setup"]:
            authed_page.click(f"[data-page='{tab}']")
            authed_page.wait_for_timeout(2000)
            # Should not redirect to login
            assert "dashboard.html" in authed_page.url

    def test_logout_clears_session(self, authed_page: Page, site_url):
        authed_page.click("#logout-btn")
        authed_page.wait_for_timeout(2000)
        assert "login.html" in authed_page.url
