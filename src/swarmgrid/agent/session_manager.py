"""Tmux session manager for edge agent work.

Reuses patterns from runner.py — launching Claude in tmux sessions,
capturing output, checking session state, and terminating sessions.
"""
from __future__ import annotations

import logging
import os
import re
import shlex
import shutil
import subprocess
import time
from datetime import datetime, UTC
from pathlib import Path

logger = logging.getLogger(__name__)

SESSION_PREFIX = "swarmgrid-"
DEFAULT_WIDTH = 185
DEFAULT_HEIGHT = 55


def timestamp() -> str:
    return datetime.now(UTC).isoformat()


def session_name(ticket_key: str) -> str:
    """Generate a unique tmux session name for a ticket."""
    slug = timestamp().replace(":", "").replace("-", "").replace("+", "").replace(".", "")[:24]
    return f"{SESSION_PREFIX}{ticket_key.lower()}-{slug}"


def launch_session(
    ticket_key: str,
    prompt: str,
    *,
    working_dir: str | None = None,
    claude_command: str = "claude",
    session_config: dict | None = None,
) -> dict:
    """Spawn Claude in a new tmux session for the given ticket.

    Returns a dict with session_id, state, and metadata.
    """
    if shutil.which("tmux") is None:
        return {"ok": False, "error": "tmux is not installed"}

    name = session_name(ticket_key)
    config = session_config or {}
    width = config.get("width", DEFAULT_WIDTH)
    height = config.get("height", DEFAULT_HEIGHT)
    cwd = working_dir or config.get("working_dir")

    # Create tmux session
    tmux_cmd = [
        "tmux", "new-session", "-d",
        "-x", str(width), "-y", str(height),
        "-s", name,
    ]
    if cwd:
        tmux_cmd.extend(["-c", cwd])

    shell = shutil.which("zsh") or shutil.which("bash") or "/bin/sh"
    tmux_cmd.append(shell)

    try:
        subprocess.run(tmux_cmd, check=True, capture_output=True)
    except subprocess.CalledProcessError as exc:
        return {"ok": False, "error": f"tmux new-session failed: {exc.stderr.decode()[:200]}"}

    # Configure the session
    for opt_cmd in [
        ["tmux", "set-option", "-t", name, "window-size", "manual"],
        ["tmux", "resize-window", "-t", name, "-x", str(width), "-y", str(height)],
        ["tmux", "set-option", "-t", name, "mouse", "on"],
    ]:
        subprocess.run(opt_cmd, check=False, capture_output=True)

    # Build and send the claude command
    claude_path = shutil.which(claude_command) or claude_command
    extra_args = []
    if Path(claude_command).name == "claude":
        extra_args = ["--dangerously-skip-permissions", "--chrome"]

    cmd_str = shlex.join([claude_path, *extra_args])
    _tmux_send(name, cmd_str)
    _tmux_send_enter(name)

    # Wait for Claude to be ready, then send the prompt
    if _wait_for_claude_ready(name, timeout_seconds=25):
        _tmux_send(name, prompt)
        _tmux_send_enter(name)

    pid = _tmux_pane_pid(name)

    return {
        "ok": True,
        "session_id": name,
        "ticket_key": ticket_key,
        "state": "running",
        "pid": pid,
        "created_at": timestamp(),
    }


def session_status(session_id: str) -> dict:
    """Check the status of a tmux session."""
    if not _tmux_session_exists(session_id):
        return {"ok": True, "session_id": session_id, "state": "exited"}

    pid = _tmux_pane_pid(session_id)
    return {
        "ok": True,
        "session_id": session_id,
        "state": "running",
        "pid": pid,
    }


def capture_output(session_id: str, lines: int = 80) -> dict:
    """Capture terminal output from a tmux session."""
    if not _tmux_session_exists(session_id):
        return {"ok": False, "error": f"session {session_id} not found"}

    # Try alternate screen first (Claude uses full-screen TUI)
    output = _capture_pane(session_id, lines=lines, alternate=True)
    if not output.strip():
        output = _capture_pane(session_id, lines=lines, alternate=False)

    cleaned = _sanitize(output)
    tail = cleaned.splitlines()[-lines:] if cleaned else []

    return {
        "ok": True,
        "session_id": session_id,
        "lines": len(tail),
        "output": "\n".join(tail),
    }


