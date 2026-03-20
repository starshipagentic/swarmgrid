from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, UTC, timedelta
from getpass import getpass
import select
import shutil
import sys
import termios
import tty
import webbrowser

import yaml
from rich.columns import Columns
from rich.console import Console
from rich.console import Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich.text import Text

from .auth import resolve_auth_state, save_token_to_keychain
from .config import load_config
from .dagster_manager import get_dagster_status, start_dagster, stop_dagster
from .jira import JiraClient
from .operator_settings import OperatorSettings, load_operator_settings, save_operator_settings
from .service import get_status, run_heartbeat
from .state import StateStore
from .runner import apply_tmux_defaults, attach_session, capture_session_output, command_preview, max_parallel_runs, open_session_in_terminal, terminate_process


console = Console()


@dataclass
class DashboardState:
    message: str
    last_result: dict | None
    next_heartbeat_at: datetime | None
    auto_heartbeat: bool = True
    selected_process_index: int = 0


def run_menu(config_path: str) -> int:
    ensure_setup(config_path)
    apply_tmux_defaults()
    config = load_config(config_path)
    dashboard = DashboardState(
        message="Starting with a fresh heartbeat...",
        last_result=None,
        next_heartbeat_at=None,
        auto_heartbeat=True,
    )
    dashboard = _execute_heartbeat(config_path, config, dashboard, trigger="startup")
    keyboard = _cbreak_input()
    with keyboard:
        with Live(
            _render_dashboard(config_path, dashboard),
            console=console,
            screen=True,
            auto_refresh=False,
            transient=False,
        ) as live:
            while True:
                config = load_config(config_path)
                if (
                    dashboard.auto_heartbeat
                    and dashboard.next_heartbeat_at is not None
                    and _utc_now() >= dashboard.next_heartbeat_at
                ):
                    dashboard = _execute_heartbeat(
                        config_path,
                        config,
                        dashboard,
                        trigger="timer",
                    )
                live.update(_render_dashboard(config_path, dashboard), refresh=True)
                key = _read_key(timeout=1.0)
                if not key:
                    continue
                if key in {"q", "\x03"}:
                    return 0
                if key in {"UP", "k"}:
                    dashboard.selected_process_index = max(0, dashboard.selected_process_index - 1)
                    dashboard.message = "Selected previous running Claude session."
                    continue
                if key in {"DOWN", "j"}:
                    dashboard.selected_process_index += 1
                    dashboard.message = "Selected next running Claude session."
                    continue
                if key.isdigit() and key != "0":
                    dashboard.selected_process_index = int(key) - 1
                    dashboard.message = f"Selected running Claude session {key}."
                    continue
                if key == "h":
                    dashboard = _execute_heartbeat(
                        config_path,
                        config,
                        dashboard,
                        trigger="manual",
                    )
                    continue

                live.stop()
                try:
                    keyboard.suspend()
                    if key == "a" or key == "ENTER":
                        dashboard.message = attach_selected_process(config_path, dashboard)
                    elif key == "i":
                        dashboard.message = attach_selected_process_inline(config_path, dashboard)
                    elif key == "u":
                        ensure_setup(config_path, force_prompt=True)
                        dashboard.message = "Setup saved and Jira auth verified."
                    elif key == "t":
                        toggle_routes(config_path)
                        dashboard.message = "Route config updated."
                    elif key.isdigit():
                        route_index = int(key) - 1
                        route_label, enabled = toggle_route_index(config_path, route_index)
                        if route_label is None:
                            dashboard.message = f"No route bound to key {key}."
                        else:
                            state = "ON" if enabled else "OFF"
                            dashboard.message = f"{route_label} is now {state}. Press h to apply immediately."
                    elif key == "g":
                        dagster = get_dagster_status(config)
                        if dagster.running:
                            status = stop_dagster(config)
                            dashboard.message = (
                                "Dagster stopped."
                                if not status.running
                                else "Dagster stop did not complete."
                            )
                        else:
                            status = start_dagster(config)
                            webbrowser.open(status.url)
                            dashboard.message = f"Dagster running at {status.url}"
                    elif key == "x":
                        dashboard.message = kill_selected_process(config_path, dashboard)
                    elif key in {"-", "_"}:
                        new_limit = adjust_max_parallel(config_path, -1)
                        dashboard.message = f"Claude max parallel set to {new_limit}."
                    elif key in {"=", "+"}:
                        new_limit = adjust_max_parallel(config_path, 1)
                        dashboard.message = f"Claude max parallel set to {new_limit}."
                    elif key == "s":
                        dashboard.auto_heartbeat = not dashboard.auto_heartbeat
                        if dashboard.auto_heartbeat:
                            dashboard.next_heartbeat_at = _utc_now() + timedelta(
                                minutes=config.poll_interval_minutes
                            )
                            dashboard.message = "Auto heartbeat enabled."
                        else:
                            dashboard.next_heartbeat_at = None
                            dashboard.message = "Auto heartbeat paused."
                    elif key == "v":
                        show_tracked_issues(config_path)
                        _pause("Press Enter to return to the live dashboard")
                        dashboard.message = "Returned from tracked issues."
                    elif key == "d":
                        show_recent_decisions(config_path)
                        _pause("Press Enter to return to the live dashboard")
                        dashboard.message = "Returned from recent decisions."
                    elif key == "p":
                        show_prompt_preview(config_path)
                        _pause("Press Enter to return to the live dashboard")
                        dashboard.message = "Returned from prompt preview."
                    elif key == "r":
                        show_recent_process_runs(config_path)
                        _pause("Press Enter to return to the live dashboard")
                        dashboard.message = "Returned from recent process runs."
                    elif key == "w":
                        dashboard.message = watch_selected_process(config_path, dashboard)
                    else:
                        dashboard.message = f"Unknown key: {key}"
                finally:
                    keyboard.resume()
                    live.start(refresh=True)


