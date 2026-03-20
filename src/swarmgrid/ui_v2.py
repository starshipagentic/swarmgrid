from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
import select
import sys
import termios
import tty

import yaml
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.prompt import Confirm
from rich.table import Table
from rich.text import Text

from .config import AppConfig, load_config
from .jira import JiraClient
from .operator_settings import OperatorSettings, load_operator_settings, save_operator_settings
from .runner import (
    apply_tmux_defaults,
    attach_session,
    capture_session_output,
    classify_process_row,
    open_session_in_terminal,
    reconcile_processes,
    terminate_process,
)
from .service import run_heartbeat
from .state import StateStore


console = Console()
READ_REFRESH_SECONDS = 10
LOCAL_REFRESH_SECONDS = 2
PAGES = ["board", "routes", "setup"]
VISIBLE_CARDS_PER_COLUMN = 3
BOARD_ROW_LIMIT = 25


@dataclass
class UiState:
    page: str = "board"
    board_issues: list[dict] | None = None
    board_error: str | None = None
    message: str = "Starting operator console..."
    selected_board_index: int = 0
    selected_board_key: str | None = None
    selected_route_index: int = 0
    selected_setup_index: int = 0
    auto_heartbeat: bool = True
    next_board_refresh_at: datetime | None = None
    next_local_refresh_at: datetime | None = None
    next_heartbeat_at: datetime | None = None
    last_board_refresh_at: datetime | None = None
    last_local_refresh_at: datetime | None = None
    last_heartbeat_result: dict | None = None
    setup_editing: bool = False
    setup_edit_buffer: str = ""
    local_output_by_issue: dict[str, str] | None = None
    board_scroll_by_status: dict[str, int] | None = None


def run_console_v2(config_path: str) -> int:
    apply_tmux_defaults()
    config = load_config(config_path)
    state = UiState()
    state.page = _initial_page(config)
    state.message = _initial_message(config)
    state = _refresh_board(config, state, initial=True)
    state = _refresh_local(config, state, initial=True)
    state.next_heartbeat_at = _utc_now() + timedelta(minutes=config.poll_interval_minutes)

    keyboard = _cbreak_input()
    with keyboard:
        with Live(
            _render_app(config_path, state),
            console=console,
            screen=True,
            auto_refresh=False,
            transient=False,
        ) as live:
            while True:
                config = load_config(config_path)
                now = _utc_now()
                if state.next_board_refresh_at is None or now >= state.next_board_refresh_at:
                    state = _refresh_board(config, state)
                if state.next_local_refresh_at is None or now >= state.next_local_refresh_at:
                    state = _refresh_local(config, state)
                if state.auto_heartbeat and state.next_heartbeat_at and now >= state.next_heartbeat_at:
                    state = _run_tick(config_path, config, state, trigger="timer")
                    state = _refresh_board(config, state)
                    state = _refresh_local(config, state)

                live.update(_render_app(config_path, state), refresh=True)
                key = _read_key(timeout=1.0)
                if not key:
                    continue
                if key in {"q", "\x03"}:
                    return 0

                if state.setup_editing:
                    state = _handle_setup_edit_key(config_path, config, state, key)
                    continue

                board_rows = _live_board_rows(config, StateStore(config.local_state_dir), state.board_issues or [])

                if key in {"w", "UP"}:
                    state = _move_selection(config, state, board_rows, [], "up")
                    continue
                if key in {"s", "DOWN"}:
                    state = _move_selection(config, state, board_rows, [], "down")
                    continue
                if key in {"a", "LEFT"}:
                    state = _move_selection(config, state, board_rows, [], "left")
                    continue
                if key in {"d", "RIGHT"}:
                    state = _move_selection(config, state, board_rows, [], "right")
                    continue
                if key in {",", "<"}:
                    state.page = _cycle_page(state.page, -1)
                    state.message = f"{state.page.title()} view."
                    continue
                if key in {".", ">"}:
                    state.page = _cycle_page(state.page, 1)
                    state.message = f"{state.page.title()} view."
                    continue

                live.stop()
                try:
                    keyboard.suspend()
                    state = _handle_action_key(
                        key=key,
                        config_path=config_path,
                        config=config,
                        state=state,
                        board_rows=board_rows,
                        archived_board_rows=[],
                    )
                finally:
                    keyboard.resume()
                    live.start(refresh=True)


