from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import os
from typing import Any

import yaml


@dataclass(slots=True)
class JiraSettings:
    email_env: str
    token_env: str
    token_file: str


@dataclass(slots=True)
class LlmSettings:
    command: str
    args: list[str]
    working_dir: str | None
    enabled: bool
    dry_run: bool
    max_parallel: int = 1


@dataclass(slots=True)
class JiraActionSettings:
    enabled: bool


@dataclass(slots=True)
class RouteSettings:
    status: str
    action: str
    prompt_template: str
    enabled: bool = False
    allowed_issue_types: list[str] = field(default_factory=list)
    fire_on_first_seen: bool = True
    transition_on_launch: str | None = None
    transition_on_success: str | None = None
    transition_on_failure: str | None = None
    comment_on_launch_template: str | None = None
    comment_on_success_template: str | None = None
    comment_on_failure_template: str | None = None
    artifact_globs: list[str] = field(default_factory=list)
    # -- Route-based state detection (stretch, schema only) --
    idle_timeout_minutes: int | None = None
    cold_timeout_minutes: int | None = None
    output_match_patterns: list[str] = field(default_factory=list)
    transition_on_idle: str | None = None
    transition_on_match: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class AppConfig:
    config_path: Path
    site_url: str
    project_key: str
    board_id: str | None
    board_map_path: Path
    operator_settings_path: Path
    poll_interval_minutes: int
    local_state_dir: Path
    jira: JiraSettings
    llm: LlmSettings
    jira_actions: JiraActionSettings
    routes: list[RouteSettings] = field(default_factory=list)
    stale_display_minutes: int = 1440

    @property
    def watched_statuses(self) -> list[str]:
        return [route.status for route in self.routes]

    @property
    def display_statuses(self) -> list[str]:
        """All statuses relevant to the board — trigger statuses plus their transitions."""
        ordered: list[str] = []
        seen: set[str] = set()
        for route in self.routes:
            for value in [route.status, route.transition_on_launch, route.transition_on_success, route.transition_on_failure]:
                if not value or value in seen:
                    continue
                seen.add(value)
                ordered.append(value)
        if "Done" not in seen:
            ordered.append("Done")
        return ordered


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def _build_route(item: dict[str, Any]) -> RouteSettings:
    """Construct a RouteSettings, filling missing collection fields with defaults."""
    item = dict(item)  # shallow copy so we don't mutate the caller's data
    # Ensure list/dict fields are never None from YAML nulls
    if item.get("allowed_issue_types") is None:
        item.pop("allowed_issue_types", None)
    if item.get("artifact_globs") is None:
        item.pop("artifact_globs", None)
    if item.get("output_match_patterns") is None:
        item.pop("output_match_patterns", None)
    if item.get("transition_on_match") is None:
        item.pop("transition_on_match", None)
    return RouteSettings(**item)


def load_config(config_path: str | Path) -> AppConfig:
    path = Path(config_path).expanduser().resolve()
    raw = load_yaml(path)

    jira = JiraSettings(**raw["jira"])
    llm = LlmSettings(**raw["llm"])
    jira_actions = JiraActionSettings(**raw.get("jira_actions", {"enabled": False}))
    routes = [_build_route(item) for item in raw.get("routes", [])]

    state_dir = Path(raw["local_state_dir"]).expanduser()
    if not state_dir.is_absolute():
        state_dir = (path.parent / state_dir).resolve()

    board_map_path = Path(raw["board_map_path"]).expanduser()
    if not board_map_path.is_absolute():
        board_map_path = (path.parent / board_map_path).resolve()
    board_id = raw.get("board_id")
    if board_id is None and board_map_path.exists():
        board_map_raw = load_yaml(board_map_path)
        board_id = str(board_map_raw.get("board", {}).get("board_id") or "") or None

    operator_settings_path = Path(
        raw.get("operator_settings_path", "./operator-settings.yaml")
    ).expanduser()
    if not operator_settings_path.is_absolute():
        operator_settings_path = (path.parent / operator_settings_path).resolve()

    stale_display_minutes = int(raw.get("stale_display_minutes", 1440))

    return AppConfig(
        config_path=path,
        site_url=raw["site_url"].rstrip("/"),
        project_key=raw["project_key"],
        board_id=board_id,
        board_map_path=board_map_path,
        operator_settings_path=operator_settings_path,
        poll_interval_minutes=int(raw.get("poll_interval_minutes", 5)),
        local_state_dir=state_dir,
        jira=jira,
        llm=llm,
        jira_actions=jira_actions,
        routes=routes,
        stale_display_minutes=stale_display_minutes,
    )


def load_configs(paths: list[str | Path]) -> list[AppConfig]:
    """Load multiple board configurations from a list of paths."""
    return [load_config(p) for p in paths]


def discover_board_configs(directory: str | Path) -> list[Path]:
    """Find all YAML config files in a boards directory.

    Returns paths sorted by filename so ordering is deterministic.
    """
    board_dir = Path(directory).expanduser().resolve()
    if not board_dir.is_dir():
        return []
    configs = sorted(
        p for p in board_dir.iterdir()
        if p.suffix in {".yaml", ".yml"} and p.is_file()
    )
    return configs


def load_all_board_configs(
    config_paths: list[str | Path] | None = None,
    configs_dir: str | Path | None = None,
) -> list[AppConfig]:
    """Load board configurations from explicit paths and/or a directory.

    If both are provided, explicit paths come first, followed by
    any discovered in the directory (de-duplicated by resolved path).
    """
    seen: set[Path] = set()
    configs: list[AppConfig] = []

    paths: list[Path] = []
    if config_paths:
        paths.extend(Path(p).expanduser().resolve() for p in config_paths)
    if configs_dir:
        paths.extend(discover_board_configs(configs_dir))

    for path in paths:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        configs.append(load_config(resolved))

    return configs


def board_name_from_config(config: AppConfig) -> str:
    """Derive a short board name from a config for use as a prefix/label.

    For ``board-routes.yaml`` (the default name) we return the project
    key.  For ``board-routes-foo.yaml`` we return ``foo``.
    """
    stem = config.config_path.stem
    # Exact matches for default names -> use project key
    if stem in ("board-routes", "board"):
        return config.project_key
    # Strip common prefixes to get a short label
    for prefix in ("board-routes-", "board-"):
        if stem.startswith(prefix) and len(stem) > len(prefix):
            return stem[len(prefix):]
    return stem or config.project_key


def resolve_jira_auth(config: AppConfig) -> tuple[str, str]:
    from .auth import resolve_auth_state
    auth = resolve_auth_state(config)

    if not auth.email:
        raise RuntimeError(
            f"Missing Jira email. Export {config.jira.email_env} before running the heartbeat."
        )

    if not auth.token:
        raise RuntimeError(
            "Missing Jira token. Set "
            f"{config.jira.token_env}, macOS keychain entry {auth.email!r}, "
            f"or create {Path(config.jira.token_file).expanduser()}."
        )

    return auth.email, auth.token