def ensure_setup(config_path: str, force_prompt: bool = False) -> None:
    config = load_config(config_path)
    settings = load_operator_settings(config.operator_settings_path)
    auth = resolve_auth_state(config)

    claude_command_value = settings.claude_command or config.llm.command
    claude_detected = shutil.which(claude_command_value)
    token_needs_upgrade = auth.token_source not in {"env", "keychain"} and auth.token is not None
    if (
        not force_prompt
        and auth.email
        and auth.token
        and not token_needs_upgrade
        and claude_detected
        and settings.claude_working_dir
    ):
        return

    console.print()
    console.print(
        Panel.fit(
            "[bold cyan]Setup[/bold cyan]\n"
            "Shared board config stays in the repo.\n"
            "Personal machine settings are stored locally."
        )
    )
    _show_setup_snapshot(config, settings, auth, claude_detected)

    email = Prompt.ask("Atlassian email", default=auth.email or settings.jira_email or "").strip()
    if not email:
        raise RuntimeError("Atlassian email is required.")

    token_source = auth.token_source
    token_value = auth.token
    if auth.token and auth.token_source != "keychain":
        if Confirm.ask(
            f"Found token from {auth.token_source} ({auth.token_preview}). Import into macOS Keychain?",
            default=True,
        ):
            save_token_to_keychain(email, auth.token)
            token_source = "keychain"
            token_value = auth.token
            console.print("[green]Token imported into macOS Keychain.[/]")
    elif not auth.token:
        entered_token = getpass("Jira API token (input hidden): ").strip()
        if not entered_token:
            raise RuntimeError("A Jira API token is required.")
        save_token_to_keychain(email, entered_token)
        token_source = "keychain"
        token_value = entered_token
        console.print("[green]Token saved to macOS Keychain.[/]")

    claude_default = settings.claude_command or config.llm.command
    claude_command = Prompt.ask("Claude command path", default=claude_default).strip()

    working_dir_default = (
        settings.claude_working_dir
        or config.llm.working_dir
        or str(config.config_path.parent)
    )
    claude_working_dir = Prompt.ask(
        "Claude working directory", default=working_dir_default
    ).strip()

    save_operator_settings(
        config.operator_settings_path,
        OperatorSettings(
            jira_email=email,
            token_file=settings.token_file or config.jira.token_file,
            claude_command=claude_command,
            claude_working_dir=claude_working_dir,
            claude_max_parallel=settings.claude_max_parallel or config.llm.max_parallel,
        ),
    )

    try:
        validation = _validate_jira(config_path)
    except Exception as exc:
        console.print(f"[red]Jira auth failed:[/] {exc}")
        raise

    console.print(
        f"[green]Jira auth OK.[/] Connected as "
        f"[bold]{validation['display_name']}[/bold] ({validation['account_id']})"
    )
    apply_tmux_defaults()
    console.print(f"[green]Saved operator settings to[/] {config.operator_settings_path}")


def _show_setup_snapshot(
    config,
    settings: OperatorSettings,
    auth,
    claude_detected: str | None,
) -> None:
    table = Table(show_header=False, box=None)
    table.add_row("Email", auth.email or settings.jira_email or "[red]missing[/red]")
    table.add_row("Token source", auth.token_source)
    table.add_row("Token preview", auth.token_preview)
    table.add_row("Token file fallback", settings.token_file or config.jira.token_file)
    table.add_row("Claude", claude_detected or "[yellow]not detected[/yellow]")
    table.add_row(
        "Working dir",
        settings.claude_working_dir or config.llm.working_dir or str(config.config_path.parent),
    )
    table.add_row(
        "Max parallel",
        str(settings.claude_max_parallel or config.llm.max_parallel),
    )
    console.print(table)