def _handle_action_key(
    key: str,
    config_path: str,
    config: AppConfig,
    state: UiState,
    board_rows: list[dict],
    archived_board_rows: list[dict],
) -> UiState:
    if key == "b":
        state.page = "board"
        state.message = "Board view."
        return state
    if key == "t":
        state.page = "routes"
        state.message = "Routes / arming view."
        return state
    if key == "u":
        state.page = "setup"
        state.message = "Setup view."
        return state
    if key == "h":
        state = _run_tick(config_path, config, state, trigger="manual")
        return _refresh_board(load_config(config_path), state)
    if key == "z":
        state.auto_heartbeat = not state.auto_heartbeat
        if state.auto_heartbeat:
            state.next_heartbeat_at = _utc_now() + timedelta(minutes=config.poll_interval_minutes)
            state.message = "5-minute heartbeat enabled."
        else:
            state.next_heartbeat_at = None
            state.message = "5-minute heartbeat paused."
        return state
    if key in {"-", "_"}:
        state.message = _adjust_parallel(config)
        return state
    if key in {"=", "+"}:
        state.message = _adjust_parallel(config, delta=1)
        return state
    if key == " " and state.page == "routes":
        state.message = _toggle_selected_route(config_path, state.selected_route_index)
        return _refresh_board(load_config(config_path), state)
    if key in {"ENTER", "o"}:
        if state.page == "setup":
            return _begin_setup_edit(config, state)
        if state.page == "routes":
            state.message = _toggle_selected_route(config_path, state.selected_route_index)
            return _refresh_board(load_config(config_path), state)
        state.message = _open_selected_terminal(config, state, board_rows, archived_board_rows)
        return state
    if key == "e" and state.page == "setup":
        return _begin_setup_edit(config, state)
    if key in {"v", "W"}:
        state.message = _watch_selected(config, state, board_rows, archived_board_rows)
        return state
    if key == "i":
        state.message = _inline_attach_selected(config, state, board_rows, archived_board_rows)
        return state
    if key == "x":
        state.message = _kill_selected(config, state, board_rows, archived_board_rows)
        return state
    state.message = f"Unknown key: {key}"
    return state


def _move_selection(
    config: AppConfig,
    state: UiState,
    board_rows: list[dict],
    archived_rows: list[dict],
    direction: str,
) -> UiState:
    if state.page == "board":
        state.selected_board_key = _move_grid_key(
            rows=board_rows,
            statuses=_display_statuses(config),
            current_key=state.selected_board_key or _selected_issue_key(board_rows, state.selected_board_index),
            direction=direction,
        )
        state.selected_board_index = _index_for_key(board_rows, state.selected_board_key)
    elif state.page == "routes":
        delta = -1 if direction in {"up", "left"} else 1
        state.selected_route_index = _next_index(state.selected_route_index, config.routes, delta)
    elif state.page == "setup":
        delta = -1 if direction in {"up", "left"} else 1
        state.selected_setup_index = _next_index(state.selected_setup_index, _setup_fields(config), delta)
    return state


def _initial_page(config: AppConfig) -> str:
    settings = load_operator_settings(config.operator_settings_path)
    if not settings.jira_email or not (settings.claude_working_dir or config.llm.working_dir):
        return "setup"
    return "board"


def _initial_message(config: AppConfig) -> str:
    settings = load_operator_settings(config.operator_settings_path)
    if not settings.jira_email:
        return "Setup needed: add Jira email in Setup."
    if not (settings.claude_working_dir or config.llm.working_dir):
        return "Setup needed: set Claude working directory in Setup."
    return "Board view."


def _refresh_board(config: AppConfig, state: UiState, initial: bool = False) -> UiState:
    store = StateStore(config.local_state_dir)
    reconcile_processes(store)
    try:
        jira = JiraClient(config)
        issues = [asdict(issue) for issue in jira.search_issues_by_statuses(_display_statuses(config))]
        state.board_issues = issues
        state.board_error = None
        state.last_board_refresh_at = _utc_now()
        state.next_board_refresh_at = _utc_now() + timedelta(seconds=READ_REFRESH_SECONDS)
        if initial:
            state.message = state.message or "Board loaded from Jira."
    except Exception as exc:
        state.board_error = str(exc)
        state.next_board_refresh_at = _utc_now() + timedelta(seconds=READ_REFRESH_SECONDS)
        if state.board_issues is None:
            state.board_issues = []
        if initial or "Board refresh failed" not in state.message:
            state.message = f"Board refresh failed: {exc}"
    return state


def _refresh_local(config: AppConfig, state: UiState, initial: bool = False) -> UiState:
    store = StateStore(config.local_state_dir)
    reconcile_processes(store)
    outputs: dict[str, str] = {}
    for row in store.list_running_processes():
        outputs[row["issue_key"]] = capture_session_output(row, lines=18).strip()
    for row in store.list_archived_processes(limit=50):
        outputs.setdefault(row["issue_key"], capture_session_output(row, lines=18).strip())
    state.local_output_by_issue = outputs
    state.last_local_refresh_at = _utc_now()
    state.next_local_refresh_at = _utc_now() + timedelta(seconds=LOCAL_REFRESH_SECONDS)
    if initial and not state.message:
        state.message = "Local session view loaded."
    return state


def _run_tick(config_path: str, config: AppConfig, state: UiState, trigger: str) -> UiState:
    result = run_heartbeat(config_path, force_reconsider=(trigger == "manual"))
    launched = ", ".join(item["issue_key"] for item in result.get("launches", [])) or "none"
    archived = ", ".join(result.get("archived", [])) or "none"
    prefix = "Manual" if trigger == "manual" else "Scheduled"
    state.last_heartbeat_result = result
    state.next_heartbeat_at = _utc_now() + timedelta(minutes=config.poll_interval_minutes)
    state.message = (
        f"{prefix} heartbeat: launched {result['launched_count']} ({launched}), "
        f"archived {archived}."
    )
    return state


