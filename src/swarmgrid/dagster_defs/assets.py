"""Dagster assets for the heartbeat pipeline.

Each asset materializes a logical step of the heartbeat cycle and
stores rich metadata so the Dagster UI shows useful info per block.

NOTE: ``from __future__ import annotations`` is intentionally omitted.
Dagster's context-type validation uses ``get_type_hints()`` at decoration
time and breaks under PEP 563 deferred annotations on Python 3.14.
"""
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import Any

from dagster import (
    AssetExecutionContext,
    MetadataValue,
    Output,
    asset,
)

from .resources import HeartbeatConfigResource


SENTINEL_PATH = Path(__file__).resolve().parents[3] / "var" / "dagster" / "daemon_active"


def _touch_sentinel() -> None:
    """Update the sentinel file so the web server knows dagster is driving."""
    SENTINEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    SENTINEL_PATH.write_text("active\n", encoding="utf-8")


def _md_table(headers: list[str], rows: list[list[str]]) -> MetadataValue:
    """Build a markdown table for dagster metadata display."""
    lines = []
    lines.append("| " + " | ".join(headers) + " |")
    lines.append("| " + " | ".join("---" for _ in headers) + " |")
    for row in rows:
        lines.append("| " + " | ".join(str(c) for c in row) + " |")
    return MetadataValue.md("\n".join(lines))


# ---------------------------------------------------------------------------
# Asset: jira_issues
# ---------------------------------------------------------------------------
@asset(
    description="Fetch Jira issues from all display statuses.",
    group_name="heartbeat",
    compute_kind="jira",
)
def jira_issues(context: AssetExecutionContext, config_resource: HeartbeatConfigResource) -> Output[list[dict]]:
    from ..config import load_config
    from ..jira import JiraClient
    from ..runner import reconcile_processes
    from ..state import StateStore

    _touch_sentinel()
    config_path = config_resource.effective_path()
    config = load_config(config_path)
    store = StateStore(config.local_state_dir)
    reconcile_processes(store)

    jira = JiraClient(config)
    statuses = config.display_statuses
    issues = jira.search_issues_by_statuses(statuses)
    context.log.info(f"Fetched {len(issues)} issues from Jira (statuses: {', '.join(statuses)})")

    issue_dicts = [asdict(i) for i in issues]

    # Status breakdown
    status_counts = Counter(i.status_name for i in issues)
    status_rows = [[s, str(c)] for s, c in sorted(status_counts.items(), key=lambda x: -x[1])]

    # Issue table (top 25)
    issue_rows = [[i.key, i.issue_type, i.status_name, (i.summary or "")[:60]] for i in issues[:25]]
    truncated = f" (showing 25 of {len(issues)})" if len(issues) > 25 else ""

    return Output(
        issue_dicts,
        metadata={
            "issue_count": MetadataValue.int(len(issues)),
            "statuses_queried": MetadataValue.text(", ".join(statuses)),
            "status_breakdown": _md_table(["Status", "Count"], status_rows),
            f"tickets{truncated}": _md_table(["Key", "Type", "Status", "Summary"], issue_rows),
            "config_path": MetadataValue.path(config_path),
        },
    )


# ---------------------------------------------------------------------------
# Asset: heartbeat_decisions
# ---------------------------------------------------------------------------
@asset(
    description="Plan decisions for each fetched issue (launch / skip / wait).",
    group_name="heartbeat",
    deps=[jira_issues],
    compute_kind="python",
)
def heartbeat_decisions(
    context: AssetExecutionContext,
    config_resource: HeartbeatConfigResource,
) -> Output[list[dict]]:
    from ..config import load_config
    from ..jira import JiraClient
    from ..service import plan_decisions, utc_now
    from ..state import StateStore

    config_path = config_resource.effective_path()
    config = load_config(config_path)
    store = StateStore(config.local_state_dir)

    jira = JiraClient(config)
    raw_issues = jira.search_issues_by_statuses(config.display_statuses)

    created_at = utc_now()
    decisions = plan_decisions(config, store, raw_issues, created_at=created_at)

    launch_count = sum(1 for d in decisions if d.should_launch)
    skip_count = len(decisions) - launch_count
    context.log.info(f"Planned {len(decisions)} decisions: {launch_count} to launch, {skip_count} to skip")

    decision_dicts = [asdict(d) for d in decisions]

    # Decision table
    decision_rows = []
    for d in decisions:
        action = "LAUNCH" if d.should_launch else "skip"
        reason = getattr(d, "reason", "") or getattr(d, "skip_reason", "") or ""
        decision_rows.append([d.issue_key, action, d.route_action or "-", str(reason)[:50]])

    return Output(
        decision_dicts,
        metadata={
            "decision_count": MetadataValue.int(len(decisions)),
            "launch_count": MetadataValue.int(launch_count),
            "skip_count": MetadataValue.int(skip_count),
            "decisions": _md_table(["Ticket", "Action", "Route", "Reason"], decision_rows),
        },
    )


