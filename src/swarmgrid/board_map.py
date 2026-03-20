from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def load_board_map(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def transition_id_for_status(path: Path, status_name: str) -> str | None:
    raw = load_board_map(path)
    status_map = raw.get("status_map", {})
    entry = status_map.get(status_name)
    if not entry:
        return None
    transition_id = entry.get("transition_id")
    return str(transition_id) if transition_id is not None else None