def _validate_jira(config_path: str) -> dict:
    config = load_config(config_path)
    client = JiraClient(config)
    return client.validate_auth()


def show_tracked_issues(config_path: str) -> None:
    config = load_config(config_path)
    store = StateStore(config.local_state_dir)
    rows = store.list_issue_states()
    console.print()
    console.print("[bold cyan]Tracked issues[/bold cyan]")
    if not rows:
        console.print("[yellow]No tracked issues yet. Run a heartbeat first.[/]")
        return

    table = Table()
    table.add_column("Key", style="bold")
    table.add_column("Status", style="cyan")
    table.add_column("Kind", style="magenta")
    table.add_column("Tags", style="yellow")
    table.add_column("Assignee", style="green")
    table.add_column("Summary", style="white")
    for row in rows:
        table.add_row(
            row["issue_key"],
            row["status_name"],
            _hierarchy_label(row),
            _labels_label(row),
            row["assignee"] or "Unassigned",
            row["summary"],
        )
    console.print(table)


def show_recent_decisions(config_path: str) -> None:
    config = load_config(config_path)
    store = StateStore(config.local_state_dir)
    rows = store.list_recent_decisions()
    console.print()
    console.print("[bold cyan]Recent decisions[/bold cyan]")
    if not rows:
        console.print("[yellow]No decisions recorded yet.[/]")
        return

    table = Table()
    table.add_column("When", style="dim")
    table.add_column("Issue", style="bold")
    table.add_column("Action", style="cyan")
    table.add_column("Decision", style="magenta")
    table.add_column("Reason", style="green")
    for row in rows:
        table.add_row(
            row["created_at"],
            row["issue_key"],
            row["action_name"],
            "launch" if row["should_launch"] else "skip",
            row["reason"],
        )
    console.print(table)


def toggle_routes(config_path: str) -> None:
    config = load_config(config_path)
    with config.config_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}

    routes = raw.get("routes", [])
    if not routes:
        console.print("[yellow]No routes configured.[/]")
        return

    table = Table(title="Toggle routes")
    table.add_column("#", style="bold")
    table.add_column("Status", style="cyan")
    table.add_column("Action", style="magenta")
    table.add_column("Enabled", style="green")
    for index, route in enumerate(routes, start=1):
        table.add_row(
            str(index),
            route.get("status", ""),
            route.get("action", ""),
            "yes" if route.get("enabled") else "no",
        )
    console.print(table)

    choice = Prompt.ask("Toggle which route number", default="").strip()
    if not choice.isdigit():
        console.print("[yellow]No change made.[/]")
        return

    selected = int(choice) - 1
    if selected < 0 or selected >= len(routes):
        console.print("[yellow]No change made.[/]")
        return

    routes[selected]["enabled"] = not bool(routes[selected].get("enabled"))
    raw["routes"] = routes

    with config.config_path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(raw, handle, sort_keys=False)

    state = "enabled" if routes[selected]["enabled"] else "disabled"
    console.print(f"[green]{routes[selected]['status']}[/] is now [bold]{state}[/].")


def toggle_route_index(config_path: str, route_index: int) -> tuple[str | None, bool | None]:
    config = load_config(config_path)
    with config.config_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}

    routes = raw.get("routes", [])
    if route_index < 0 or route_index >= len(routes):
        return None, None

    routes[route_index]["enabled"] = not bool(routes[route_index].get("enabled"))
    raw["routes"] = routes
    with config.config_path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(raw, handle, sort_keys=False)
    return routes[route_index].get("status"), bool(routes[route_index].get("enabled"))


def adjust_max_parallel(config_path: str, delta: int) -> int:
    config = load_config(config_path)
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
    return updated


def show_recent_process_runs(config_path: str) -> None:
    config = load_config(config_path)
    store = StateStore(config.local_state_dir)
    rows = store.list_recent_process_runs()
    archived_rows = store.list_archived_processes(limit=20)
    console.print()
    console.print("[bold cyan]Process archive[/bold cyan]")
    if not rows and not archived_rows:
        console.print("[yellow]No process runs recorded yet.[/]")
        return

    table = Table()
    table.add_column("Issue", style="bold")
    table.add_column("Action", style="cyan")
    table.add_column("State", style="magenta")
    table.add_column("Live", style="yellow")
    table.add_column("Archived", style="dim")
    table.add_column("Prompt", style="white")
    table.add_column("Proofs", style="green")
    table.add_column("Log", style="dim")
    for row in rows:
        proofs = row["artifact_paths"].replace("\n", ", ") if row["artifact_paths"] else "-"
        table.add_row(
            row["issue_key"],
            row["action_name"] or "-",
            row["state"],
            "yes" if row.get("is_live") else "no",
            row.get("archived_reason") or "-",
            row["prompt"] or "-",
            proofs,
            row["log_path"],
        )
    console.print(table)