# ---------------------------------------------------------------------------
# Asset: heartbeat_launches
# ---------------------------------------------------------------------------
@asset(
    description="Execute planned launches (with pre-launch Jira transition for race prevention).",
    group_name="heartbeat",
    deps=[heartbeat_decisions],
    compute_kind="tmux",
)
def heartbeat_launches(
    context: AssetExecutionContext,
    config_resource: HeartbeatConfigResource,
) -> Output[list[dict]]:
    from ..config import load_config
    from ..jira import JiraClient
    from ..service import (
        launch_planned_decisions,
        plan_decisions,
        utc_now,
    )
    from ..state import StateStore

    _touch_sentinel()
    config_path = config_resource.effective_path()
    config = load_config(config_path)
    store = StateStore(config.local_state_dir)

    jira = JiraClient(config)
    raw_issues = jira.search_issues_by_statuses(config.display_statuses)
    created_at = utc_now()
    decisions = plan_decisions(config, store, raw_issues, created_at=created_at)

    launches = launch_planned_decisions(config, store, decisions)

    context.log.info(f"Launched {len(launches)} processes")

    launch_dicts = [asdict(l) for l in launches]

    # Launch table
    launch_rows = []
    for l in launches:
        state = getattr(l, "state", "unknown")
        session = getattr(l, "session_name", "-") or "-"
        launch_rows.append([l.issue_key, state, session[:40]])

    # Running sessions summary
    running = store.list_running_processes()
    session_rows = []
    for r in running[:20]:
        from ..runner import classify_process_row
        mode = classify_process_row(r)
        session_rows.append([r.get("issue_key", "?"), mode, (r.get("session_name", "") or "")[:40]])

    _touch_sentinel()
    return Output(
        launch_dicts,
        metadata={
            "launched_count": MetadataValue.int(len(launches)),
            "active_sessions": MetadataValue.int(len(running)),
            "launches": _md_table(["Ticket", "State", "Session"], launch_rows) if launch_rows else MetadataValue.text("No new launches"),
            "all_sessions": _md_table(["Ticket", "Mode", "Session"], session_rows) if session_rows else MetadataValue.text("No active sessions"),
        },
    )


# ---------------------------------------------------------------------------
# Asset: process_reconciliation
# ---------------------------------------------------------------------------
@asset(
    description="Reconcile tmux sessions and finished runs.",
    group_name="heartbeat",
    compute_kind="python",
)
def process_reconciliation(
    context: AssetExecutionContext,
    config_resource: HeartbeatConfigResource,
) -> Output[list[dict]]:
    from ..config import load_config
    from ..runner import classify_process_row, reconcile_processes
    from ..service import reconcile_runs
    from ..state import StateStore

    _touch_sentinel()
    config_path = config_resource.effective_path()
    config = load_config(config_path)
    store = StateStore(config.local_state_dir)

    reconcile_processes(store)
    reconciled = reconcile_runs(config, store)

    # Current session status
    running = store.list_running_processes()
    mode_counts = Counter(classify_process_row(r) for r in running)
    mode_rows = [[m, str(c)] for m, c in sorted(mode_counts.items(), key=lambda x: -x[1])]

    # Session detail table
    session_rows = []
    for r in running[:25]:
        mode = classify_process_row(r)
        session_rows.append([r.get("issue_key", "?"), mode, (r.get("session_name", "") or "")[:40]])

    context.log.info(f"Reconciliation done: {len(reconciled)} finalized, {len(running)} active sessions")
    _touch_sentinel()

    result_dicts = [asdict(r) for r in reconciled]
    return Output(
        result_dicts,
        metadata={
            "reconciled_count": MetadataValue.int(len(reconciled)),
            "total_sessions": MetadataValue.int(len(running)),
            "session_modes": _md_table(["Mode", "Count"], mode_rows) if mode_rows else MetadataValue.text("No sessions"),
            "sessions": _md_table(["Ticket", "Mode", "Session"], session_rows) if session_rows else MetadataValue.text("No sessions"),
        },
    )


# ---------------------------------------------------------------------------
# Asset: ticket_changelogs
# ---------------------------------------------------------------------------
@asset(
    description="Fetch Jira changelogs for tickets with active tmux sessions.",
    group_name="heartbeat",
    compute_kind="jira",
)
def ticket_changelogs(
    context: AssetExecutionContext,
    config_resource: HeartbeatConfigResource,
) -> Output[dict]:
    from ..config import load_config
    from ..service import _fetch_changelogs
    from ..state import StateStore

    config_path = config_resource.effective_path()
    config = load_config(config_path)
    store = StateStore(config.local_state_dir)

    running = store.list_running_processes()
    ticket_keys = [r.get("issue_key", "?") for r in running]

    try:
        _fetch_changelogs(config, store)
        context.log.info(f"Fetched changelogs for {len(running)} active tickets")
        status = "ok"
    except Exception as exc:
        context.log.warning(f"Changelog pass failed: {exc}")
        status = f"error: {exc}"

    # Read back stored transitions to show in metadata
    timeline_rows = []
    for key in ticket_keys[:15]:
        transitions = store.get_transitions(key)
        for t in transitions[-3:]:  # last 3 transitions per ticket
            who = t.get("author", "?")
            bot = " (bot)" if t.get("is_bot") else ""
            timeline_rows.append([
                key,
                t.get("timestamp", "?")[-8:],  # just the time part
                f"{t.get('from_status', '?')} → {t.get('to_status', '?')}",
                f"{who}{bot}",
            ])

    return Output(
        {"ticket_count": len(running), "status": status},
        metadata={
            "ticket_count": MetadataValue.int(len(running)),
            "status": MetadataValue.text(status),
            "tickets_tracked": MetadataValue.text(", ".join(ticket_keys[:20]) or "none"),
            "recent_transitions": _md_table(["Ticket", "Time", "Transition", "By"], timeline_rows) if timeline_rows else MetadataValue.text("No transitions recorded"),
        },
    )


