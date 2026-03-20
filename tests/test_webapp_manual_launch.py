from pathlib import Path

from swarmgrid.config import AppConfig, JiraActionSettings, JiraSettings, LlmSettings, RouteSettings
from swarmgrid.models import JiraIssue, LaunchRecord
from swarmgrid.webapp import manual_launch_issue


class DummyStore:
    def __init__(self) -> None:
        self.upserts: list[tuple[str, str]] = []

    def upsert_issue_state(self, issue: JiraIssue, seen_at: str) -> None:
        self.upserts.append((issue.key, seen_at))


class DummyJira:
    def __init__(self, issue: JiraIssue | None) -> None:
        self.issue = issue

    def fetch_issue(self, issue_key: str) -> JiraIssue | None:
        if self.issue and self.issue.key == issue_key:
            return self.issue
        return None


def make_config(*, status: str = "Todo", route_enabled: bool = False) -> AppConfig:
    return AppConfig(
        config_path=Path("board-routes.yaml"),
        site_url="https://example.atlassian.net",
        project_key="PROJ",
        board_id="1183",
        board_map_path=Path("project.jira-map.yaml"),
        operator_settings_path=Path("operator-settings.yaml"),
        poll_interval_minutes=5,
        local_state_dir=Path("var/heartbeat"),
        jira=JiraSettings(
            email_env="ATLASSIAN_EMAIL",
            token_env="ATLASSIAN_TOKEN",
            token_file="~/.atlassian-token",
        ),
        llm=LlmSettings(
            command="claude",
            args=["-p", "{prompt}"],
            working_dir="/tmp",
            enabled=True,
            dry_run=False,
            max_parallel=1,
        ),
        jira_actions=JiraActionSettings(enabled=False),
        routes=[
            RouteSettings(
                status=status,
                action="claude_solve2",
                prompt_template="/solve2 {issue_key}",
                enabled=route_enabled,
                allowed_issue_types=["Task", "Story"],
                transition_on_launch="In Progress",
                transition_on_success="REVIEW",
                transition_on_failure="Blocked",
            )
        ],
    )


def make_issue(*, key: str = "PROJ-123", status: str = "Todo") -> JiraIssue:
    return JiraIssue(
        key=key,
        summary="Fix search launch",
        issue_type="Task",
        status_name=status,
        status_id="10001",
        updated="2026-03-17T16:30:00.000+0000",
        assignee=None,
        browse_url=f"https://example.atlassian.net/browse/{key}",
        labels=[],
    )


def make_launch(issue: JiraIssue, action: str) -> LaunchRecord:
    return LaunchRecord(
        run_id=1,
        issue_key=issue.key,
        status_name=issue.status_name,
        action=action,
        prompt="",
        state="running",
        pid=1234,
        log_path="/tmp/command.log",
        command_line="cmd",
        run_dir="/tmp/run",
        artifact_globs=[],
        session_name=f"swarmgrid-{issue.key.lower()}",
        launch_mode="tmux",
    )


def test_manual_launch_uses_route_for_trigger_status(monkeypatch):
    config = make_config(route_enabled=False)
    issue = make_issue(status="Todo")
    store = DummyStore()
    launch = make_launch(issue, "claude_solve2")
    calls: list[str] = []

    monkeypatch.setattr("swarmgrid.webapp.load_config", lambda _: config)
    monkeypatch.setattr("swarmgrid.webapp.StateStore", lambda _: store)
    monkeypatch.setattr("swarmgrid.webapp.JiraClient", lambda _: DummyJira(issue))
    monkeypatch.setattr("swarmgrid.webapp._pre_launch_transition", lambda *_: calls.append("pre"))
    monkeypatch.setattr("swarmgrid.webapp.launch_decision", lambda *_: launch)
    monkeypatch.setattr("swarmgrid.webapp._apply_launch_side_effects", lambda *_: calls.append("post"))
    monkeypatch.setattr(
        "swarmgrid.webapp.launch_manual_tmux_shell",
        lambda *_: (_ for _ in ()).throw(AssertionError("plain tmux should not launch")),
    )

    result = manual_launch_issue("board-routes.yaml", issue.key)

    assert result["ok"] is True
    assert result["mode"] == "route"
    assert result["launch"]["action"] == "claude_solve2"
    assert calls == ["pre", "post"]
    assert len(store.upserts) == 1


def test_manual_launch_falls_back_to_plain_tmux_for_non_route_status(monkeypatch):
    config = make_config()
    issue = make_issue(status="QA")
    store = DummyStore()
    launch = make_launch(issue, "manual_tmux_shell")
    calls: list[str] = []

    monkeypatch.setattr("swarmgrid.webapp.load_config", lambda _: config)
    monkeypatch.setattr("swarmgrid.webapp.StateStore", lambda _: store)
    monkeypatch.setattr("swarmgrid.webapp.JiraClient", lambda _: DummyJira(issue))
    monkeypatch.setattr("swarmgrid.webapp._pre_launch_transition", lambda *_: calls.append("pre"))
    monkeypatch.setattr(
        "swarmgrid.webapp.launch_decision",
        lambda *_: (_ for _ in ()).throw(AssertionError("route launch should not run")),
    )
    monkeypatch.setattr("swarmgrid.webapp.launch_manual_tmux_shell", lambda *_: launch)

    result = manual_launch_issue("board-routes.yaml", issue.key)

    assert result["ok"] is True
    assert result["mode"] == "plain_tmux"
    assert result["launch"]["action"] == "manual_tmux_shell"
    assert calls == []
    assert len(store.upserts) == 1