def kill_session(session_id: str) -> dict:
    """Terminate a tmux session."""
    if not _tmux_session_exists(session_id):
        return {"ok": True, "session_id": session_id, "state": "not_found"}

    subprocess.run(
        ["tmux", "kill-session", "-t", session_id],
        check=False,
        capture_output=True,
    )
    return {"ok": True, "session_id": session_id, "state": "killed"}


def list_sessions() -> dict:
    """List all swarmgrid tmux sessions."""
    if shutil.which("tmux") is None:
        return {"ok": True, "sessions": []}

    result = subprocess.run(
        ["tmux", "list-sessions", "-F", "#{session_name}"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return {"ok": True, "sessions": []}

    sessions = []
    for line in result.stdout.strip().splitlines():
        name = line.strip()
        if name.startswith(SESSION_PREFIX):
            pid = _tmux_pane_pid(name)
            sessions.append({
                "session_id": name,
                "state": "running",
                "pid": pid,
            })
    return {"ok": True, "sessions": sessions}


# -- Internal helpers (reused from runner.py patterns) --

def _tmux_session_exists(name: str) -> bool:
    if shutil.which("tmux") is None:
        return False
    result = subprocess.run(
        ["tmux", "has-session", "-t", name],
        check=False,
        capture_output=True,
    )
    return result.returncode == 0


def _tmux_pane_pid(name: str) -> int | None:
    result = subprocess.run(
        ["tmux", "list-panes", "-t", name, "-F", "#{pane_pid}"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    lines = result.stdout.strip().splitlines()
    if not lines:
        return None
    try:
        return int(lines[0].strip())
    except ValueError:
        return None


def _tmux_send(name: str, text: str) -> None:
    subprocess.run(["tmux", "send-keys", "-l", "-t", name, text], check=True)


def _tmux_send_enter(name: str) -> None:
    subprocess.run(["tmux", "send-keys", "-t", name, "C-m"], check=True)


def _capture_pane(name: str, lines: int, alternate: bool = False) -> str:
    cmd = ["tmux", "capture-pane", "-p", "-J"]
    if alternate:
        cmd.append("-a")
    cmd.extend(["-S", "-", "-E", "-", "-t", name])
    result = subprocess.run(cmd, check=False, capture_output=True, text=True)
    return result.stdout if result.returncode == 0 else ""


def _wait_for_claude_ready(name: str, timeout_seconds: int) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        pane = _capture_pane(name, lines=80)
        if _looks_ready(pane):
            return True
        if "Resume this session with:" in pane:
            return False
        time.sleep(0.5)
    return False


def _looks_ready(text: str) -> bool:
    markers = [
        "bypass permissions on",
        "esc to interrupt",
        "current:",
        "0 tokens",
        "What would you like to do",
        "got something you'd like to work on",
    ]
    return any(m in text for m in markers)


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_OSC_RE = re.compile(r"\x1b\][^\x07]*(?:\x07|\x1b\\)")


def _sanitize(text: str) -> str:
    cleaned = _OSC_RE.sub("", text)
    cleaned = _ANSI_RE.sub("", cleaned)
    cleaned = cleaned.replace("\r", "\n")
    lines = []
    for raw in cleaned.splitlines():
        line = "".join(ch for ch in raw if ch == "\t" or 32 <= ord(ch) <= 126)
        lines.append(line.rstrip())
    # Collapse blank runs
    collapsed: list[str] = []
    blank_run = 0
    for line in lines:
        if line.strip():
            blank_run = 0
            collapsed.append(line)
        else:
            blank_run += 1
            if blank_run <= 1:
                collapsed.append("")
    return "\n".join(collapsed).strip()