# ---------------------------------------------------------------------------
# Multi-board asset factory
# ---------------------------------------------------------------------------
def build_board_assets(board_name: str, config_path: str) -> list:
    """Build a set of prefixed assets for a specific board configuration."""
    prefix = board_name.replace("-", "_").replace(".", "_").lower()

    @asset(
        name=f"{prefix}_jira_issues",
        description=f"[{board_name}] Fetch Jira issues.",
        group_name=f"board_{prefix}",
        compute_kind="jira",
    )
    def board_jira_issues(context: AssetExecutionContext, config_resource: HeartbeatConfigResource) -> Output[list[dict]]:
        from ..config import load_config
        from ..jira import JiraClient
        from ..state import StateStore

        _touch_sentinel()
        config = load_config(config_path)
        store = StateStore(config.local_state_dir)
        jira = JiraClient(config)
        issues = jira.search_issues_by_statuses(config.display_statuses)
        context.log.info(f"[{board_name}] Fetched {len(issues)} issues")
        return Output(
            [asdict(i) for i in issues],
            metadata={"issue_count": MetadataValue.int(len(issues))},
        )

    @asset(
        name=f"{prefix}_heartbeat_decisions",
        description=f"[{board_name}] Plan decisions.",
        group_name=f"board_{prefix}",
        deps=[board_jira_issues],
        compute_kind="python",
    )
    def board_decisions(context: AssetExecutionContext, config_resource: HeartbeatConfigResource) -> Output[list[dict]]:
        from ..config import load_config
        from ..jira import JiraClient
        from ..service import plan_decisions, utc_now
        from ..state import StateStore

        config = load_config(config_path)
        store = StateStore(config.local_state_dir)
        jira = JiraClient(config)
        raw_issues = jira.search_issues_by_statuses(config.display_statuses)
        decisions = plan_decisions(config, store, raw_issues, created_at=utc_now())
        launch_count = sum(1 for d in decisions if d.should_launch)
        context.log.info(f"[{board_name}] {len(decisions)} decisions, {launch_count} to launch")
        return Output(
            [asdict(d) for d in decisions],
            metadata={
                "decision_count": MetadataValue.int(len(decisions)),
                "launch_count": MetadataValue.int(launch_count),
            },
        )

    @asset(
        name=f"{prefix}_heartbeat_launches",
        description=f"[{board_name}] Execute launches.",
        group_name=f"board_{prefix}",
        deps=[board_decisions],
        compute_kind="tmux",
    )
    def board_launches(context: AssetExecutionContext, config_resource: HeartbeatConfigResource) -> Output[list[dict]]:
        from ..config import load_config
        from ..jira import JiraClient
        from ..service import launch_planned_decisions, plan_decisions, utc_now
        from ..state import StateStore

        _touch_sentinel()
        config = load_config(config_path)
        store = StateStore(config.local_state_dir)
        jira = JiraClient(config)
        raw_issues = jira.search_issues_by_statuses(config.display_statuses)
        decisions = plan_decisions(config, store, raw_issues, created_at=utc_now())
        launches = launch_planned_decisions(config, store, decisions)
        context.log.info(f"[{board_name}] Launched {len(launches)} processes")
        _touch_sentinel()
        return Output(
            [asdict(l) for l in launches],
            metadata={"launched_count": MetadataValue.int(len(launches))},
        )

    @asset(
        name=f"{prefix}_process_reconciliation",
        description=f"[{board_name}] Reconcile processes.",
        group_name=f"board_{prefix}",
        compute_kind="python",
    )
    def board_reconciliation(context: AssetExecutionContext, config_resource: HeartbeatConfigResource) -> Output[list[dict]]:
        from ..config import load_config
        from ..runner import reconcile_processes
        from ..service import reconcile_runs
        from ..state import StateStore

        _touch_sentinel()
        config = load_config(config_path)
        store = StateStore(config.local_state_dir)
        reconcile_processes(store)
        reconciled = reconcile_runs(config, store)
        context.log.info(f"[{board_name}] Reconciled {len(reconciled)} runs")
        _touch_sentinel()
        return Output(
            [asdict(r) for r in reconciled],
            metadata={"reconciled_count": MetadataValue.int(len(reconciled))},
        )

    return [board_jira_issues, board_decisions, board_launches, board_reconciliation]