def _render_app(config_path: str, state: UiState):
    config = load_config(config_path)
    store = StateStore(config.local_state_dir)
    running_rows = store.list_running_processes()
    board_rows = _live_board_rows(config, store, state.board_issues or [])
    state.selected_board_index = _clamp_index(state.selected_board_index, board_rows)
    state.selected_board_key = _selected_issue_key(board_rows, state.selected_board_index)
    state.selected_route_index = _clamp_index(state.selected_route_index, config.routes)
    state.selected_setup_index = _clamp_index(state.selected_setup_index, _setup_fields(config))
    statuses = _display_statuses(config)
    state.board_scroll_by_status = _sync_viewports(
        state.board_scroll_by_status,
        board_rows,
        statuses,
        state.selected_board_key,
    )

    layout = Layout()
    layout.split_column(
        Layout(_header_panel(config, state, running_rows), size=6),
        Layout(name="body", ratio=1),
        Layout(_footer_panel(state.page), size=3),
    )

    if state.page == "board":
        layout["body"].split_column(
            Layout(_board_panel(config, state, board_rows), ratio=3),
            Layout(_selected_issue_panel(config, state, board_rows), size=18),
        )
    elif state.page == "routes":
        layout["body"].split_row(
            Layout(_routes_panel(config, state, board_rows), ratio=2),
            Layout(_routes_help_panel(config, state), ratio=1),
        )
    elif state.page == "setup":
        layout["body"].split_column(
            Layout(_setup_panel(config, state), ratio=1),
            Layout(_setup_editor_panel(config, state), size=6 if state.setup_editing else 3),
        )
    return layout


def _header_panel(
    config: AppConfig,
    state: UiState,
    running_rows: list[dict],
) -> Panel:
    settings = load_operator_settings(config.operator_settings_path)
    pages = Text("Pages: ", style="white")
    for page in PAGES:
        label = page.title()
        style = "bold black on cyan" if state.page == page else "white"
        pages.append(f" {label} ", style=style)
        pages.append(" ")

    board_rows = _live_board_rows(config, StateStore(config.local_state_dir), state.board_issues or [])
    lines = [
        pages,
        Text(
            f"Board refresh: {_countdown(state.next_board_refresh_at, READ_REFRESH_SECONDS)}    "
            f"Local: {_countdown(state.next_local_refresh_at, LOCAL_REFRESH_SECONDS)}    "
            f"Heartbeat: {_countdown(state.next_heartbeat_at, config.poll_interval_minutes * 60) if state.auto_heartbeat else 'paused'}    "
            f"Live: {len(running_rows)}    "
            f"Visible: {len(board_rows)}",
            style="white",
        ),
        Text(
            f"Site: {config.site_url}   Board: {config.board_id or '-'}   Project: {config.project_key}   "
            f"Workdir: {settings.claude_working_dir or config.llm.working_dir or '-'}",
            style="cyan",
        ),
        Text(state.message, style="yellow"),
    ]
    if state.board_error:
        lines.append(Text(f"Board error: {state.board_error}", style="red"))
    return Panel(Group(*lines), title="SwarmGrid Heartbeat V2", border_style="bright_blue")


def _board_panel(config: AppConfig, state: UiState, board_rows: list[dict]) -> Panel:
    columns = _display_statuses(config)
    selected_key = _selected_issue_key(board_rows, state.selected_board_index)
    store = StateStore(config.local_state_dir)
    running_modes = {
        row["issue_key"]: classify_process_row(row)
        for row in store.list_running_processes()
    }
    archived_keys = {row["issue_key"] for row in store.list_archived_processes(limit=100)}
    grid = Table.grid(expand=True)
    for _ in columns:
        grid.add_column(ratio=1)

    panels = []
    for status in columns:
        items = [row for row in board_rows if row["status_name"] == status]
        offset = _viewport_offset(state.board_scroll_by_status, status)
        visible_items = items[offset : offset + VISIBLE_CARDS_PER_COLUMN]
        cards: list[Panel | Text] = []
        if not items:
            cards.append(Text("No tickets", style="dim"))
        else:
            if offset > 0:
                cards.append(Text(f"^ {offset} more", style="dim"))
        for row in visible_items:
            cards.append(
                _ticket_card(
                    row=row,
                    selected=(row["key"] == selected_key),
                    local_mode=_local_ticket_mode(row["key"], running_modes, archived_keys),
                )
            )
        remaining = len(items) - (offset + len(visible_items))
        if remaining > 0:
            cards.append(Text(f"v {remaining} more", style="dim"))
        panels.append(Panel(Group(*cards), title=f"{status} ({len(items)})", border_style="magenta"))
    if panels:
        grid.add_row(*panels)
    return Panel(grid, title="Kanban", border_style="bright_magenta")


def _local_ticket_mode(issue_key: str, running_modes: dict[str, str], archived_keys: set[str]) -> str:
    if issue_key in running_modes:
        return running_modes[issue_key]
    if issue_key in archived_keys:
        return "archived"
    return "none"


def _ticket_card(row: dict, selected: bool, local_mode: str) -> Panel:
    key_style = "bold white"
    border_style = "white"
    if local_mode == "active":
        border_style = "bright_green"
        key_style = "bold black on bright_green" if selected else "bold bright_green"
    elif local_mode == "idle":
        border_style = "bright_blue"
        key_style = "bold black on bright_blue" if selected else "bold bright_blue"
    elif local_mode == "stale":
        border_style = "bright_magenta"
        key_style = "bold black on bright_magenta" if selected else "bold bright_magenta"
    elif local_mode == "archived":
        border_style = "yellow"
        key_style = "bold black on yellow" if selected else "bold yellow"
    elif selected:
        border_style = "bright_cyan"
        key_style = "bold black on bright_cyan"

    key_text = Text()
    if selected:
        key_text.append("> ", style=key_style)
    key_text.append(row["key"], style=key_style)
    marker = _local_ticket_marker(local_mode)
    if marker:
        key_text.append(f" {marker}", style=key_style)
    return Panel(key_text, border_style=border_style)


