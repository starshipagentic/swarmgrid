"""Tmux session manager for edge agent work.

Reuses patterns from runner.py — launching Claude in tmux sessions,
capturing output, checking session state, and terminating sessions.
Includes per-ticket upterm sharing with board-scoped --github-user.
"""
from __future__ import annotations

import json
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
UPTERM_PREFIX = "upterm-"
DEFAULT_WIDTH = 185
DEFAULT_HEIGHT = 55
UPTERM_SERVER = "ssh://uptermd.upterm.dev:22"
TEAM_CONFIG_PATH = Path.home() / ".swarmgrid" / "team_config.json"
SESSION_SHARES_DIR = Path.home() / ".swarmgrid" / "session_shares"


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
    share_upterm: bool = True,
    github_users: list[str] | None = None,
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

    result = {
        "ok": True,
        "session_id": name,
        "ticket_key": ticket_key,
        "state": "running",
        "pid": pid,
        "created_at": timestamp(),
    }

    # Start upterm share for pair programming
    if share_upterm and shutil.which("upterm") is not None:
        users = github_users or _github_users_for_ticket(ticket_key)
        if users:
            try:
                share_info = _start_upterm_share(ticket_key, name, users)
                result["upterm_shared"] = True
                result["ssh_connect"] = share_info["ssh_connect"]
            except RuntimeError as exc:
                logger.warning("Upterm share failed for %s: %s", ticket_key, exc)
                result["upterm_shared"] = False
                result["upterm_error"] = str(exc)
        else:
            logger.info("No github_users configured for %s — skipping upterm share", ticket_key)
            result["upterm_shared"] = False

    return result


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
    """Terminate a tmux session and its upterm share."""
    if not _tmux_session_exists(session_id):
        return {"ok": True, "session_id": session_id, "state": "not_found"}

    # Extract ticket_key from session_id to clean up upterm
    ticket_key = _extract_ticket_key(session_id)
    if ticket_key:
        _cleanup_upterm_share(ticket_key)

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


def get_session_share(ticket_key: str) -> dict | None:
    """Read the persisted upterm share info for a ticket.

    Returns a dict with ssh_connect, github_users, session_id, etc.
    or None if no share file exists.
    """
    share_file = SESSION_SHARES_DIR / f"{ticket_key.upper()}.json"
    try:
        data = json.loads(share_file.read_text(encoding="utf-8"))
        return data
    except (FileNotFoundError, json.JSONDecodeError):
        return None


# -- Upterm sharing helpers --

def _board_prefix(ticket_key: str) -> str:
    """Extract the board prefix from a ticket key.

    "LMSV3-857" -> "LMSV3"
    "ACME-42"   -> "ACME"
    """
    parts = ticket_key.split("-")
    if len(parts) >= 2 and parts[-1].isdigit():
        return "-".join(parts[:-1]).upper()
    return ticket_key.upper()