def kill_running_process_prompt(config_path: str) -> str:
    config = load_config(config_path)
    store = StateStore(config.local_state_dir)
    rows = store.list_running_processes()
    if not rows:
        return "No running Claude process to kill."

    table = Table(title="Kill running Claude")
    table.add_column("#", style="bold")
    table.add_column("Issue", style="cyan")
    table.add_column("Action", style="magenta")
    table.add_column("PID", style="yellow")
    for index, row in enumerate(rows, start=1):
        table.add_row(
            str(index),
            row["issue_key"],
            row.get("action_name") or "-",
            str(row.get("pid") or "-"),
        )
    console.print(table)

    choice = Prompt.ask("Kill which running process number", default="1").strip()
    if not choice.isdigit():
        return "Kill cancelled."
    selected = int(choice) - 1
    if selected < 0 or selected >= len(rows):
        return "Kill cancelled."

    row = rows[selected]
    terminated = terminate_process(store, row)
    issue_key = row["issue_key"]
    if terminated:
        return f"Sent TERM to Claude for {issue_key}."
    return f"Marked {issue_key} as killed; process was already gone."


def attach_selected_process(config_path: str, dashboard: DashboardState) -> str:
    config = load_config(config_path)
    store = StateStore(config.local_state_dir)
    rows = store.list_running_processes()
    if not rows:
        return "No running Claude session to attach."

    selected = _clamp_selected_index(dashboard.selected_process_index, rows)
    dashboard.selected_process_index = selected
    row = rows[selected]
    issue_key = row["issue_key"]
    session_name = row.get("session_name")
    if not session_name:
        return f"{issue_key} is not running in tmux, so there is nothing to attach."
    opened = open_session_in_terminal(row)
    if not opened:
        return (
            f"Could not open a new Terminal window for {issue_key}. "
            "The tmux session may still be running."
        )
    return (
        f"Opened Terminal for {issue_key}. "
        "Detach safely with Ctrl-b then d."
    )


def attach_selected_process_inline(config_path: str, dashboard: DashboardState) -> str:
    config = load_config(config_path)
    store = StateStore(config.local_state_dir)
    rows = store.list_running_processes()
    if not rows:
        return "No running Claude session to attach."

    selected = _clamp_selected_index(dashboard.selected_process_index, rows)
    dashboard.selected_process_index = selected
    row = rows[selected]
    issue_key = row["issue_key"]
    session_name = row.get("session_name")
    if not session_name:
        return f"{issue_key} is not running in tmux, so there is nothing to attach."
    console.print()
    console.print(
        f"[bold green]Inline attach: {issue_key}[/bold green] "
        f"[dim]({session_name})[/dim]\n"
        "[yellow]Detach with Ctrl-b then d.[/] "
        "[red]Ctrl-C interrupts Claude and may end the session.[/]"
    )
    result = attach_session(row)
    if result is None:
        return f"Claude session for {issue_key} is already gone."
    return f"Returned from inline Claude session for {issue_key}."


def kill_selected_process(config_path: str, dashboard: DashboardState) -> str:
    config = load_config(config_path)
    store = StateStore(config.local_state_dir)
    rows = store.list_running_processes()
    if not rows:
        return "No running Claude process to kill."

    selected = _clamp_selected_index(dashboard.selected_process_index, rows)
    dashboard.selected_process_index = selected
    row = rows[selected]
    issue_key = row["issue_key"]
    session_name = row.get("session_name")
    label = f"{issue_key} ({session_name})" if session_name else issue_key
    if not Confirm.ask(f"Kill Claude for {label}?", default=False):
        return "Kill cancelled."
    terminated = terminate_process(store, row)
    if terminated:
        return f"Sent TERM to Claude for {issue_key}."
    return f"Marked {issue_key} as killed; process was already gone."


def show_prompt_preview(config_path: str) -> None:
    config = load_config(config_path)
    store = StateStore(config.local_state_dir)
    decisions = store.list_recent_decisions(limit=20)
    issues_by_key = {row["issue_key"]: row for row in store.list_issue_states()}
    console.print()
    console.print("[bold cyan]Prompt preview[/bold cyan]")
    if not decisions:
        console.print("[yellow]No decisions recorded yet. Run a heartbeat first.[/]")
        return

    seen: set[str] = set()
    for row in decisions:
        issue_key = row["issue_key"]
        if issue_key in seen:
            continue
        seen.add(issue_key)
        issue = issues_by_key.get(issue_key, {})
        prompt = row.get("prompt") or "-"
        command_preview = _command_preview(config_path, prompt) if prompt != "-" else "-"
        header = (
            f"[bold]{issue_key}[/bold]  "
            f"[magenta]{_hierarchy_label(issue)}[/magenta]  "
            f"[cyan]{row.get('status_name', '-')}[/cyan]  "
            f"[green]{row.get('reason', '-')}[/green]"
        )
        lines = [
            issue.get("summary", "-"),
            f"[dim]prompt[/dim]: [magenta]{prompt}[/magenta]",
            f"[dim]cmd[/dim]: [yellow]{command_preview}[/yellow]",
        ]
        parent_key = issue.get("parent_key")
        if parent_key:
            parent_type = issue.get("parent_issue_type") or "Parent"
            parent_summary = issue.get("parent_summary") or ""
            lines.append(
                f"[dim]parent[/dim]: [blue]{parent_type} {parent_key}[/blue] {parent_summary}".rstrip()
            )
        console.print(Panel("\n".join(lines), title=header, border_style="cyan"))


