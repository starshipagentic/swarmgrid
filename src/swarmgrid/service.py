from __future__ import annotations

import logging
import shutil
import time
from dataclasses import asdict
from datetime import datetime, UTC
from pathlib import Path

from .board_map import transition_id_for_status
from .cloud_config import fetch_cloud_routes
from .config import AppConfig, load_config
from .jira import JiraClient
from .models import JiraIssue, LaunchRecord, RouteDecision, RunReconciliation
from .router import evaluate_route
from .runner import (
    classify_process_row,
    launch_decision,
    max_parallel_runs,
    reconcile_finished_runs,
    reconcile_processes,
)
from .state import StateStore

logger = logging.getLogger(__name__)


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def fetch_issues(config: AppConfig, store: StateStore) -> list[JiraIssue]:
    reconcile_processes(store)
    jira = JiraClient(config)
    return jira.search_issues_by_statuses(config.watched_statuses)


def plan_decisions(
    config: AppConfig,
    store: StateStore,
    issues: list[JiraIssue],
    created_at: str,
    force_reconsider: bool = False,
) -> list[RouteDecision]:
    route_by_status = {route.status: route for route in config.routes}
    decisions: list[RouteDecision] = []

    for issue in issues:
        previous_state = store.get_issue_state(issue.key)
        route = route_by_status.get(issue.status_name)
        if route:
            active_process = store.get_active_process(issue.key, route.action)
            if active_process:
                decision = RouteDecision(
                    issue_key=issue.key,
                    status_name=issue.status_name,
                    action=route.action,
                    prompt=route.prompt_template.format(
                        issue_key=issue.key,
                        summary=issue.summary,
                        status=issue.status_name,
                        issue_type=issue.issue_type,
                    ),
                    should_launch=False,
                    reason="already_running",
                    transition_on_launch=route.transition_on_launch,
                    transition_on_success=route.transition_on_success,
                    transition_on_failure=route.transition_on_failure,
                    comment_on_launch=route.comment_on_launch_template,
                    comment_on_success=route.comment_on_success_template,
                    comment_on_failure=route.comment_on_failure_template,
                    artifact_globs=[
                        pattern.format(
                            issue_key=issue.key,
                            summary=issue.summary,
                            status=issue.status_name,
                            issue_type=issue.issue_type,
                        )
                        for pattern in route.artifact_globs
                    ],
                )
            else:
                latest_decision = store.get_latest_decision(issue.key, route.action)
                decision = evaluate_route(
                    issue,
                    previous_state,
                    latest_decision,
                    route,
                    config,
                    force_reconsider=force_reconsider,
                )
            decisions.append(decision)
            store.record_decision(
                issue_key=decision.issue_key,
                status_name=decision.status_name,
                action_name=decision.action,
                prompt=decision.prompt,
                should_launch=decision.should_launch,
                reason=decision.reason,
                created_at=created_at,
            )

        store.upsert_issue_state(issue, seen_at=created_at)

    return decisions


def launch_planned_decisions(
    config: AppConfig,
    store: StateStore,
    decisions: list[RouteDecision],
) -> list[LaunchRecord]:
    launches: list[LaunchRecord] = []
    jira = JiraClient(config)
    running_now = sum(
        1 for row in store.list_running_processes() if classify_process_row(row) == "active"
    )
    capacity = max_parallel_runs(config)
    for decision in decisions:
        if not decision.should_launch:
            continue
        if running_now >= capacity:
            break

        # --- Immediate pre-launch transition (multiuser race prevention) ---
        # This runs BEFORE the tmux/subprocess launch so that the next
        # heartbeat poll (from any developer) sees the ticket already moved
        # out of the trigger status and skips it.  This is independent of
        # jira_actions.enabled which controls the optional post-launch
        # side-effects.
        _pre_launch_transition(config, jira, decision)

        launch = launch_decision(config, store, decision)
        launches.append(launch)
        if launch.state == "running":
            running_now += 1
        _apply_launch_side_effects(config, store, jira, launch)
    return launches


