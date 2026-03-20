from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class JiraIssue:
    key: str
    summary: str
    issue_type: str
    status_name: str
    status_id: str
    updated: str
    assignee: str | None
    browse_url: str
    parent_key: str | None = None
    parent_issue_type: str | None = None
    parent_summary: str | None = None
    epic_story_count: int | None = None
    labels: list[str] | None = None


@dataclass(slots=True)
class RouteDecision:
    issue_key: str
    status_name: str
    action: str
    prompt: str
    should_launch: bool
    reason: str
    transition_on_launch: str | None = None
    transition_on_success: str | None = None
    transition_on_failure: str | None = None
    comment_on_launch: str | None = None
    comment_on_success: str | None = None
    comment_on_failure: str | None = None
    artifact_globs: list[str] | None = None


@dataclass(slots=True)
class LaunchRecord:
    run_id: int | None
    issue_key: str
    status_name: str
    action: str
    prompt: str
    state: str
    pid: int | None
    log_path: str
    command_line: str
    run_dir: str
    artifact_globs: list[str]
    session_name: str | None = None
    launch_mode: str | None = None
    transition_on_launch: str | None = None
    transition_on_success: str | None = None
    transition_on_failure: str | None = None
    comment_on_launch: str | None = None
    comment_on_success: str | None = None
    comment_on_failure: str | None = None


@dataclass(slots=True)
class RunReconciliation:
    run_id: int
    issue_key: str
    state: str
    proof_files: list[str]
    log_path: str
    prompt: str
    action: str
    transition_target: str | None
    comment_body: str | None