def show_selected_session_output(config_path: str, dashboard: DashboardState) -> None:
    config = load_config(config_path)
    store = StateStore(config.local_state_dir)
    rows = store.list_running_processes()
    console.print()
    console.print("[bold cyan]Selected session output[/bold cyan]")
    if not rows:
        console.print("[yellow]No running Claude session.[/]")
        return

    selected = _clamp_selected_index(dashboard.selected_process_index, rows)
    dashboard.selected_process_index = selected
    row = rows[selected]
    output = capture_session_output(row, lines=80).strip()
    if not output:
        console.print("[yellow]No output captured yet.[/]")
        return

    title = f"{row['issue_key']}  {row.get('session_name') or '-'}"
    console.print(Panel(output, title=title, border_style="green"))


def watch_selected_process(config_path: str, dashboard: DashboardState) -> str:
    config = load_config(config_path)
    store = StateStore(config.local_state_dir)
    rows = store.list_running_processes()
    if not rows:
        return "No running Claude session to watch."

    selected = _clamp_selected_index(dashboard.selected_process_index, rows)
    dashboard.selected_process_index = selected
    row = rows[selected]
    issue_key = row["issue_key"]

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
                key = _read_key(timeout=1.0)
                if not key or key == "w":
                    continue
                if key in {"q", "\x03"}:
                    return f"Stopped watching {issue_key}."
                if key in {"a", "ENTER"}:
                    keyboard.suspend()
                    try:
                        opened = open_session_in_terminal(row)
                    finally:
                        keyboard.resume()
                    if opened:
                        return f"Opened Terminal for {issue_key}. Detach safely with Ctrl-b then d."
                    return f"Could not open a new Terminal window for {issue_key}."
                if key == "i":
                    keyboard.suspend()
                    try:
                        result = attach_session(row)
                    finally:
                        keyboard.resume()
                    if result is None:
                        return f"Claude session for {issue_key} is already gone."
                    return f"Returned from inline Claude session for {issue_key}."
                if key == "x":
                    keyboard.suspend()
                    try:
                        if Confirm.ask(f"Kill Claude for {issue_key}?", default=False):
                            terminated = terminate_process(store, row)
                            if terminated:
                                return f"Sent TERM to Claude for {issue_key}."
                            return f"Marked {issue_key} as killed; process was already gone."
                    finally:
                        keyboard.resume()
                    live.refresh()


def _command_preview(config_path: str, prompt: str) -> str:
    config = load_config(config_path)
    return command_preview(config, prompt)


def _render_dashboard(config_path: str, dashboard: DashboardState):
    config = load_config(config_path)
    store = StateStore(config.local_state_dir)
    summary = get_status(config_path)
    dagster = get_dagster_status(config)
    auth = resolve_auth_state(config)
    issues = (
        dashboard.last_result.get("issues", [])
        if dashboard.last_result
        else store.list_issue_states()
    )
    decisions = (
        dashboard.last_result.get("decisions", [])
        if dashboard.last_result
        else store.list_recent_decisions(limit=8)
    )
    running_processes = store.list_running_processes()
    archived_processes = store.list_archived_processes(limit=8)
    dashboard.selected_process_index = _clamp_selected_index(
        dashboard.selected_process_index,
        running_processes,
    )

    layout = Layout()
    layout.split_column(
        Layout(_header_panel(config, summary, auth, dagster, dashboard, issues), size=8),
        Layout(name="body", ratio=1),
        Layout(_footer_panel(), size=3),
    )
    layout["body"].split_row(
        Layout(_tickets_panel(config_path, config, issues, decisions), ratio=3),
        Layout(name="right", ratio=2),
    )
    layout["body"]["right"].split_column(
        Layout(_control_panel(config, issues, decisions, running_processes, dashboard), ratio=2),
        Layout(_process_panel(running_processes, archived_processes, dashboard.selected_process_index), ratio=1),
    )
    return layout


