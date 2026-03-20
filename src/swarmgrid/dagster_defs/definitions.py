"""Dagster Definitions entry point -- the single object dagster discovers."""
from __future__ import annotations

import os
from pathlib import Path

from dagster import Definitions

from .assets import (
    heartbeat_decisions,
    heartbeat_launches,
    jira_issues,
    process_reconciliation,
    ticket_changelogs,
    build_board_assets,
)
from .jobs import heartbeat_force_job, heartbeat_job, reconcile_job
from .resources import HeartbeatConfigResource
from .schedules import (
    heartbeat_schedule,
    reconcile_schedule,
    heartbeat_asset_schedule,
    reconcile_asset_schedule,
    heartbeat_asset_job,
    reconcile_asset_job,
)
from .sensors import manual_trigger_sensor


def _discover_extra_boards() -> list:
    """Discover additional board configs from the boards/ directory.

    Returns a flat list of extra asset definitions produced by
    ``build_board_assets`` for each YAML file in boards/.
    """
    boards_dir = Path(__file__).resolve().parents[3] / "boards"
    if not boards_dir.is_dir():
        return []

    from ..config import board_name_from_config, load_config

    extra_assets: list = []
    for cfg_path in sorted(boards_dir.iterdir()):
        if cfg_path.suffix not in {".yaml", ".yml"} or not cfg_path.is_file():
            continue
        try:
            config = load_config(cfg_path)
            name = board_name_from_config(config)
            extra_assets.extend(build_board_assets(name, str(cfg_path)))
        except Exception:
            pass
    return extra_assets


# Primary board assets
_primary_assets = [
    jira_issues,
    heartbeat_decisions,
    heartbeat_launches,
    process_reconciliation,
    ticket_changelogs,
]

# Multi-board assets (from boards/ directory)
_extra_board_assets = _discover_extra_boards()

defs = Definitions(
    assets=_primary_assets + _extra_board_assets,
    jobs=[heartbeat_job, heartbeat_force_job, reconcile_job, heartbeat_asset_job, reconcile_asset_job],
    schedules=[heartbeat_schedule, reconcile_schedule, heartbeat_asset_schedule, reconcile_asset_schedule],
    sensors=[manual_trigger_sensor],
    resources={
        "config_resource": HeartbeatConfigResource(),
    },
)