def _local_ticket_marker(local_mode: str) -> str:
    if local_mode == "active":
        return "✺" if _utc_now().second % 2 == 0 else "✹"
    if local_mode == "idle":
        return "◌"
    if local_mode == "stale":
        return "◇"
    if local_mode == "archived":
        return "✦"
    return ""


def _selected_issue_panel(
    config: AppConfig,
    state: UiState,
    rows: list[dict],
) -> Panel:
    index = state.selected_board_index
    selected = _selected_issue(rows, index)
    if not selected:
        return Panel("No ticket selected.", title="Selected Ticket", border_style="cyan")

    store = StateStore(config.local_state_dir)
    process_rows = store.list_running_processes()
    local_row = next((row for row in process_rows if row["issue_key"] == selected["key"]), None)
    route = next((item for item in config.routes if item.status == selected["status_name"]), None)

    lines: list[Text] = [
        Text(selected["summary"], style="bold white"),
        Text(f"{_kind_label(selected)}  |  {selected['status_name']}  |  {selected['key']}", style="magenta"),
    ]
    if selected.get("parent_key"):
        lines.append(
            Text(
                f"Parent: {selected.get('parent_issue_type') or 'Parent'} "
                f"{selected['parent_key']} {_truncate(selected.get('parent_summary') or '', 60)}",
                style="cyan",
            )
        )
    if selected.get("labels"):
        lines.append(Text(f"Tags: {', '.join(selected['labels'][:5])}", style="yellow"))
    if local_row:
        session_kind = classify_process_row(local_row)
        lines.append(
            Text(
                f"Local state: {local_row['state']} / {session_kind}  |  {local_row.get('session_name') or '-'}",
                style="bright_cyan",
            )
        )
        if local_row.get("prompt"):
            lines.append(Text(f"Prompt: {local_row['prompt']}", style="green"))
    elif route:
        prompt = route.prompt_template.format(
            issue_key=selected["key"],
            summary=selected["summary"],
            status=selected["status_name"],
            issue_type=selected.get("issue_type", ""),
        )
        lines.append(Text(f"Prompt map: {route.status} -> {prompt}", style="green"))
    else:
        lines.append(Text("No local Claude session for this ticket on this machine.", style="dim"))

    if local_row:
        output = ((state.local_output_by_issue or {}).get(selected["key"]) or "").strip()
        lines.append(Text(""))
        lines.append(Text("Latest output", style="bold green"))
        if output:
            lines.append(Text(_compact_preview_output(output), style="white"))
        else:
            lines.append(Text("No recent tmux output captured yet.", style="dim"))

    return Panel(Group(*lines), title="Selected Ticket", border_style="cyan")


def _compact_preview_output(output: str, max_lines: int = 8, max_width: int = 132) -> str:
    raw_lines = [line.rstrip() for line in output.splitlines()]
    compacted: list[str] = []
    blank_run = 0
    for line in raw_lines:
        if line.strip():
            blank_run = 0
            compacted.append(_truncate(line.strip(), max_width))
            continue
        blank_run += 1
        if blank_run <= 1:
            compacted.append("")

    if not compacted:
        return ""

    tail = compacted[-max_lines:]
    while tail and not tail[0].strip():
        tail = tail[1:]
    return "\n".join(tail).strip()


def _routes_panel(config: AppConfig, state: UiState, board_rows: list[dict]) -> Panel:
    trigger_counts = {route.status: 0 for route in config.routes}
    for row in board_rows:
        if row["status_name"] in trigger_counts:
            trigger_counts[row["status_name"]] += 1

    table = Table(expand=True)
    table.add_column("", width=2, no_wrap=True)
    table.add_column("Status", style="cyan")
    table.add_column("Armed", style="magenta", no_wrap=True)
    table.add_column("Board", style="white", no_wrap=True)
    table.add_column("Prompt", style="green")
    table.add_column("Launch", style="yellow", no_wrap=True)
    table.add_column("Success", style="bright_green", no_wrap=True)
    for index, route in enumerate(config.routes):
        marker = ">" if index == state.selected_route_index else " "
        style = "bold green" if index == state.selected_route_index else "white"
        table.add_row(
            marker,
            route.status,
            "ON" if route.enabled else "OFF",
            str(trigger_counts.get(route.status, 0)),
            route.prompt_template,
            route.transition_on_launch or "-",
            route.transition_on_success or "-",
            style=style,
        )
    return Panel(table, title="Routes / Arming", border_style="cyan")


def _routes_help_panel(config: AppConfig, state: UiState) -> Panel:
    route = config.routes[state.selected_route_index] if config.routes else None
    lines: list[Text] = [
        Text("Trigger columns show all board tickets.", style="bold cyan"),
        Text("Downstream columns only show tickets with local tmux sessions.", style="white"),
        Text("Use Enter or space to arm/disarm the selected route.", style="dim"),
        Text("Use - and = to change local Claude parallelism.", style="dim"),
        Text("Auto order: " + " -> ".join(_display_statuses(config)), style="white"),
    ]
    if route:
        lines.extend(
            [
                Text(""),
                Text(f"Selected: {route.status}", style="bold green"),
                Text(f"Allowed types: {', '.join(route.allowed_issue_types) or 'any'}", style="white"),
                Text(f"Action: {route.action}", style="white"),
                Text(f"On launch: {route.transition_on_launch or '-'}", style="yellow"),
                Text(f"On success: {route.transition_on_success or '-'}", style="bright_green"),
                Text(f"On failure: {route.transition_on_failure or '-'}", style="red"),
            ]
        )
    return Panel(Group(*lines), title="Route Help", border_style="green")


