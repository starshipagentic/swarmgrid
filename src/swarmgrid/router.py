from __future__ import annotations

from .config import AppConfig, RouteSettings
from .models import JiraIssue, RouteDecision


def evaluate_route(
    issue: JiraIssue,
    previous_state: dict | None,
    latest_decision: dict | None,
    route: RouteSettings,
    config: AppConfig,
    force_reconsider: bool = False,
) -> RouteDecision:
    previous_status = previous_state["status_name"] if previous_state else None
    first_seen = previous_state is None
    entered_status = previous_status != issue.status_name
    latest_reason = latest_decision["reason"] if latest_decision else None

    prompt = route.prompt_template.format(
        issue_key=issue.key,
        summary=issue.summary,
        status=issue.status_name,
        issue_type=issue.issue_type,
    )
    if first_seen and not route.fire_on_first_seen:
        return RouteDecision(
            issue_key=issue.key,
            status_name=issue.status_name,
            action=route.action,
            prompt=prompt,
            should_launch=False,
            reason="first_seen_suppressed",
            transition_on_launch=route.transition_on_launch,
            transition_on_success=route.transition_on_success,
            transition_on_failure=route.transition_on_failure,
            comment_on_launch=route.comment_on_launch_template,
            comment_on_success=route.comment_on_success_template,
            comment_on_failure=route.comment_on_failure_template,
            artifact_globs=_render_artifact_globs(route.artifact_globs, issue, prompt),
        )

    if not route.enabled:
        return RouteDecision(
            issue_key=issue.key,
            status_name=issue.status_name,
            action=route.action,
            prompt=prompt,
            should_launch=False,
            reason="route_disabled",
            transition_on_launch=route.transition_on_launch,
            transition_on_success=route.transition_on_success,
            transition_on_failure=route.transition_on_failure,
            comment_on_launch=route.comment_on_launch_template,
            comment_on_success=route.comment_on_success_template,
            comment_on_failure=route.comment_on_failure_template,
            artifact_globs=_render_artifact_globs(route.artifact_globs, issue, prompt),
        )

    if route.allowed_issue_types and issue.issue_type not in route.allowed_issue_types:
        return RouteDecision(
            issue_key=issue.key,
            status_name=issue.status_name,
            action=route.action,
            prompt=prompt,
            should_launch=False,
            reason=f"unsupported_issue_type:{issue.issue_type}",
            transition_on_launch=route.transition_on_launch,
            transition_on_success=route.transition_on_success,
            transition_on_failure=route.transition_on_failure,
            comment_on_launch=route.comment_on_launch_template,
            comment_on_success=route.comment_on_success_template,
            comment_on_failure=route.comment_on_failure_template,
            artifact_globs=_render_artifact_globs(route.artifact_globs, issue, prompt),
        )

    if not config.llm.enabled:
        return RouteDecision(
            issue_key=issue.key,
            status_name=issue.status_name,
            action=route.action,
            prompt=prompt,
            should_launch=False,
            reason="llm_disabled",
            transition_on_launch=route.transition_on_launch,
            transition_on_success=route.transition_on_success,
            transition_on_failure=route.transition_on_failure,
            comment_on_launch=route.comment_on_launch_template,
            comment_on_success=route.comment_on_success_template,
            comment_on_failure=route.comment_on_failure_template,
            artifact_globs=_render_artifact_globs(route.artifact_globs, issue, prompt),
        )

    if config.llm.dry_run:
        return RouteDecision(
            issue_key=issue.key,
            status_name=issue.status_name,
            action=route.action,
            prompt=prompt,
            should_launch=False,
            reason="dry_run",
            transition_on_launch=route.transition_on_launch,
            transition_on_success=route.transition_on_success,
            transition_on_failure=route.transition_on_failure,
            comment_on_launch=route.comment_on_launch_template,
            comment_on_success=route.comment_on_success_template,
            comment_on_failure=route.comment_on_failure_template,
            artifact_globs=_render_artifact_globs(route.artifact_globs, issue, prompt),
        )

    if (
        not force_reconsider
        and not entered_status
        and not _should_reconsider(latest_reason, issue, route, config)
    ):
        return RouteDecision(
            issue_key=issue.key,
            status_name=issue.status_name,
            action=route.action,
            prompt=prompt,
            should_launch=False,
            reason="status_unchanged",
            transition_on_launch=route.transition_on_launch,
            transition_on_success=route.transition_on_success,
            transition_on_failure=route.transition_on_failure,
            comment_on_launch=route.comment_on_launch_template,
            comment_on_success=route.comment_on_success_template,
            comment_on_failure=route.comment_on_failure_template,
            artifact_globs=_render_artifact_globs(route.artifact_globs, issue, prompt),
        )

    return RouteDecision(
        issue_key=issue.key,
        status_name=issue.status_name,
        action=route.action,
        prompt=prompt,
        should_launch=True,
        reason="ready_to_launch",
        transition_on_launch=route.transition_on_launch,
        transition_on_success=route.transition_on_success,
        transition_on_failure=route.transition_on_failure,
        comment_on_launch=route.comment_on_launch_template,
        comment_on_success=route.comment_on_success_template,
        comment_on_failure=route.comment_on_failure_template,
        artifact_globs=_render_artifact_globs(route.artifact_globs, issue, prompt),
    )


def _render_artifact_globs(patterns: list[str], issue: JiraIssue, prompt: str) -> list[str]:
    context = {
        "issue_key": issue.key,
        "summary": issue.summary,
        "status": issue.status_name,
        "issue_type": issue.issue_type,
        "prompt": prompt,
    }
    return [pattern.format(**context) for pattern in patterns]


def _should_reconsider(
    latest_reason: str | None,
    issue: JiraIssue,
    route: RouteSettings,
    config: AppConfig,
) -> bool:
    if latest_reason is None:
        return False
    if latest_reason == "ready_to_launch":
        return True
    if latest_reason == "route_disabled" and route.enabled:
        return True
    if latest_reason == "llm_disabled" and route.enabled and config.llm.enabled:
        return True
    if latest_reason == "dry_run" and route.enabled and config.llm.enabled and not config.llm.dry_run:
        return True
    if latest_reason.startswith("unsupported_issue_type:"):
        return bool(route.allowed_issue_types) and issue.issue_type in route.allowed_issue_types
    return False