def _header_panel(config, summary: dict, auth, dagster, dashboard: DashboardState, issues: list[dict]) -> Panel:
    countdown = _heartbeat_countdown(config, dashboard)
    max_parallel = max_parallel_runs(config)
    current_issue_count = len(issues)
    current_decision_count = (
        len(dashboard.last_result.get("decisions", []))
        if dashboard.last_result
        else summary["decision_count"]
    )
    lines = [
        "[bold cyan]SwarmGrid Heartbeat[/bold cyan]",
        (
            f"Statuses: [yellow]{', '.join(summary['watched_statuses']) or 'none'}[/yellow]    "
            f"Board now: [green]{current_issue_count}[/green]    "
            f"Decisions: [green]{current_decision_count}[/green]    "
            f"Running: [green]{summary['running_count']}[/green]/[green]{max_parallel}[/green]    "
            f"Archived: [green]{summary.get('archived_count', 0)}[/green]"
        ),
        (
            f"Jira auth: [magenta]{auth.token_source}[/magenta] "
            f"[magenta]{auth.token_preview}[/magenta]    "
            f"Dagster: [{'green' if dagster.running else 'red'}]"
            f"{'running' if dagster.running else 'stopped'}[/] [blue]{dagster.url}[/blue]"
        ),
        (
            f"Next heartbeat: [yellow]{countdown}[/yellow]    "
            f"Auto: [{'green' if dashboard.auto_heartbeat else 'red'}]"
            f"{'on' if dashboard.auto_heartbeat else 'off'}[/]"
        ),
        f"[white]{dashboard.message}[/white]",
    ]
    return Panel("\n".join(lines), border_style="bright_blue")


def _tickets_panel(config_path: str, config, issues: list[dict], decisions: list[dict]) -> Panel:
    issues_by_key = {
        _issue_row_key(row): row
        for row in issues
        if _issue_row_key(row)
    }
    cards = []
    seen: set[str] = set()
    for decision in decisions:
        issue_key = decision["issue_key"]
        if issue_key in seen:
            continue
        seen.add(issue_key)
        issue = issues_by_key.get(issue_key, {})
        prompt = decision.get("prompt") or "-"
        command_preview = _command_preview(config_path, prompt) if prompt != "-" else "-"
        action_line = _decision_action_line(config, decision)
        details = [
            Text(issue.get("summary", "-"), style="bold white"),
            Text(
                f"{_hierarchy_label(issue)}  |  {decision.get('status_name', '-')}  |  {_friendly_reason(decision.get('reason', '-'))}",
                style="magenta",
            ),
            Text(f"next: {action_line}", style="cyan"),
            Text(f"prompt: {prompt}", style="bright_magenta"),
            Text(f"cmd: {command_preview}", style="yellow"),
        ]
        labels = _labels_label(issue)
        if labels != "-":
            details.append(Text(f"tags: {labels}", style="bright_black"))
        assignee = issue.get("assignee")
        if assignee:
            details.append(Text(f"assignee: {assignee}", style="green"))
        cards.append(Panel(Group(*details), title=issue_key, border_style="blue"))
        if len(cards) == 4:
            break

    if not cards:
        cards = [Panel("No decisions recorded yet.", border_style="blue")]

    return Panel(
        Group(*cards),
        title="Tickets",
        border_style="bright_magenta",
    )


def _issue_row_key(row: dict) -> str | None:
    return row.get("issue_key") or row.get("key")


def _control_panel(
    config,
    issues: list[dict],
    decisions: list[dict],
    running_processes: list[dict],
    dashboard: DashboardState,
) -> Panel:
    current_by_status = _issue_counts_by_status(issues)
    running_count = len(running_processes)
    max_parallel = max_parallel_runs(config)
    running_keys = [row["issue_key"] for row in running_processes[:3]]
    queued_keys = _queued_issue_keys(dashboard.last_result, running_keys)
    route_table = Table(expand=True)
    route_table.add_column("Key", style="bold", no_wrap=True)
    route_table.add_column("Route", style="cyan")
    route_table.add_column("State", style="magenta", no_wrap=True)
    route_table.add_column("Now", style="yellow", no_wrap=True)
    route_table.add_column("Next", style="green")
    for index, route in enumerate(config.routes, start=1):
        current_count = current_by_status.get(route.status, 0)
        route_table.add_row(
            str(index),
            route.status,
            "ON" if route.enabled else "OFF",
            str(current_count),
            _route_gate_summary(config, route, current_count),
        )

    call_to_action = _call_to_action(config, issues, dashboard)
    body = [
        Text("Heartbeat / Routes", style="bold cyan"),
        Text(call_to_action, style="white"),
        Text(""),
        Text(f"Claude capacity: {running_count}/{max_parallel} running    use [-] and [=] to change", style="white"),
        Text(f"Running now: {', '.join(running_keys) if running_keys else 'none'}", style="green"),
        Text(f"Queued next: {', '.join(queued_keys) if queued_keys else 'none'}", style="yellow"),
        Text(""),
        route_table,
    ]
    return Panel(Group(*body), title="Control", border_style="cyan")