def _setup_panel(config: AppConfig, state: UiState) -> Panel:
    fields = _setup_fields(config)
    table = Table(expand=True)
    table.add_column("", width=2, no_wrap=True)
    table.add_column("Field", style="cyan", no_wrap=True)
    table.add_column("Value", style="white")
    table.add_column("Scope", style="magenta", no_wrap=True)
    for index, field in enumerate(fields):
        selected = index == state.selected_setup_index
        marker = ">" if selected else " "
        style = "bold green" if selected else "white"
        table.add_row(marker, field["label"], _display_value(field["value"]), field["scope"], style=style)
    help_lines = [
        table,
        Text("Use w/s to move. Press Enter to edit inline below.", style="yellow"),
        Text("Shared fields update board-routes.yaml. Local fields update operator-settings.yaml.", style="dim"),
    ]
    return Panel(Group(*help_lines), title="Setup", border_style="bright_blue")


def _setup_editor_panel(config: AppConfig, state: UiState) -> Panel:
    fields = _setup_fields(config)
    if not fields:
        return Panel("No setup fields available.", title="Editor", border_style="white")
    field = fields[_clamp_index(state.selected_setup_index, fields)]
    if state.setup_editing:
        lines = [
            Text(f"Editing: {field['label']}", style="bold cyan"),
            Text(f"{state.setup_edit_buffer}", style="white"),
            Text("Enter=save  Esc=cancel  Backspace=delete", style="yellow"),
        ]
        return Panel(Group(*lines), title="Inline Editor", border_style="cyan")
    lines = [
        Text(f"Selected: {field['label']}", style="bold cyan"),
        Text(f"Current: {_display_value(field['value'])}", style="white"),
        Text("Press Enter to edit in place.", style="yellow"),
    ]
    return Panel(Group(*lines), title="Inline Editor", border_style="white")


def _footer_panel(page: str) -> Panel:
    common = "[cyan]w/a/s/d[/cyan] move  [cyan]< >[/cyan] pages  [cyan]enter[/cyan] select/open  [cyan]q[/cyan] quit"
    legend = "[green]✹ active[/green]  [blue]◌ idle[/blue]  [magenta]◇ stale[/magenta]  [yellow]✦ archived[/yellow]"
    if page == "board":
        text = (
            "[bold]Board[/bold]  "
            f"{common}  [cyan]h[/cyan] heartbeat  [cyan]v[/cyan] watch  [cyan]i[/cyan] inline  "
            f"[cyan]x[/cyan] kill  [cyan]z[/cyan] auto on/off    {legend}"
        )
    elif page == "routes":
        text = (
            "[bold]Routes[/bold]  "
            f"{common}  [cyan]space[/cyan] arm/disarm  [cyan]-/=[/cyan] max claudes"
        )
    elif page == "setup":
        text = f"[bold]Setup[/bold]  {common}"
    else:
        text = f"{common}"
    return Panel(text, border_style="white")


def _live_board_rows(config: AppConfig, store: StateStore, board_issues: list[dict]) -> list[dict]:
    display_statuses = _display_statuses(config)
    trigger_statuses = {route.status for route in config.routes}
    live_rows = store.list_running_processes()
    live_keys = {row["issue_key"] for row in live_rows}
    issue_map = _issue_map(board_issues)
    _hydrate_issue_map_from_local_state(store, issue_map, live_keys)

    rows = [
        row
        for row in issue_map.values()
        if row["status_name"] in display_statuses
        and (row["status_name"] in trigger_statuses or row["key"] in live_keys)
    ]
    return _sort_issue_rows(rows, display_statuses)[:BOARD_ROW_LIMIT]


def _issue_map(board_issues: list[dict]) -> dict[str, dict]:
    return {row["key"]: dict(row) for row in board_issues if row.get("key")}


def _hydrate_issue_map_from_local_state(
    store: StateStore,
    issue_map: dict[str, dict],
    issue_keys: set[str],
) -> None:
    for key in issue_keys:
        if key in issue_map:
            continue
        row = store.get_issue_state(key)
        if not row:
            continue
        issue_map[key] = {
            "key": row["issue_key"],
            "summary": row["summary"],
            "issue_type": row["issue_type"],
            "status_name": row["status_name"],
            "status_id": row["status_id"],
            "updated": row["updated"],
            "assignee": row.get("assignee"),
            "browse_url": row["browse_url"],
            "parent_key": row.get("parent_key"),
            "parent_issue_type": row.get("parent_issue_type"),
            "parent_summary": row.get("parent_summary"),
            "epic_story_count": row.get("epic_story_count"),
            "labels": [value for value in (row.get("labels") or "").splitlines() if value],
        }


def _sort_issue_rows(rows: list[dict], statuses: list[str]) -> list[dict]:
    status_order = {status: index for index, status in enumerate(statuses)}
    rows = sorted(
        rows,
        key=lambda row: (row.get("updated", ""), row.get("key", "")),
        reverse=True,
    )
    return sorted(
        rows,
        key=lambda row: status_order.get(row.get("status_name"), len(status_order)),
    )