def _load_team_config() -> dict:
    """Load the team config written by the daemon during heartbeat."""
    try:
        return json.loads(TEAM_CONFIG_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _github_users_for_ticket(ticket_key: str) -> list[str]:
    """Look up github_users for the board that owns a ticket."""
    config = _load_team_config()
    boards = config.get("boards", {})
    prefix = _board_prefix(ticket_key)
    board = boards.get(prefix, {})
    return board.get("github_users", [])


def _start_upterm_share(
    ticket_key: str,
    tmux_session: str,
    github_users: list[str],
) -> dict:
    """Start an upterm share for a tmux session.

    Follows the same pattern as upterm.py _start_share():
    - Runs upterm host inside a wrapper tmux session
    - Parses the SSH connect string from the log
    - Saves share info to a JSON file

    Returns dict with ssh_connect, session_id, github_users.
    Raises RuntimeError on failure.
    """
    wrapper_name = f"{UPTERM_PREFIX}{ticket_key.lower()}"

    # Kill existing wrapper session if any
    subprocess.run(
        ["tmux", "kill-session", "-t", wrapper_name],
        check=False,
        capture_output=True,
    )

    # Build upterm command
    cmd_parts = [
        "upterm", "host",
        "--accept",
        "--skip-host-key-check",
        "--server", UPTERM_SERVER,
    ]
    for user in github_users:
        cmd_parts.extend(["--github-user", user])
    cmd_parts.extend(["--", "tmux", "attach-session", "-t", tmux_session])

    log_file = f"/tmp/upterm-{ticket_key.lower()}.log"
    shell_cmd = shlex.join(cmd_parts) + f" 2>&1 | tee {log_file}; sleep 999"

    # Launch upterm inside a tmux session (needs a TTY)
    try:
        subprocess.run(
            [
                "tmux", "new-session", "-d",
                "-s", wrapper_name,
                "-x", "180", "-y", "50",
                shell_cmd,
            ],
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"Failed to create upterm wrapper session: {exc.stderr.decode()[:200]}"
        )

    # Wait for upterm to establish the tunnel and write its log
    session_id = None
    ssh_connect = None
    for _ in range(20):  # up to 10 seconds
        time.sleep(0.5)
        try:
            with open(log_file) as f:
                content = f.read()
            # Parse session ID from output
            match = re.search(r"Session:\s+(\S+)", content)
            if match:
                session_id = match.group(1)
            # Parse SSH connect string (capture full line including -p PORT)
            ssh_match = re.search(r"ssh\s+(\S+@\S+(?:\s+-p\s+\d+)?)", content)
            if ssh_match:
                ssh_connect = f"ssh {ssh_match.group(1)}"
            if session_id and ssh_connect:
                break
        except FileNotFoundError:
            continue

    if not session_id or not ssh_connect:
        # Clean up on failure
        subprocess.run(
            ["tmux", "kill-session", "-t", wrapper_name],
            check=False,
            capture_output=True,
        )
        log_content = ""
        try:
            with open(log_file) as f:
                log_content = f.read()
        except FileNotFoundError:
            pass
        raise RuntimeError(f"Upterm failed to start. Log: {log_content[:500]}")

    share_info = {
        "ssh_connect": ssh_connect,
        "session_id": session_id,
        "github_users": github_users,
        "ticket_key": ticket_key.upper(),
        "tmux_session": tmux_session,
        "wrapper_session": wrapper_name,
        "created_at": timestamp(),
    }

    _save_session_share(ticket_key, share_info)
    logger.info("Started upterm share for %s: %s", ticket_key, ssh_connect)
    return share_info


def _save_session_share(ticket_key: str, share_info: dict) -> None:
    """Persist share info to ~/.swarmgrid/session_shares/{TICKET_KEY}.json."""
    SESSION_SHARES_DIR.mkdir(parents=True, exist_ok=True)
    share_file = SESSION_SHARES_DIR / f"{ticket_key.upper()}.json"
    share_file.write_text(
        json.dumps(share_info, indent=2),
        encoding="utf-8",
    )


def _cleanup_upterm_share(ticket_key: str) -> None:
    """Kill the upterm wrapper session and remove the share file."""
    wrapper_name = f"{UPTERM_PREFIX}{ticket_key.lower()}"

    # Kill upterm wrapper tmux session
    subprocess.run(
        ["tmux", "kill-session", "-t", wrapper_name],
        check=False,
        capture_output=True,
    )

    # Remove share file
    share_file = SESSION_SHARES_DIR / f"{ticket_key.upper()}.json"
    try:
        share_file.unlink(missing_ok=True)
    except OSError:
        pass

    # Remove upterm log file
    log_file = Path(f"/tmp/upterm-{ticket_key.lower()}.log")
    try:
        log_file.unlink(missing_ok=True)
    except OSError:
        pass

    logger.info("Cleaned up upterm share for %s", ticket_key)


def _extract_ticket_key(session_id: str) -> str | None:
    """Extract the ticket key from a swarmgrid session name.

    Session names follow: "swarmgrid-{ticket_key_lower}-{timestamp_slug}"
    The timestamp slug is a run of digits (and possibly 't'/'z' chars).

    Examples:
        "swarmgrid-lmsv3-857-20260322t140000000z" -> "lmsv3-857"
        "swarmgrid-acme-42-20260322t140000000z"    -> "acme-42"
    """
    if not session_id.startswith(SESSION_PREFIX):
        return None
    remainder = session_id[len(SESSION_PREFIX):]
    segments = remainder.split("-")

    # Walk from the right. The timestamp slug is a long run of digits/letters
    # at the end (e.g. "20260322t140000000z"). Everything before that is the
    # ticket key.
    ticket_parts: list[str] = []
    found_ticket = False
    for segment in reversed(segments):
        if not found_ticket and re.fullmatch(r"[0-9tz]+", segment, re.IGNORECASE) and len(segment) > 6:
            continue  # skip timestamp slug
        else:
            found_ticket = True
            ticket_parts.insert(0, segment)

    if not ticket_parts:
        return None
    return "-".join(ticket_parts)


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