def _process_panel(running_rows: list[dict], archived_rows: list[dict], selected_index: int) -> Panel:
    running_table = Table(expand=True)
    running_table.add_column("", no_wrap=True, width=2)
    running_table.add_column("Issue", style="bold", no_wrap=True)
    running_table.add_column("Session", style="cyan", no_wrap=True)
    running_table.add_column("Mode", style="magenta", no_wrap=True)
    running_table.add_column("Updated", style="dim", no_wrap=True)

    if not running_rows:
        running_table.add_row("-", "-", "-", "-", "No running Claude sessions.")
    else:
        selected = _clamp_selected_index(selected_index, running_rows)
        for index, row in enumerate(running_rows):
            marker = ">" if index == selected else " "
            style = "bold green" if index == selected else "white"
            running_table.add_row(
                marker,
                row["issue_key"],
                row.get("session_name") or "-",
                row.get("launch_mode") or "-",
                _short_timestamp(row["updated_at"]),
                style=style,
            )

    archived_count = len(archived_rows)
    archived_preview = ", ".join(row["issue_key"] for row in archived_rows[:3]) if archived_rows else "none"

    return Panel(
        Group(
            Text("Live sessions  [j/k or up/down] move  [1-9] direct select", style="bold green"),
            Text("[enter/a] iTerm   [i] inline tmux   [w] watch live   [x] kill", style="bold green"),
            running_table,
            Text(f"Archived sessions: {archived_count}   [r] archive list   preview: {archived_preview}", style="cyan"),
        ),
        title="Processes",
        border_style="green",
    )


def _watch_panel(row: dict, output: str) -> Panel:
    body = Group(
        Text(f"Issue: {row['issue_key']}", style="bold green"),
        Text(f"Session: {row.get('session_name') or '-'}", style="cyan"),
        Text("[enter/a] iTerm   [i] inline tmux   [x] kill   [q] back", style="yellow"),
        Text(""),
        Text(output or "(no output yet)", style="white"),
    )
    return Panel(body, title="Live Watch", border_style="bright_green")


def _footer_panel() -> Panel:
    return Panel(
        "[bold]Keys[/bold]  [cyan]h[/cyan] heartbeat  "
        "[cyan]j/k[/cyan] select session  "
        "[cyan]1-9[/cyan] direct session  "
        "[cyan]up/down[/cyan] select session  "
        "[cyan]a[/cyan] open terminal  "
        "[cyan]enter[/cyan] open terminal  "
        "[cyan]i[/cyan] inline tmux  "
        "[cyan]w[/cyan] watch live  "
        "[cyan]u[/cyan] auth/setup  "
        "[cyan]s[/cyan] auto on/off  "
        "[cyan]-/=[/cyan] max claudes  "
        "[cyan]t[/cyan] routes menu  "
        "[cyan]x[/cyan] kill claude  "
        "[cyan]g[/cyan] dagster on/off  "
        "[cyan]v[/cyan] issues  "
        "[cyan]p[/cyan] prompt detail  "
        "[cyan]d[/cyan] decisions  "
        "[cyan]r[/cyan] archive  "
        "[cyan]q[/cyan] quit",
        border_style="white",
    )


def _hierarchy_label(issue: dict) -> str:
    issue_type = issue.get("issue_type") or "-"
    if issue_type == "Epic":
        story_count = issue.get("epic_story_count")
        if story_count is None:
            return "Epic(?)"
        if story_count == 0:
            return "Epic(0)"
        return f"Epic({story_count})"

    parent_key = issue.get("parent_key")
    parent_type = issue.get("parent_issue_type")
    if parent_key and parent_type:
        return f"{issue_type} -> {parent_type} {parent_key}"
    if parent_key:
        return f"{issue_type} -> {parent_key}"
    return issue_type


def _labels_label(issue: dict) -> str:
    labels = issue.get("labels") or []
    if not labels:
        return "-"
    visible = labels[:1]
    suffix = f" +{len(labels) - len(visible)}" if len(labels) > len(visible) else ""
    return ", ".join(visible) + suffix


def _friendly_reason(reason: str) -> str:
    mapping = {
        "status_unchanged": "unchanged; not re-triggering",
        "route_disabled": "route is disabled",
        "llm_disabled": "LLM is disabled",
        "dry_run": "dry-run only",
        "ready_to_launch": "ready to launch",
        "already_running": "already running",
        "first_seen_suppressed": "first-seen launch suppressed",
    }
    if reason.startswith("unsupported_issue_type:"):
        return f"wrong type ({reason.split(':', 1)[1]})"
    return mapping.get(reason, reason)


def _decision_action_line(config, decision: dict) -> str:
    if decision.get("should_launch"):
        return "launch now"
    return _friendly_reason(decision.get("reason", "-"))