def reconcile_runs(
    config: AppConfig,
    store: StateStore,
) -> list[RunReconciliation]:
    results = reconcile_finished_runs(store)
    if not results:
        return results

    jira = JiraClient(config)
    for result in results:
        _apply_final_side_effects(config, store, jira, result)
    return results


def _fetch_changelogs(config: AppConfig, store: StateStore) -> None:
    """Fetch and store Jira changelogs for tickets with active tmux sessions.

    This is lightweight and fault-tolerant: errors on individual tickets are
    logged and swallowed so they never break the heartbeat.
    """
    running = store.list_running_processes()
    if not running:
        return

    jira = JiraClient(config)
    bot_email = jira.auth_email

    # Resolve our own account ID so we can tag bot transitions
    bot_account_id: str | None = None
    try:
        myself = jira.validate_auth()
        bot_account_id = myself.get("account_id")
    except Exception:
        pass

    for row in running:
        issue_key = row.get("issue_key")
        if not issue_key:
            continue
        try:
            transitions = jira.fetch_issue_changelog(issue_key)
            for t in transitions:
                is_bot = False
                if bot_account_id and t.get("author_id") == bot_account_id:
                    is_bot = True
                t["is_bot"] = is_bot
            store.store_transitions(issue_key, transitions)
        except Exception as exc:
            logger.warning("Changelog fetch failed for %s: %s", issue_key, exc)


def _with_routes(config: AppConfig, routes: list) -> AppConfig:
    """Return a shallow copy of config with replaced routes."""
    from dataclasses import fields
    kwargs = {f.name: getattr(config, f.name) for f in fields(config)}
    kwargs["routes"] = routes
    return AppConfig(**kwargs)


def run_heartbeat(config_path: str | Path, force_reconsider: bool = False) -> dict:
    config = load_config(config_path)

    # Overlay cloud routes when available (YAML is the fallback)
    cloud_routes = fetch_cloud_routes(config)
    if cloud_routes is not None:
        config = _with_routes(config, cloud_routes)

    store = StateStore(config.local_state_dir)

    started_at = utc_now()
    issues = fetch_issues(config, store)
    archived: list[str] = []
    decisions = plan_decisions(
        config,
        store,
        issues,
        created_at=started_at,
        force_reconsider=force_reconsider,
    )
    launches = launch_planned_decisions(config, store, decisions)
    reconciled = reconcile_runs(config, store)

    # Fetch changelogs for active sessions (fault-tolerant)
    try:
        _fetch_changelogs(config, store)
    except Exception as exc:
        logger.warning("Changelog pass failed: %s", exc)

    finished_at = utc_now()

    store.record_tick(
        started_at=started_at,
        finished_at=finished_at,
        issue_count=len(issues),
        decision_count=len(decisions),
        launched_count=len(launches),
    )

    return {
        "started_at": started_at,
        "finished_at": finished_at,
        "issue_count": len(issues),
        "decision_count": len(decisions),
        "launched_count": len(launches),
        "watched_statuses": config.watched_statuses,
        "issues": [asdict(issue) for issue in issues],
        "archived": archived,
        "decisions": [asdict(decision) for decision in decisions],
        "launches": [asdict(launch) for launch in launches],
        "reconciled": [asdict(result) for result in reconciled],
        "state": store.summarize(),
        "force_reconsider": force_reconsider,
    }


def get_status(config_path: str | Path) -> dict:
    config = load_config(config_path)
    store = StateStore(config.local_state_dir)
    reconcile_processes(store)
    summary = store.summarize()
    summary["watched_statuses"] = config.watched_statuses
    summary["local_state_dir"] = str(config.local_state_dir)
    summary["recent_runs"] = store.list_recent_process_runs()
    summary["archived_count"] = len(store.list_archived_processes(limit=200))
    return summary