def _viewport_offset(scroll_map: dict[str, int] | None, status: str) -> int:
    if not scroll_map:
        return 0
    return max(0, int(scroll_map.get(status, 0)))


def _sync_viewports(
    scroll_map: dict[str, int] | None,
    rows: list[dict],
    statuses: list[str],
    selected_key: str | None,
) -> dict[str, int]:
    synced = dict(scroll_map or {})
    for status in statuses:
        items = [row for row in rows if row["status_name"] == status]
        max_offset = max(0, len(items) - VISIBLE_CARDS_PER_COLUMN)
        offset = min(max(0, synced.get(status, 0)), max_offset)
        if selected_key:
            selected_index = next(
                (index for index, row in enumerate(items) if row["key"] == selected_key),
                None,
            )
            if selected_index is not None:
                offset = min(selected_index, max_offset)
        synced[status] = offset
    return synced


def _toggle_selected_route(config_path: str, route_index: int) -> str:
    parsed = yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}
    routes = parsed.get("routes", [])
    if route_index < 0 or route_index >= len(routes):
        return "No route selected."
    routes[route_index]["enabled"] = not bool(routes[route_index].get("enabled"))
    parsed["routes"] = routes
    Path(config_path).write_text(yaml.safe_dump(parsed, sort_keys=False), encoding="utf-8")
    state = "ON" if routes[route_index]["enabled"] else "OFF"
    return f"{routes[route_index].get('status', 'Route')} is now {state}."


def _adjust_parallel(config: AppConfig, delta: int = -1) -> str:
    settings = load_operator_settings(config.operator_settings_path)
    current = settings.claude_max_parallel or config.llm.max_parallel
    updated = max(1, int(current) + delta)
    save_operator_settings(
        config.operator_settings_path,
        OperatorSettings(
            jira_email=settings.jira_email,
            token_file=settings.token_file or config.jira.token_file,
            claude_command=settings.claude_command or config.llm.command,
            claude_working_dir=settings.claude_working_dir or config.llm.working_dir,
            claude_max_parallel=updated,
        ),
    )
    return f"Claude max parallel set to {updated}."


def _begin_setup_edit(config: AppConfig, state: UiState) -> UiState:
    fields = _setup_fields(config)
    if not fields:
        state.message = "No setup fields available."
        return state
    field = fields[_clamp_index(state.selected_setup_index, fields)]
    state.setup_editing = True
    state.setup_edit_buffer = "" if field["value"] in {None, "-"} else str(field["value"])
    state.message = f"Editing {field['label']} inline."
    return state


def _handle_setup_edit_key(config_path: str, config: AppConfig, state: UiState, key: str) -> UiState:
    if key in {"\x1b", "ESC"}:
        state.setup_editing = False
        state.setup_edit_buffer = ""
        state.message = "Edit cancelled."
        return state
    if key == "ENTER":
        message = _save_selected_setup_field(config_path, config, state, state.setup_edit_buffer)
        state.setup_editing = False
        state.setup_edit_buffer = ""
        state.message = message
        apply_tmux_defaults()
        return _refresh_board(load_config(config_path), state)
    if key in {"\x7f", "BACKSPACE"}:
        state.setup_edit_buffer = state.setup_edit_buffer[:-1]
        return state
    if len(key) == 1 and key.isprintable():
        state.setup_edit_buffer += key
    return state


def _setup_fields(config: AppConfig) -> list[dict]:
    settings = load_operator_settings(config.operator_settings_path)
    return [
        {"label": "Jira email", "value": settings.jira_email or "", "scope": "local", "key": "jira_email", "type": "str"},
        {"label": "Token file", "value": settings.token_file or config.jira.token_file, "scope": "local", "key": "token_file", "type": "str"},
        {"label": "Claude command", "value": settings.claude_command or config.llm.command, "scope": "local", "key": "claude_command", "type": "str"},
        {"label": "Claude workdir", "value": settings.claude_working_dir or config.llm.working_dir or "", "scope": "local", "key": "claude_working_dir", "type": "str"},
        {"label": "Max parallel", "value": settings.claude_max_parallel or config.llm.max_parallel, "scope": "local", "key": "claude_max_parallel", "type": "int"},
        {"label": "Site URL", "value": config.site_url, "scope": "shared", "key": "site_url", "type": "str"},
        {"label": "Project key", "value": config.project_key, "scope": "shared", "key": "project_key", "type": "str"},
        {"label": "Board ID", "value": config.board_id or "", "scope": "shared", "key": "board_id", "type": "str"},
        {"label": "Heartbeat minutes", "value": config.poll_interval_minutes, "scope": "shared", "key": "poll_interval_minutes", "type": "int"},
    ]


def _save_selected_setup_field(config_path: str, config: AppConfig, state: UiState, raw: str) -> str:
    fields = _setup_fields(config)
    if not fields:
        return "No setup fields available."
    field = fields[_clamp_index(state.selected_setup_index, fields)]
    raw = raw.strip()
    if raw == "":
        return f"{field['label']} unchanged."
    value = raw
    if field["type"] == "int":
        try:
            value = int(raw)
        except ValueError:
            return f"{field['label']} must be a number."
    if field["scope"] == "local":
        _save_local_field(config, field["key"], value)
    else:
        _save_shared_field(config_path, field["key"], value)
    return f"Saved {field['label']}."


