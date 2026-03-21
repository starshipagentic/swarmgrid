"""E2E tests for the SwarmGrid landing page and login flow."""
import pytest
from playwright.sync_api import Page, expect


class TestLandingPage:
    def test_landing_loads(self, page: Page, site_url):
        page.goto(site_url)
        page.wait_for_load_state("networkidle")
        expect(page.locator("h1", has_text="SwarmGrid")).to_be_visible()

    def test_has_get_started_button(self, page: Page, site_url):
        page.goto(site_url)
        page.wait_for_load_state("networkidle")
        expect(page.locator("text=Get Started").first).to_be_visible()

    def test_has_login_button(self, page: Page, site_url):
        page.goto(site_url)
        page.wait_for_load_state("networkidle")
        expect(page.locator("text=Login")).to_be_visible()

    def test_has_github_link(self, page: Page, site_url):
        page.goto(site_url)
        page.wait_for_load_state("networkidle")
        expect(page.locator("text=GitHub").first).to_be_visible()

    def test_how_it_works_section(self, page: Page, site_url):
        page.goto(site_url)
        page.wait_for_load_state("networkidle")
        expect(page.locator("text=How it works")).to_be_visible()

    def test_install_command(self, page: Page, site_url):
        page.goto(site_url)
        page.wait_for_load_state("networkidle")
        page_text = page.text_content("body")
        assert "pip install swarmgrid" in page_text


class TestLoginPage:
    def test_login_page_loads(self, page: Page, site_url):
        page.goto(f"{site_url}/login.html")
        page.wait_for_load_state("networkidle")
        expect(page.locator("text=Sign in with GitHub")).to_be_visible()

    def test_login_redirects_if_authed(self, page: Page, site_url, auth_token):
        # Set auth cookie then visit login
        page.goto(f"{site_url}/dashboard.html?token={auth_token}")
        page.wait_for_load_state("networkidle")
        page.goto(f"{site_url}/login.html")
        page.wait_for_timeout(2000)
        # Should redirect to dashboard
        assert "dashboard" in page.url