def heartbeat_status(config: AppConfig, store: StateStore) -> dict:
    """Return a diagnostic dict about the heartbeat system's health.

    Shared backend logic — both dagster and webapp can call this.
    Never raises; every section catches its own exceptions.
    """
    result: dict = {
        "heartbeat_source": "web",
        "dagster_sentinel_age": None,
        "tmux_available": shutil.which("tmux") is not None,
        "running_sessions": 0,
        "max_parallel": 1,
        "capacity_available": True,
        "trigger_tickets_waiting": 0,
        "stuck_tickets": [],
        "enabled_routes": sum(1 for r in config.routes if r.enabled),
    }

    # 1. Dagster sentinel detection
    try:
        sentinel = Path(__file__).resolve().parents[2] / "var" / "dagster" / "daemon_active"
        if sentinel.exists():
            age = round(time.time() - sentinel.stat().st_mtime, 1)
            result["dagster_sentinel_age"] = age
            if age < 600:
                result["heartbeat_source"] = "dagster"
    except OSError:
        pass

    # 2. Running sessions count
    try:
        running_rows = store.list_running_processes()
        active_count = sum(
            1 for row in running_rows if classify_process_row(row) == "active"
        )
        result["running_sessions"] = active_count
    except Exception as exc:
        logger.warning("heartbeat_status: running count failed: %s", exc)

    # 3. Max parallel capacity
    try:
        capacity = max_parallel_runs(config)
        result["max_parallel"] = capacity
        result["capacity_available"] = result["running_sessions"] < capacity
    except Exception as exc:
        logger.warning("heartbeat_status: max_parallel failed: %s", exc)

    # 4. Trigger tickets waiting and stuck tickets
    try:
        jira = JiraClient(config)
        issues = jira.search_issues_by_statuses(config.watched_statuses)
        created_at = utc_now()
        decisions = plan_decisions(
            config, store, issues, created_at=created_at,
        )
        # Running issue keys (so we know which have been picked up)
        running_keys = set()
        try:
            for row in store.list_running_processes():
                k = row.get("issue_key")
                if k:
                    running_keys.add(k)
        except Exception:
            pass

        waiting = 0
        stuck: list[str] = []
        poll_interval_sec = config.poll_interval_minutes * 60
        stuck_threshold = poll_interval_sec * 2

        for d in decisions:
            if d.should_launch and d.issue_key not in running_keys:
                waiting += 1

        # Check for tickets sitting in trigger statuses too long
        trigger_statuses = set(config.watched_statuses)
        now = time.time()
        for issue in issues:
            if issue.status_name not in trigger_statuses:
                continue
            # Check if this ticket has a running session — if so, not stuck
            if issue.key in running_keys:
                continue
            # Use the issue_state last_seen_at to gauge how long it's been visible
            try:
                issue_state = store.get_issue_state(issue.key)
                if issue_state:
                    first_seen = issue_state.get("first_seen_at") or issue_state.get("last_seen_at")
                    if first_seen:
                        from datetime import datetime as _dt
                        seen_dt = _dt.fromisoformat(first_seen)
                        age_sec = now - seen_dt.timestamp()
                        if age_sec > stuck_threshold:
                            stuck.append(issue.key)
            except Exception:
                pass

        result["trigger_tickets_waiting"] = waiting
        result["stuck_tickets"] = stuck
    except Exception as exc:
        logger.warning("heartbeat_status: ticket analysis failed: %s", exc)

    return result


def archive_done_processes(config: AppConfig, store: StateStore) -> list[str]:
    return []


