"""Dagster schedules for periodic heartbeat execution."""
from __future__ import annotations

import os
from pathlib import Path

from dagster import (
    AssetSelection,
    DefaultScheduleStatus,
    ScheduleDefinition,
    define_asset_job,
)

from .jobs import heartbeat_job, reconcile_job


def _poll_interval_minutes() -> int:
    """Read poll_interval_minutes from board-routes.yaml at import time."""
    try:
        from ..config import load_config

        env_path = os.environ.get("DAGSTER_HEARTBEAT_CONFIG", "")
        if env_path:
            config_path = env_path
        else:
            project_root = Path(__file__).resolve().parents[3]
            config_path = str(project_root / "board-routes.yaml")
        config = load_config(config_path)
        return max(1, config.poll_interval_minutes)
    except Exception:
        return 5


# --- Legacy job-based schedules (kept for backward compat) ---

heartbeat_schedule = ScheduleDefinition(
    job=heartbeat_job,
    cron_schedule=f"*/{_poll_interval_minutes()} * * * *",
    name="heartbeat_schedule",
    description=f"Run heartbeat every {_poll_interval_minutes()} minutes.",
    default_status=DefaultScheduleStatus.STOPPED,
)

reconcile_schedule = ScheduleDefinition(
    job=reconcile_job,
    cron_schedule="* * * * *",
    name="reconcile_schedule",
    description="Reconcile process states every minute.",
    default_status=DefaultScheduleStatus.STOPPED,
)


# --- Asset-based schedules (the new way) ---

heartbeat_asset_job = define_asset_job(
    name="heartbeat_asset_job",
    selection=AssetSelection.assets("jira_issues", "heartbeat_decisions", "heartbeat_launches", "ticket_changelogs"),
    description="Materialize heartbeat pipeline assets.",
)

reconcile_asset_job = define_asset_job(
    name="reconcile_asset_job",
    selection=AssetSelection.assets("process_reconciliation"),
    description="Materialize process reconciliation asset.",
)

heartbeat_asset_schedule = ScheduleDefinition(
    job=heartbeat_asset_job,
    cron_schedule=f"*/{_poll_interval_minutes()} * * * *",
    name="heartbeat_asset_schedule",
    description=f"Materialize heartbeat assets every {_poll_interval_minutes()} minutes.",
    default_status=DefaultScheduleStatus.RUNNING,
)

reconcile_asset_schedule = ScheduleDefinition(
    job=reconcile_asset_job,
    cron_schedule="* * * * *",
    name="reconcile_asset_schedule",
    description="Materialize reconciliation asset every minute.",
    default_status=DefaultScheduleStatus.RUNNING,
)
