from __future__ import annotations

from datetime import datetime, UTC
from pathlib import Path
import glob
import os
import re
import signal
import shlex
import shutil
import subprocess
import textwrap
import time

from .config import AppConfig
from .models import JiraIssue, LaunchRecord, RouteDecision, RunReconciliation
from .operator_settings import load_operator_settings
from .state import StateStore, pid_is_alive


ACTIVE_OUTPUT_SECONDS = 120
IDLE_OUTPUT_SECONDS = 3600


def timestamp() -> str:
    return datetime.now(UTC).isoformat()


def reconcile_processes(store: StateStore) -> None:
    for row in store.list_running_processes():
        session_name = row.get("session_name")
        if session_name:
            if _tmux_session_exists(session_name):
                continue
            store.update_process_state(row["id"], "exited", timestamp())
            continue
        pid = row["pid"]
        if pid is None:
            store.update_process_state(row["id"], "unknown", timestamp())
            continue
        if pid_is_alive(pid):
            continue
        store.update_process_state(row["id"], "exited", timestamp())


def launch_decision(
    config: AppConfig,
    store: StateStore,
    decision: RouteDecision,
) -> LaunchRecord:
    created_at = timestamp()
    slug = created_at.replace(":", "-")
    run_dir = store.run_artifacts_dir / decision.issue_key / slug
    run_dir.mkdir(parents=True, exist_ok=True)

    prompt_path = run_dir / "prompt.txt"
    prompt_path.write_text(decision.prompt + "\n", encoding="utf-8")

    log_path = run_dir / "command.log"
    session_name: str | None = None
    launch_mode = "subprocess"
    if _should_use_tmux(config):
        command = build_interactive_command(config)
        command_line = command_preview(config, decision.prompt)
    else:
        command = build_command(config, decision.prompt)
        command_line = shlex.join(command)
    artifact_globs = _resolve_artifact_globs(decision, run_dir)
    working_dir = _resolve_working_dir(config)

    if shutil.which(command[0]) is None:
        launch = LaunchRecord(
            run_id=None,
            issue_key=decision.issue_key,
            status_name=decision.status_name,
            action=decision.action,
            prompt=decision.prompt,
            state="command_missing",
            pid=None,
            log_path=str(log_path),
            command_line=command_line,
            run_dir=str(run_dir),
            artifact_globs=artifact_globs,
            session_name=None,
            launch_mode=launch_mode,
            transition_on_launch=decision.transition_on_launch,
            transition_on_success=decision.transition_on_success,
            transition_on_failure=decision.transition_on_failure,
            comment_on_launch=decision.comment_on_launch,
            comment_on_success=decision.comment_on_success,
            comment_on_failure=decision.comment_on_failure,
        )
        launch.run_id = store.record_process_run(launch, created_at=created_at)
        return launch

    if _should_use_tmux(config):
        session_name = _session_name(decision.issue_key, slug)
        _launch_tmux_session(
            session_name=session_name,
            working_dir=working_dir,
            command=command,
            prompt=decision.prompt,
            log_path=log_path,
        )
        pid = _tmux_pane_pid(session_name)
        launch_mode = "tmux"
    else:
        with log_path.open("a", encoding="utf-8") as log_handle:
            process = subprocess.Popen(
                command,
                cwd=working_dir,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
        pid = process.pid

    _write_launch_metadata(
        run_dir=run_dir,
        issue_key=decision.issue_key,
        prompt=decision.prompt,
        command_line=command_line,
        working_dir=working_dir,
        session_name=session_name,
        launch_mode=launch_mode,
    )

    launch = LaunchRecord(
        run_id=None,
        issue_key=decision.issue_key,
        status_name=decision.status_name,
        action=decision.action,
        prompt=decision.prompt,
        state="running",
        pid=pid,
        log_path=str(log_path),
        command_line=command_line,
        run_dir=str(run_dir),
        artifact_globs=artifact_globs,
        session_name=session_name,
        launch_mode=launch_mode,
        transition_on_launch=decision.transition_on_launch,
        transition_on_success=decision.transition_on_success,
        transition_on_failure=decision.transition_on_failure,
        comment_on_launch=decision.comment_on_launch,
        comment_on_success=decision.comment_on_success,
        comment_on_failure=decision.comment_on_failure,
    )
    launch.run_id = store.record_process_run(launch, created_at=created_at)
    return launch


def launch_manual_tmux_shell(
    config: AppConfig,
    store: StateStore,
    issue: JiraIssue,
) -> LaunchRecord:
    created_at = timestamp()
    slug = created_at.replace(":", "-")
    run_dir = store.run_artifacts_dir / issue.key / slug
    run_dir.mkdir(parents=True, exist_ok=True)

    prompt_path = run_dir / "prompt.txt"
    prompt_path.write_text("manual tmux shell\n", encoding="utf-8")

    log_path = run_dir / "command.log"
    session_name = _session_name(issue.key, slug)
    working_dir = _resolve_working_dir(config)
    command_line = f"tmux shell -> {_default_shell()}"

    if shutil.which("tmux") is None:
        launch = LaunchRecord(
            run_id=None,
            issue_key=issue.key,
            status_name=issue.status_name,
            action="manual_tmux_shell",
            prompt="",
            state="command_missing",
            pid=None,
            log_path=str(log_path),
            command_line=command_line,
            run_dir=str(run_dir),
            artifact_globs=[],
            session_name=None,
            launch_mode="tmux",
        )
        launch.run_id = store.record_process_run(launch, created_at=created_at)
        return launch

    _launch_tmux_session(
        session_name=session_name,
        working_dir=working_dir,
        command=None,
        prompt=None,
        log_path=log_path,
    )
    pid = _tmux_pane_pid(session_name)

    _write_launch_metadata(
        run_dir=run_dir,
        issue_key=issue.key,
        prompt="",
        command_line=command_line,
        working_dir=working_dir,
        session_name=session_name,
        launch_mode="tmux",
    )

    launch = LaunchRecord(
        run_id=None,
        issue_key=issue.key,
        status_name=issue.status_name,
        action="manual_tmux_shell",
        prompt="",
        state="running",
        pid=pid,
        log_path=str(log_path),
        command_line=command_line,
        run_dir=str(run_dir),
        artifact_globs=[],
        session_name=session_name,
        launch_mode="tmux",
    )
    launch.run_id = store.record_process_run(launch, created_at=created_at)
    return launch


def max_parallel_runs(config: AppConfig) -> int:
    settings = load_operator_settings(config.operator_settings_path)
    configured = settings.claude_max_parallel
    if configured is None:
        configured = config.llm.max_parallel
    return max(1, int(configured))


def classify_process_row(row: dict) -> str:
    state = row.get("state")
    if state != "running":
        if not row.get("is_live", 1):
            return "archived"
        return state or "unknown"

    session_name = row.get("session_name")
    if session_name:
        if not _tmux_session_exists(session_name):
            return "dead"
    else:
        pid = row.get("pid")
        if pid is not None and not pid_is_alive(pid):
            return "dead"

    log_path = row.get("log_path")
    if log_path and Path(log_path).exists():
        age_seconds = max(0.0, time.time() - Path(log_path).stat().st_mtime)
        if age_seconds <= ACTIVE_OUTPUT_SECONDS:
            return "active"
        if age_seconds <= IDLE_OUTPUT_SECONDS:
            return "idle"
        return "stale"
    return "idle"


def apply_tmux_defaults() -> bool:
    if shutil.which("tmux") is None:
        return False
    subprocess.run(
        ["tmux", "start-server"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    result = subprocess.run(
        ["tmux", "set-option", "-g", "mouse", "on"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        ["tmux", "set-option", "-g", "set-clipboard", "on"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def terminate_process(store: StateStore, row: dict) -> bool:
    session_name = row.get("session_name")
    if session_name:
        if not _tmux_session_exists(session_name):
            store.update_process_state(row["id"], "killed", timestamp())
            return False
        subprocess.run(
            ["tmux", "kill-session", "-t", session_name],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        store.update_process_state(row["id"], "killed", timestamp())
        return True
    pid = row.get("pid")
    if pid is None:
        store.update_process_state(row["id"], "killed", timestamp())
        return False
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        store.update_process_state(row["id"], "killed", timestamp())
        return False
    store.update_process_state(row["id"], "killed", timestamp())
    return True


def reconcile_finished_runs(store: StateStore) -> list[RunReconciliation]:
    reconciled: list[RunReconciliation] = []
    for row in store.list_unfinalized_processes():
        artifact_paths = _expand_globs(row.get("artifact_globs") or "")
        run_state = row["state"]

        if run_state == "running":
            if row["pid"] is not None and pid_is_alive(row["pid"]):
                continue
            run_state = "exited"
            store.update_process_state(
                row["id"],
                "exited",
                timestamp(),
                artifact_paths=artifact_paths,
            )

        if run_state in {"command_missing", "failed"}:
            final_state = run_state
            transition_target = row.get("transition_on_failure")
            comment_body = row.get("comment_on_failure")
        else:
            final_state = "succeeded" if artifact_paths else "completed_no_proof"
            transition_target = (
                row.get("transition_on_success")
                if final_state == "succeeded"
                else row.get("transition_on_failure")
            )
            comment_body = (
                row.get("comment_on_success")
                if final_state == "succeeded"
                else row.get("comment_on_failure")
            )
            store.update_process_state(
                row["id"],
                final_state,
                timestamp(),
                artifact_paths=artifact_paths,
            )

        reconciled.append(
            RunReconciliation(
                run_id=row["id"],
                issue_key=row["issue_key"],
                state=final_state,
                proof_files=artifact_paths,
                log_path=row["log_path"],
                prompt=row.get("prompt") or "",
                action=row.get("action_name") or "",
                transition_target=transition_target,
                comment_body=comment_body,
            )
        )
    return reconciled


def build_command(config: AppConfig, prompt: str) -> list[str]:
    settings = load_operator_settings(config.operator_settings_path)
    command_name = settings.claude_command or config.llm.command
    formatted_args = [arg.format(prompt=prompt) for arg in config.llm.args]
    extra_args = _claude_extra_args(command_name)
    return [command_name, *extra_args, *formatted_args]


def build_interactive_command(config: AppConfig) -> list[str]:
    settings = load_operator_settings(config.operator_settings_path)
    command_name = settings.claude_command or config.llm.command
    return [command_name, *_claude_extra_args(command_name)]


def command_preview(config: AppConfig, prompt: str) -> str:
    if _should_use_tmux(config):
        interactive = shlex.join(build_interactive_command(config))
        return f"tmux shell -> {interactive}  ; send {shlex.quote(prompt)}"
    return shlex.join(build_command(config, prompt))


def _resolve_working_dir(config: AppConfig) -> str | None:
    settings = load_operator_settings(config.operator_settings_path)
    return settings.claude_working_dir or config.llm.working_dir or None


def attach_session(row: dict) -> subprocess.CompletedProcess[str] | None:
    session_name = row.get("session_name")
    if not session_name or not _tmux_session_exists(session_name):
        return None
    if os.environ.get("TMUX"):
        return subprocess.run(["tmux", "switch-client", "-t", session_name], check=False)
    return subprocess.run(["tmux", "attach-session", "-t", session_name], check=False)


def open_session_in_terminal(row: dict) -> bool:
    session_name = row.get("session_name")
    if not session_name or not _tmux_session_exists(session_name):
        return False
    if shutil.which("osascript") is None:
        return False

    attach_cmd = textwrap.dedent(
        f"""
        clear
        printf '\\nAttached to Claude ticket session: {session_name}\\n'
        printf 'Detach safely with Ctrl-b then d\\n'
        printf 'Do not use Ctrl-C unless you want to interrupt Claude.\\n\\n'
        tmux attach-session -t {shlex.quote(session_name)}
        """
    ).strip().replace("\n", "; ")
    escaped_cmd = attach_cmd.replace("\\", "\\\\").replace('"', '\\"')
    for script in _terminal_scripts(escaped_cmd):
        result = subprocess.run(
            ["osascript", *sum([["-e", line] for line in script], [])],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if result.returncode == 0:
            return True
    return False


def capture_session_output(row: dict, lines: int = 80) -> str:
    session_name = row.get("session_name")
    if session_name and _tmux_session_exists(session_name):
        alt_screen = _capture_tmux_pane(session_name, lines=lines, alternate=True)
        if alt_screen.strip():
            return _tail_terminal_output(_sanitize_terminal_output(alt_screen), lines)
        normal_screen = _capture_tmux_pane(session_name, lines=lines, alternate=False)
        if normal_screen.strip():
            return _tail_terminal_output(_sanitize_terminal_output(normal_screen), lines)

    log_path = row.get("log_path")
    if not log_path:
        return ""
    path = Path(log_path)
    if not path.exists():
        return ""
    content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return _tail_terminal_output(_sanitize_terminal_output("\n".join(content[-max(50, lines * 4):])), lines)


def _should_use_tmux(config: AppConfig) -> bool:
    settings = load_operator_settings(config.operator_settings_path)
    command_name = settings.claude_command or config.llm.command
    return Path(command_name).name == "claude" and shutil.which("tmux") is not None


def _claude_extra_args(command_name: str) -> list[str]:
    if Path(command_name).name != "claude":
        return []
    return ["--dangerously-skip-permissions", "--chrome"]


def _session_name(issue_key: str, slug: str) -> str:
    compact_slug = (
        slug.replace("+00-00", "z")
        .replace("-", "")
        .replace(":", "")
        .replace(".", "")
    )[:24]
    return f"swarmgrid-{issue_key.lower()}-{compact_slug}"


def _launch_tmux_session(
    session_name: str,
    working_dir: str | None,
    command: list[str] | None,
    prompt: str | None,
    log_path: Path,
) -> None:
    tmux_cmd = [
        "tmux",
        "new-session",
        "-d",
        "-x",
        "185",
        "-y",
        "55",
        "-s",
        session_name,
    ]
    if working_dir:
        tmux_cmd.extend(["-c", working_dir])
    tmux_cmd.append(_default_shell())
    subprocess.run(tmux_cmd, check=True)
    subprocess.run(
        ["tmux", "set-option", "-t", session_name, "window-size", "manual"],
        check=True,
    )
    subprocess.run(
        ["tmux", "resize-window", "-t", session_name, "-x", "185", "-y", "55"],
        check=True,
    )
    subprocess.run(
        ["tmux", "set-option", "-t", session_name, "mouse", "on"],
        check=True,
    )
    subprocess.run(
        ["tmux", "set-option", "-t", session_name, "set-clipboard", "on"],
        check=True,
    )
    subprocess.run(
        ["tmux", "pipe-pane", "-o", "-t", session_name, f"cat >> {shlex.quote(str(log_path))}"],
        check=True,
    )
    if command:
        _tmux_send_literal(session_name, shlex.join(command))
        _tmux_send_enter(session_name)
    if prompt:
        _wait_for_claude_ready(session_name, timeout_seconds=25)
        _tmux_send_literal(session_name, prompt)
        _tmux_send_enter(session_name)


def _tmux_session_exists(session_name: str) -> bool:
    if shutil.which("tmux") is None:
        return False
    result = subprocess.run(
        ["tmux", "has-session", "-t", session_name],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def _terminal_scripts(escaped_cmd: str) -> list[list[str]]:
    scripts: list[list[str]] = []
    if Path("/Applications/iTerm.app").exists():
        scripts.append(
            [
                "tell application \"iTerm\" to activate",
                "tell application \"iTerm\" to create window with default profile",
                (
                    "tell application \"iTerm\" to tell current session of current window "
                    f"to write text \"{escaped_cmd}\""
                ),
            ]
        )
    scripts.append(
        [
            "tell application \"Terminal\" to activate",
            f"tell application \"Terminal\" to do script \"{escaped_cmd}\"",
        ]
    )
    return scripts


def _tmux_pane_pid(session_name: str) -> int | None:
    result = subprocess.run(
        ["tmux", "list-panes", "-t", session_name, "-F", "#{pane_pid}"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    line = result.stdout.strip().splitlines()
    if not line:
        return None
    try:
        return int(line[0].strip())
    except ValueError:
        return None


def _default_shell() -> str:
    return shutil.which("zsh") or shutil.which("bash") or "/bin/sh"


def _tmux_send_literal(session_name: str, text: str) -> None:
    subprocess.run(["tmux", "send-keys", "-l", "-t", session_name, text], check=True)


def _tmux_send_enter(session_name: str) -> None:
    subprocess.run(["tmux", "send-keys", "-t", session_name, "C-m"], check=True)


def _wait_for_claude_ready(session_name: str, timeout_seconds: int) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        pane = _capture_tmux_pane(session_name, lines=80)
        if _looks_like_claude_ready(pane):
            return True
        if "Resume this session with:" in pane:
            return False
        time.sleep(0.5)
    return False


def _capture_tmux_pane(session_name: str, lines: int, alternate: bool = False) -> str:
    command = ["tmux", "capture-pane", "-p", "-J"]
    if alternate:
        command.append("-a")
    command.extend(["-S", "-", "-E", "-", "-t", session_name])
    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return ""
    return result.stdout


def _looks_like_claude_ready(text: str) -> bool:
    candidates = [
        "bypass permissions on",
        "esc to interrupt",
        "current:",
        "0 tokens",
        "What would you like to do",
        "got something you'd like to work on",
    ]
    return any(token in text for token in candidates)


def _write_launch_metadata(
    run_dir: Path,
    issue_key: str,
    prompt: str,
    command_line: str,
    working_dir: str | None,
    session_name: str | None,
    launch_mode: str,
) -> None:
    lines = [
        f"issue: {issue_key}",
        f"mode: {launch_mode}",
        f"cwd: {working_dir or '-'}",
        f"session: {session_name or '-'}",
        f"prompt: {prompt}",
        f"command: {command_line}",
    ]
    (run_dir / "launch.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _resolve_artifact_globs(decision: RouteDecision, run_dir: Path) -> list[str]:
    patterns = decision.artifact_globs or []
    if not patterns:
        patterns = [str(run_dir / "**" / "*.gif")]
    return patterns


def _expand_globs(pattern_blob: str) -> list[str]:
    patterns = [line.strip() for line in pattern_blob.splitlines() if line.strip()]
    paths: list[str] = []
    for pattern in patterns:
        paths.extend(sorted(glob.glob(pattern, recursive=True)))
    # preserve order, drop dupes
    deduped: list[str] = []
    seen: set[str] = set()
    for path in paths:
        if path in seen:
            continue
        seen.add(path)
        deduped.append(path)
    return deduped


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_OSC_RE = re.compile(r"\x1b\][^\x07]*(?:\x07|\x1b\\)")


def _sanitize_terminal_output(text: str) -> str:
    cleaned = _OSC_RE.sub("", text)
    cleaned = _ANSI_RE.sub("", cleaned)
    cleaned = cleaned.replace("\r", "\n")
    lines = []
    for raw_line in cleaned.splitlines():
        line = "".join(ch for ch in raw_line if ch == "\t" or 32 <= ord(ch) <= 126)
        lines.append(line.rstrip())
    collapsed: list[str] = []
    blank_run = 0
    for line in lines:
        if _should_drop_terminal_line(line):
            continue
        if line.strip():
            blank_run = 0
            collapsed.append(line)
            continue
        blank_run += 1
        if blank_run <= 1:
            collapsed.append("")
    return "\n".join(collapsed).strip()


def _tail_terminal_output(text: str, lines: int) -> str:
    cleaned_lines = [line.rstrip() for line in text.splitlines()]
    if not cleaned_lines:
        return ""
    tail = cleaned_lines[-max(1, lines):]
    return "\n".join(tail).strip()


def _should_drop_terminal_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if stripped in {'"', "'", ">", ">>", ">>>", "❯", "[]", "[<u"}:
        return True
    if set(stripped) <= {"-", "=", "_", " "}:
        return True
    terminal_noise = [
        "bypass permissions on",
        "shift+tab to cycle",
        "esc to interrupt",
        "current:",
        "latest:",
        "tokens",
        "Press Ctrl-C again to exit",
        "Tip: Run /terminal-setup",
    ]
    return any(token in stripped for token in terminal_noise)