def _pre_launch_transition(
    config: AppConfig,
    jira: JiraClient,
    decision: RouteDecision,
) -> None:
    """Transition the Jira ticket BEFORE launching the command.

    This is the critical multiuser race-prevention step.  By moving the
    ticket out of the trigger status (e.g. "Droid-Do" -> "In Progress")
    before starting the tmux session, we ensure that a second heartbeat
    instance polling at the same time will see the ticket already in
    "In Progress" and skip it.

    This runs regardless of ``config.jira_actions.enabled`` because it
    is about correctness, not optional Jira commentary.

    On failure we log and continue — we never block a launch because of
    a Jira API hiccup.
    """
    if not decision.transition_on_launch:
        return

    try:
        transition_id = transition_id_for_status(
            config.board_map_path, decision.transition_on_launch
        )
        if not transition_id:
            logger.warning(
                "No transition ID found for status %r — skipping pre-launch transition for %s",
                decision.transition_on_launch,
                decision.issue_key,
            )
            return

        logger.info(
            "Pre-launch transition: %s -> %s (transition_id=%s)",
            decision.issue_key,
            decision.transition_on_launch,
            transition_id,
        )
        jira.transition_issue(decision.issue_key, transition_id)

        # Add a comment noting the launch if the route has a template
        if decision.comment_on_launch:
            try:
                comment_text = _format_comment(
                    decision.comment_on_launch,
                    issue_key=decision.issue_key,
                    action=decision.action,
                    prompt=decision.prompt,
                    next_status=decision.transition_on_success or "",
                    proof_summary="(pending)",
                    log_path="(pending)",
                    run_dir="(pending)",
                )
                jira.add_comment(decision.issue_key, comment_text)
            except Exception as comment_exc:
                logger.warning(
                    "Pre-launch comment failed for %s: %s",
                    decision.issue_key,
                    comment_exc,
                )

        # Brief settle time for Jira to propagate the status change
        time.sleep(0.5)
    except Exception as exc:
        logger.warning(
            "Pre-launch Jira transition failed for %s: %s — launching anyway",
            decision.issue_key,
            exc,
        )


def _apply_launch_side_effects(
    config: AppConfig,
    store: StateStore,
    jira: JiraClient,
    launch: LaunchRecord,
) -> None:
    """Apply optional post-launch Jira side-effects.

    The transition and launch comment are now handled by
    ``_pre_launch_transition`` (which runs unconditionally for race
    prevention).  This function only marks the database rows so we
    know the Jira updates were applied.  If ``jira_actions.enabled``
    is True we still record it; otherwise we skip entirely.
    """
    if not config.jira_actions.enabled:
        # Even though jira_actions is disabled, the pre-launch transition
        # already fired.  Mark the DB so we don't retry.
        if launch.run_id is not None and launch.transition_on_launch:
            store.mark_jira_launch_updates_applied(launch.run_id, utc_now())
        return

    try:
        # Transition + comment already sent by _pre_launch_transition.
        # Just mark the DB.
        if launch.run_id is not None:
            store.mark_jira_launch_updates_applied(launch.run_id, utc_now())
    except Exception as exc:
        if launch.run_id is not None:
            store.update_process_state(
                launch.run_id,
                launch.state,
                utc_now(),
                jira_last_error=str(exc),
            )


def _apply_final_side_effects(
    config: AppConfig,
    store: StateStore,
    jira: JiraClient,
    result: RunReconciliation,
) -> None:
    if not config.jira_actions.enabled:
        return

    try:
        if result.transition_target:
            transition_id = transition_id_for_status(
                config.board_map_path, result.transition_target
            )
            if transition_id:
                jira.transition_issue(result.issue_key, transition_id)
        if result.comment_body:
            jira.add_comment(
                result.issue_key,
                _format_comment(
                    result.comment_body,
                    issue_key=result.issue_key,
                    action=result.action,
                    prompt=result.prompt,
                    next_status=result.transition_target or "",
                    proof_summary=", ".join(result.proof_files) if result.proof_files else "(no proof found)",
                    log_path=result.log_path,
                    run_dir=str(Path(result.log_path).parent),
                ),
            )
        store.mark_jira_final_updates_applied(result.run_id, utc_now())
    except Exception as exc:
        store.update_process_state(
            result.run_id,
            result.state,
            utc_now(),
            artifact_paths=result.proof_files,
            jira_last_error=str(exc),
        )


def _format_comment(template: str, **context: str) -> str:
    return template.format(**context)