def _issue_counts_by_status(issues: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for issue in issues:
        status = issue.get("status_name") or "-"
        counts[status] = counts.get(status, 0) + 1
    return counts


def _route_gate_summary(config, route, current_count: int) -> str:
    if not route.enabled:
        if current_count:
            return "press key to arm current tickets"
        return "waiting for tickets"
    if not config.llm.enabled:
        return "will preview, but LLM is off"
    if config.llm.dry_run:
        return "preview only; no launch"
    if current_count:
        return f"eligible now for {route.action}"
    return f"armed for next {route.action}"


def _call_to_action(config, issues: list[dict], dashboard: DashboardState) -> str:
    droid_do_route = next((route for route in config.routes if route.status == "Droid-Do"), None)
    droid_do_count = sum(1 for issue in issues if issue.get("status_name") == "Droid-Do")
    if droid_do_route is None:
        return "No Droid-Do route is configured."
    if not droid_do_route.enabled:
        if droid_do_count:
            return (
                f"Droid-Do is OFF. There are {droid_do_count} current tickets there. Press [1] to arm the route, "
                "then press h to reconsider those current tickets immediately."
            )
        return "Droid-Do is OFF. Press [1] to arm it for the next Story/Task entering Droid-Do."
    if not config.llm.enabled:
        return "Droid-Do is ON, but LLM is disabled, so heartbeats will preview only."
    if config.llm.dry_run:
        return "Droid-Do is ON in dry-run mode. Current eligible tickets will be previewed, but no Claude process will launch."
    if dashboard.last_result and dashboard.last_result.get("launched_count", 0):
        launched = ", ".join(item["issue_key"] for item in dashboard.last_result.get("launches", []))
        return f"Last heartbeat launched: {launched}"
    if droid_do_count:
        return "Droid-Do is armed. Press h to process the current eligible tickets now."
    return "Droid-Do is armed. The next Story/Task entering Droid-Do will launch Claude."


def _queued_issue_keys(last_result: dict | None, running_keys: list[str]) -> list[str]:
    if not last_result:
        return []
    launched_keys = {item["issue_key"] for item in last_result.get("launches", [])}
    running_key_set = set(running_keys)
    queued = []
    for decision in last_result.get("decisions", []):
        if not decision.get("should_launch"):
            continue
        issue_key = decision["issue_key"]
        if issue_key in launched_keys or issue_key in running_key_set:
            continue
        queued.append(issue_key)
    return queued


def _heartbeat_countdown(config, dashboard: DashboardState) -> str:
    if not dashboard.auto_heartbeat or dashboard.next_heartbeat_at is None:
        return "manual only"
    remaining = int((dashboard.next_heartbeat_at - _utc_now()).total_seconds())
    if remaining <= 0:
        return "due now"
    minutes, seconds = divmod(remaining, 60)
    return f"{minutes:02d}:{seconds:02d}"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _execute_heartbeat(
    config_path: str,
    config,
    dashboard: DashboardState,
    trigger: str,
) -> DashboardState:
    force_reconsider = trigger == "manual"
    result = run_heartbeat(config_path, force_reconsider=force_reconsider)
    launched = [item["issue_key"] for item in result.get("launches", [])]
    launched_text = ", ".join(launched) if launched else "none"
    decision_summaries = []
    for decision in result.get("decisions", [])[:3]:
        decision_summaries.append(
            f"{decision['issue_key']}={_friendly_reason(decision.get('reason', '-'))}"
        )
    decision_text = "; ".join(decision_summaries) if decision_summaries else "no route decisions"
    prefix = {
        "startup": "Startup heartbeat complete.",
        "manual": "Manual heartbeat complete (forced current-ticket reconsideration).",
        "timer": "Scheduled heartbeat complete.",
    }.get(trigger, "Heartbeat complete.")
    dashboard.last_result = result
    dashboard.message = (
        f"{prefix} Issues={result['issue_count']} launched={result['launched_count']} ({launched_text}). "
        f"{decision_text}."
    )
    dashboard.next_heartbeat_at = _utc_now() + timedelta(minutes=config.poll_interval_minutes)
    return dashboard


def _short_timestamp(value: str | None) -> str:
    if not value:
        return "-"
    return value.replace("T", " ")[:16]


def _clamp_selected_index(selected_index: int, rows: list[dict]) -> int:
    if not rows:
        return 0
    return max(0, min(selected_index, len(rows) - 1))


def _pause(message: str) -> None:
    console.print()
    console.print(f"[dim]{message}[/dim]")
    input()


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
    mapping = {
        "\x1b[A": "UP",
        "\x1b[B": "DOWN",
        "\x1b[C": "RIGHT",
        "\x1b[D": "LEFT",
    }
    return mapping.get(sequence, None)