def _save_local_field(config: AppConfig, key: str, value) -> None:
    settings = load_operator_settings(config.operator_settings_path)
    updated = OperatorSettings(
        jira_email=settings.jira_email,
        token_file=settings.token_file or config.jira.token_file,
        claude_command=settings.claude_command or config.llm.command,
        claude_working_dir=settings.claude_working_dir or config.llm.working_dir,
        claude_max_parallel=settings.claude_max_parallel or config.llm.max_parallel,
    )
    setattr(updated, key, value)
    save_operator_settings(config.operator_settings_path, updated)


def _save_shared_field(config_path: str, key: str, value) -> None:
    path = Path(config_path)
    parsed = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    parsed[key] = value
    path.write_text(yaml.safe_dump(parsed, sort_keys=False), encoding="utf-8")


def _open_selected_terminal(
    config: AppConfig,
    state: UiState,
    board_rows: list[dict],
    archived_board_rows: list[dict],
) -> str:
    row = _selected_process_row(config, state, board_rows, archived_board_rows)
    if not row:
        return "No local Claude session for the selected ticket."
    if open_session_in_terminal(row):
        return f"Opened iTerm for {row['issue_key']}."
    return f"Could not open a new terminal for {row['issue_key']}."


def _inline_attach_selected(
    config: AppConfig,
    state: UiState,
    board_rows: list[dict],
    archived_board_rows: list[dict],
) -> str:
    row = _selected_process_row(config, state, board_rows, archived_board_rows)
    if not row:
        return "No local Claude session for the selected ticket."
    console.print()
    console.print(
        f"[bold green]Inline attach: {row['issue_key']}[/bold green] "
        f"[dim]({row.get('session_name') or '-'})[/dim]\n"
        "[yellow]Detach with Ctrl-b then d.[/] "
        "[red]Ctrl-C interrupts Claude and may end the session.[/]"
    )
    result = attach_session(row)
    if result is None:
        return f"Session for {row['issue_key']} is already gone."
    return f"Returned from inline session for {row['issue_key']}."


def _watch_selected(
    config: AppConfig,
    state: UiState,
    board_rows: list[dict],
    archived_board_rows: list[dict],
) -> str:
    row = _selected_process_row(config, state, board_rows, archived_board_rows)
    if not row:
        return "No local Claude session for the selected ticket."
    with _cbreak_input() as keyboard:
        with Live(
            _watch_panel(row, capture_session_output(row, lines=100)),
            console=console,
            screen=True,
            auto_refresh=False,
            transient=True,
        ) as live:
            while True:
                live.update(_watch_panel(row, capture_session_output(row, lines=100)), refresh=True)
                key = _read_key(timeout=float(LOCAL_REFRESH_SECONDS))
                if not key:
                    continue
                if key in {"q", "\x03", "v"}:
                    return f"Stopped watching {row['issue_key']}."
                keyboard.suspend()
                try:
                    if key in {"ENTER", "o"}:
                        if open_session_in_terminal(row):
                            return f"Opened iTerm for {row['issue_key']}."
                        return f"Could not open a new terminal for {row['issue_key']}."
                    if key == "i":
                        result = attach_session(row)
                        if result is None:
                            return f"Session for {row['issue_key']} is already gone."
                        return f"Returned from inline session for {row['issue_key']}."
                    if key == "x":
                        if Confirm.ask(f"Kill Claude for {row['issue_key']}?", default=False):
                            terminated = terminate_process(StateStore(config.local_state_dir), row)
                            if terminated:
                                return f"Sent TERM to Claude for {row['issue_key']}."
                            return f"Marked {row['issue_key']} as killed; process was already gone."
                finally:
                    keyboard.resume()


def _watch_panel(row: dict, output: str) -> Panel:
    lines = [
        Text(f"{row['issue_key']}  |  {row.get('state') or '-'}", style="bold cyan"),
        Text(f"Session: {row.get('session_name') or '-'}", style="white"),
        Text(f"Updated: {_short_timestamp(row.get('updated_at'))}", style="dim"),
        Text(""),
        Text(output or "No tmux output captured yet.", style="white"),
        Text(""),
        Text("Enter=open in iTerm  i=inline attach  x=kill  q=back", style="yellow"),
    ]
    return Panel(Group(*lines), title="Live Watch", border_style="cyan")


def _kill_selected(
    config: AppConfig,
    state: UiState,
    board_rows: list[dict],
    archived_board_rows: list[dict],
) -> str:
    row = _selected_process_row(config, state, board_rows, archived_board_rows)
    if not row:
        return "No local Claude session for the selected ticket."
    if not Confirm.ask(f"Kill Claude for {row['issue_key']}?", default=False):
        return "Kill cancelled."
    terminated = terminate_process(StateStore(config.local_state_dir), row)
    if terminated:
        return f"Sent TERM to Claude for {row['issue_key']}."
    return f"Marked {row['issue_key']} as killed; process was already gone."


def _selected_process_row(
    config: AppConfig,
    state: UiState,
    board_rows: list[dict],
    archived_board_rows: list[dict],
) -> dict | None:
    store = StateStore(config.local_state_dir)
    reconcile_processes(store)
    selected_issue = _selected_issue(board_rows, state.selected_board_index)
    if not selected_issue:
        return None
    live_rows = store.list_running_processes()
    return next((row for row in live_rows if row["issue_key"] == selected_issue["key"]), None)


