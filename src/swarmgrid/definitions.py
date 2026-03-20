from __future__ import annotations

from dataclasses import asdict
import os

from dagster import Definitions, MetadataValue, OpExecutionContext, Out, Output, job, op, schedule

from .config import load_config
from .models import JiraIssue, LaunchRecord, RouteDecision
from .service import fetch_issues, launch_planned_decisions, plan_decisions, reconcile_runs, utc_now
from .state import StateStore


def _config_path() -> str:
    return os.environ.get("TRAV_JIRA_HEARTBEAT_CONFIG", "board-routes.yaml")


@op(out=Out(dict))
def load_runtime() -> dict:
    config = load_config(_config_path())
    return {
        "config_path": str(config.config_path),
        "watched_statuses": config.watched_statuses,
        "local_state_dir": str(config.local_state_dir),
    }


@op(out=Out(list[dict]))
def fetch_jira_issues_op(context: OpExecutionContext, runtime: dict) -> Output[list[dict]]:
    config = load_config(runtime["config_path"])
    store = StateStore(config.local_state_dir)
    issues = fetch_issues(config, store)
    issue_dicts = [asdict(issue) for issue in issues]
    preview = "\n".join(
        f"- {issue.key} [{issue.status_name}] {issue.summary}" for issue in issues[:20]
    ) or "(none)"
    for issue in issues:
        context.log.info(f"FOUND {issue.key} [{issue.status_name}] {issue.summary}")
    return Output(
        issue_dicts,
        metadata={
            "issue_count": len(issue_dicts),
            "issues": MetadataValue.md(preview),
        },
    )


@op(out=Out(list[dict]))
def plan_routes_op(context: OpExecutionContext, runtime: dict, issues: list[dict]) -> Output[list[dict]]:
    config = load_config(runtime["config_path"])
    store = StateStore(config.local_state_dir)
    created_at = utc_now()
    issue_models = [JiraIssue(**issue) for issue in issues]
    decisions = plan_decisions(config, store, issue_models, created_at=created_at)
    decision_dicts = [asdict(item) for item in decisions]
    preview = "\n".join(
        f"- {item.issue_key}: `{item.action}` reason=`{item.reason}` prompt=`{item.prompt}`"
        for item in decisions[:20]
    ) or "(none)"
    prompt_preview = "\n".join(
        f"- {item.issue_key}: {item.prompt}" for item in decisions[:20]
    ) or "(none)"
    for item in decisions:
        context.log.info(
            f"PLAN {item.issue_key} status={item.status_name} reason={item.reason} prompt={item.prompt}"
        )
    return Output(
        decision_dicts,
        metadata={
            "decision_count": len(decision_dicts),
            "launchable_count": sum(1 for item in decisions if item.should_launch),
            "decisions": MetadataValue.md(preview),
            "prompt_preview": MetadataValue.md(prompt_preview),
        },
    )


@op(out=Out(list[dict]))
def launch_actions_op(context: OpExecutionContext, runtime: dict, decisions: list[dict]) -> Output[list[dict]]:
    config = load_config(runtime["config_path"])
    store = StateStore(config.local_state_dir)
    decision_models = [RouteDecision(**item) for item in decisions]
    launches = launch_planned_decisions(config, store, decision_models)
    launch_dicts = [asdict(item) for item in launches]
    preview = "\n".join(
        f"- {item.issue_key}: state=`{item.state}` prompt=`{item.prompt}` log=`{item.log_path}`"
        for item in launches[:20]
    ) or "(none)"
    return Output(
        launch_dicts,
        metadata={
            "launch_count": len(launch_dicts),
            "launches": MetadataValue.md(preview),
        },
    )


@op(out=Out(list[dict]))
def reconcile_runs_op(context: OpExecutionContext, runtime: dict) -> Output[list[dict]]:
    config = load_config(runtime["config_path"])
    store = StateStore(config.local_state_dir)
    reconciled = reconcile_runs(config, store)
    reconciled_dicts = [asdict(item) for item in reconciled]
    preview = "\n".join(
        f"- {item.issue_key}: state=`{item.state}` proofs={len(item.proof_files)} next=`{item.transition_target}`"
        for item in reconciled[:20]
    ) or "(none)"
    return Output(
        reconciled_dicts,
        metadata={
            "reconciled_count": len(reconciled_dicts),
            "reconciled": MetadataValue.md(preview),
        },
    )


@op(out=Out(dict))
def finalize_tick_op(
    context: OpExecutionContext,
    runtime: dict,
    issues: list[dict],
    decisions: list[dict],
    launches: list[dict],
    reconciled: list[dict],
) -> Output[dict]:
    config = load_config(runtime["config_path"])
    store = StateStore(config.local_state_dir)
    started_at = utc_now()
    finished_at = utc_now()
    store.record_tick(
        started_at=started_at,
        finished_at=finished_at,
        issue_count=len(issues),
        decision_count=len(decisions),
        launched_count=len(launches),
    )
    summary = {
        "issue_count": len(issues),
        "decision_count": len(decisions),
        "launch_count": len(launches),
        "reconciled_count": len(reconciled),
        "state": store.summarize(),
    }
    human_summary = "\n".join(
        f"- {item['issue_key']}: {item['prompt']} ({item['reason']})"
        for item in decisions[:20]
    ) or "(no decisions)"
    context.log.info(f"SUMMARY issues={len(issues)} decisions={len(decisions)} launches={len(launches)}")
    for item in decisions:
        context.log.info(
            f"SUMMARY_TICKET {item['issue_key']} title={next((issue['summary'] for issue in issues if issue['key'] == item['issue_key']), '')} prompt={item['prompt']} reason={item['reason']}"
        )
    return Output(
        summary,
        metadata={
            "issue_count": len(issues),
            "decision_count": len(decisions),
            "launch_count": len(launches),
            "reconciled_count": len(reconciled),
            "db_path": MetadataValue.path(summary["state"]["db_path"]),
            "human_summary": MetadataValue.md(human_summary),
        },
    )


@job
def heartbeat_job() -> None:
    runtime = load_runtime()
    issues = fetch_jira_issues_op(runtime)
    decisions = plan_routes_op(runtime, issues)
    launches = launch_actions_op(runtime, decisions)
    reconciled = reconcile_runs_op(runtime)
    finalize_tick_op(runtime, issues, decisions, launches, reconciled)


@schedule(job=heartbeat_job, cron_schedule="*/5 * * * *")
def heartbeat_schedule():
    return {}


defs = Definitions(jobs=[heartbeat_job], schedules=[heartbeat_schedule])
