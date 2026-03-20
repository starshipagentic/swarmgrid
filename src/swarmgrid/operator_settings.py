from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(slots=True)
class OperatorSettings:
    jira_email: str | None = None
    token_file: str | None = None
    claude_command: str | None = None
    claude_working_dir: str | None = None
    claude_max_parallel: int | None = None
    upterm_server: str | None = None
    hub_dev_id: str | None = None
    hub_ssh_connect: str | None = None
    hub_github_users: list[str] | None = None


def load_operator_settings(path: Path) -> OperatorSettings:
    if not path.exists():
        return OperatorSettings()

    with path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}

    jira = raw.get("jira", {})
    llm = raw.get("llm", {})
    sharing = raw.get("sharing", {})
    hub = raw.get("hub", {})
    return OperatorSettings(
        jira_email=jira.get("email"),
        token_file=jira.get("token_file"),
        claude_command=llm.get("command"),
        claude_working_dir=llm.get("working_dir"),
        claude_max_parallel=llm.get("max_parallel"),
        upterm_server=sharing.get("upterm_server"),
        hub_dev_id=hub.get("dev_id"),
        hub_ssh_connect=hub.get("ssh_connect"),
        hub_github_users=hub.get("github_users"),
    )


def save_operator_settings(path: Path, settings: OperatorSettings) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Preserve existing sections (like sharing:) that we don't manage
    existing: dict[str, Any] = {}
    if path.exists():
        with path.open("r", encoding="utf-8") as handle:
            existing = yaml.safe_load(handle) or {}
    existing.update({
        "schema_version": 1,
        "jira": {
            "email": settings.jira_email,
            "token_file": settings.token_file,
        },
        "llm": {
            "command": settings.claude_command,
            "working_dir": settings.claude_working_dir,
            "max_parallel": settings.claude_max_parallel,
        },
    })
    if settings.upterm_server:
        existing.setdefault("sharing", {})["upterm_server"] = settings.upterm_server
    if settings.hub_dev_id or settings.hub_ssh_connect or settings.hub_github_users is not None:
        hub = existing.setdefault("hub", {})
        if settings.hub_dev_id:
            hub["dev_id"] = settings.hub_dev_id
        if settings.hub_ssh_connect:
            hub["ssh_connect"] = settings.hub_ssh_connect
        if settings.hub_github_users is not None:
            hub["github_users"] = settings.hub_github_users
    with path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(existing, handle, sort_keys=False)
