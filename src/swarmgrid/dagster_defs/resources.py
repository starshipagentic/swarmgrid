"""Dagster resource wrapping board-routes.yaml config."""
from __future__ import annotations

import os
from pathlib import Path

from dagster import ConfigurableResource


class HeartbeatConfigResource(ConfigurableResource):
    """Dagster resource that provides the path to board-routes.yaml.

    The config_path is resolved relative to DAGSTER_HEARTBEAT_CONFIG env var,
    or falls back to board-routes.yaml in the project root.
    """

    config_path: str = ""

    def effective_path(self) -> str:
        if self.config_path:
            return self.config_path
        env_path = os.environ.get("DAGSTER_HEARTBEAT_CONFIG", "")
        if env_path:
            return env_path
        # Default: board-routes.yaml relative to project root
        project_root = Path(__file__).resolve().parents[3]
        return str(project_root / "board-routes.yaml")