def _display_statuses(config: AppConfig) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for route in config.routes:
        for value in [
            route.status,
            route.transition_on_launch,
            route.transition_on_success,
            route.transition_on_failure,
        ]:
            if not value or value in seen:
                continue
            seen.add(value)
            ordered.append(value)
    if "Done" not in seen:
        ordered.append("Done")
    return ordered


def _kind_label(row: dict) -> str:
    kind = row.get("issue_type") or "-"
    if kind == "Epic":
        story_count = row.get("epic_story_count")
        if story_count is not None:
            return f"Epic({story_count})"
        return "Epic"
    if row.get("parent_key"):
        parent_type = row.get("parent_issue_type") or "Parent"
        return f"{kind} -> {parent_type} {row['parent_key']}"
    return kind


def _selected_issue(rows: list[dict], index: int) -> dict | None:
    if not rows:
        return None
    return rows[_clamp_index(index, rows)]


def _selected_issue_key(rows: list[dict], index: int) -> str | None:
    issue = _selected_issue(rows, index)
    return issue.get("key") if issue else None


def _display_value(value) -> str:
    if value is None or value == "":
        return "-"
    return str(value)


def _grid_columns(rows: list[dict], statuses: list[str]) -> list[list[dict]]:
    return [[row for row in rows if row.get("status_name") == status] for status in statuses]


def _move_grid_key(rows: list[dict], statuses: list[str], current_key: str | None, direction: str) -> str | None:
    if not rows:
        return None
    columns = _grid_columns(rows, statuses)
    non_empty = [idx for idx, col in enumerate(columns) if col]
    if not non_empty:
        return rows[0].get("key")

    current_col, current_row = _find_grid_position(columns, current_key)
    if current_col is None:
        current_col = non_empty[0]
        current_row = 0

    if direction in {"up", "down"}:
        delta = -1 if direction == "up" else 1
        current_row = max(0, min(current_row + delta, len(columns[current_col]) - 1))
        return columns[current_col][current_row]["key"]

    delta = -1 if direction == "left" else 1
    next_col = current_col
    while True:
        next_col += delta
        if next_col < 0 or next_col >= len(columns):
            return columns[current_col][current_row]["key"]
        if columns[next_col]:
            next_row = min(current_row, len(columns[next_col]) - 1)
            return columns[next_col][next_row]["key"]


def _find_grid_position(columns: list[list[dict]], issue_key: str | None) -> tuple[int | None, int]:
    if not issue_key:
        return None, 0
    for col_idx, column in enumerate(columns):
        for row_idx, row in enumerate(column):
            if row.get("key") == issue_key:
                return col_idx, row_idx
    return None, 0


def _index_for_key(rows: list[dict], issue_key: str | None) -> int:
    if not rows:
        return 0
    if issue_key is None:
        return 0
    for idx, row in enumerate(rows):
        if row.get("key") == issue_key:
            return idx
    return 0


def _countdown(target: datetime | None, default_seconds: int) -> str:
    if target is None:
        return "manual"
    remaining = int((target - _utc_now()).total_seconds())
    if remaining <= 0:
        remaining = default_seconds
    minutes, seconds = divmod(remaining, 60)
    return f"{minutes:02d}:{seconds:02d}"


def _truncate(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def _short_timestamp(value: str | None) -> str:
    if not value:
        return "-"
    return value.replace("T", " ")[:16]


def _next_index(current: int, rows, delta: int) -> int:
    if not rows:
        return 0
    return max(0, min(current + delta, len(rows) - 1))


def _clamp_index(current: int, rows) -> int:
    if not rows:
        return 0
    return max(0, min(current, len(rows) - 1))


def _cycle_page(current: str, delta: int) -> str:
    try:
        index = PAGES.index(current)
    except ValueError:
        index = 0
    return PAGES[(index + delta) % len(PAGES)]


def _utc_now() -> datetime:
    return datetime.now(UTC)


class _cbreak_input:
    def __enter__(self):
        if not sys.stdin.isatty():
            self._fd = None
            self._old = None
            self._active = False
            return self
        self._fd = sys.stdin.fileno()
        self._old = termios.tcgetattr(self._fd)
        tty.setcbreak(self._fd)
        self._active = True
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._fd is not None and self._old is not None and self._active:
            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old)
            self._active = False

    def suspend(self) -> None:
        if self._fd is not None and self._old is not None and self._active:
            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old)
            self._active = False

    def resume(self) -> None:
        if self._fd is not None and self._old is not None and not self._active:
            tty.setcbreak(self._fd)
            self._active = True


def _read_key(timeout: float) -> str | None:
    if not sys.stdin.isatty():
        return None
    ready, _, _ = select.select([sys.stdin], [], [], timeout)
    if not ready:
        return None
    first = sys.stdin.read(1)
    if first in {"\r", "\n"}:
        return "ENTER"
    if first != "\x1b":
        return first
    sequence = first
    while True:
        ready, _, _ = select.select([sys.stdin], [], [], 0.05)
        if not ready:
            break
        sequence += sys.stdin.read(1)
        if len(sequence) >= 3 and sequence.startswith("\x1b["):
            break
    if sequence == "\x1b":
        return "ESC"
    return {
        "\x1b[A": "UP",
        "\x1b[B": "DOWN",
        "\x1b[C": "RIGHT",
        "\x1b[D": "LEFT",
    }.get(sequence)
