"""Phonebook worker — cloud-facing CGI handler with PARTIAL info only.

This is the force-command for the "phonebook" upterm agent, which faces
the cloud (swarmgrid.org). It uses --authorized-keys (cloud's SSH key
only, no GitHub users).

The phonebook gives out PARTIAL information only. It never reveals real
session connect strings, full session IDs, prompts, or output.

Like worker.py: reads a single JSON line from stdin, dispatches the
command, writes a JSON response to stdout, and exits.

Run as: python -m swarmgrid.agent.phonebook_worker
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, UTC
from pathlib import Path


# -- Helpers --

def _extract_ticket_key(session_id: str) -> str | None:
    """Extract a Jira ticket key from a session name.

    Session names look like: swarmgrid-lmsv3-857-20260322t...
    We want to return: LMSV3-857
    """
    # Strip the "swarmgrid-" prefix
    name = session_id
    if name.startswith("swarmgrid-"):
        name = name[len("swarmgrid-"):]

    # Match project-number at the start (e.g. "lmsv3-857-...")
    match = re.match(r"^([a-zA-Z][a-zA-Z0-9]*)-(\d+)", name)
    if match:
        project = match.group(1).upper()
        number = match.group(2)
        return f"{project}-{number}"
    return None


def _extract_board(ticket_key: str) -> str:
    """Extract the board/project prefix from a ticket key.

    e.g. "LMSV3-857" -> "LMSV3"
    """
    parts = ticket_key.split("-", 1)
    return parts[0] if parts else ticket_key


def _truncate_session_ref(session_id: str) -> str:
    """Create an opaque, truncated reference from a session ID.

    e.g. "swarmgrid-lmsv3-857-20260322t143200730z" -> "swarmgri...730z"
    Never reveals the full session ID.
    """
    if len(session_id) <= 16:
        return session_id  # Already short enough
    return session_id[:8] + "..." + session_id[-4:]


def _find_heartbeat_db() -> Path | None:
    """Locate the heartbeat.sqlite database.

    Checks common locations where the state dir places heartbeat.sqlite.
    """
    candidates = [
        # Default state dir from typical board-routes.yaml configs
        Path("var/heartbeat/heartbeat.sqlite"),
        Path("var/state/heartbeat.sqlite"),
        # Home-based state
        Path.home() / ".swarmgrid" / "state" / "heartbeat.sqlite",
        Path.home() / ".swarmgrid" / "heartbeat.sqlite",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _load_heartbeat_state() -> dict | None:
    """Load summary info from the heartbeat sqlite database."""
    db_path = _find_heartbeat_db()
    if not db_path:
        return None

    import sqlite3
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row

        # Get the last heartbeat tick
        row = conn.execute(
            "SELECT finished_at, issue_count, launched_count "
            "FROM heartbeat_ticks ORDER BY id DESC LIMIT 1"
        ).fetchone()
        last_tick = dict(row) if row else None

        # Count distinct boards (project keys from issue_state)
        board_rows = conn.execute(
            "SELECT DISTINCT SUBSTR(issue_key, 1, INSTR(issue_key, '-') - 1) AS board "
            "FROM issue_state"
        ).fetchall()
        boards = [r["board"] for r in board_rows if r["board"]]

        # Count running processes
        running_count = conn.execute(
            "SELECT COUNT(*) FROM process_runs WHERE state = 'running'"
        ).fetchone()[0]

        conn.close()
        return {
            "last_tick": last_tick,
            "boards": boards,
            "running_db_count": running_count,
        }
    except Exception:
        return None


# -- Command handlers --

def handle_ping(_payload: dict) -> dict:
    import platform
    return {
        "ok": True,
        "pong": True,
        "hostname": platform.node(),
        "os": platform.system(),
    }


def handle_status(_payload: dict) -> dict:
    """Return agent summary: boards active, session count, last heartbeat, uptime."""
    import platform

    # Session count from tmux
    from .session_manager import list_sessions
    sessions_result = list_sessions()
    session_count = len(sessions_result.get("sessions", []))

    # Heartbeat state from sqlite
    hb_state = _load_heartbeat_state()

    last_heartbeat = None
    boards_active = []
    db_running_count = 0
    if hb_state:
        if hb_state["last_tick"]:
            last_heartbeat = hb_state["last_tick"].get("finished_at")
        boards_active = hb_state.get("boards", [])
        db_running_count = hb_state.get("running_db_count", 0)

    # Agent uptime: check the swarmgrid-agent tmux session creation time
    agent_uptime_seconds = _get_agent_uptime()

    return {
        "ok": True,
        "hostname": platform.node(),
        "boards_active": boards_active,
        "board_count": len(boards_active),
        "session_count": session_count,
        "db_running_count": db_running_count,
        "last_heartbeat": last_heartbeat,
        "agent_uptime_seconds": agent_uptime_seconds,
    }


def _get_agent_uptime() -> int | None:
    """Get agent uptime in seconds by checking the swarmgrid-agent tmux session."""
    import shutil
    import subprocess

    if not shutil.which("tmux"):
        return None

    result = subprocess.run(
        ["tmux", "list-sessions", "-F", "#{session_name} #{session_created}"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None

    for line in result.stdout.strip().splitlines():
        parts = line.strip().split(" ", 1)
        if len(parts) == 2 and parts[0] == "swarmgrid-agent":
            try:
                created_epoch = int(parts[1])
                now_epoch = int(datetime.now(UTC).timestamp())
                return max(0, now_epoch - created_epoch)
            except (ValueError, TypeError):
                return None
    return None


def handle_sessions_summary(_payload: dict) -> dict:
    """Return PARTIAL session info — no connect strings, no full IDs, no output."""
    from .session_manager import list_sessions

    sessions_result = list_sessions()
    raw_sessions = sessions_result.get("sessions", [])

    summaries = []
    for s in raw_sessions:
        session_id = s.get("session_id", "")
        ticket_key = _extract_ticket_key(session_id)
        if not ticket_key:
            # Skip non-work sessions (e.g. swarmgrid-agent daemon)
            continue
        board = _extract_board(ticket_key)
        state = s.get("state", "unknown")

        summaries.append({
            "ticket_key": ticket_key,
            "state": state,
            "session_ref": _truncate_session_ref(session_id),
            "board": board,
        })

    return {
        "ok": True,
        "session_count": len(summaries),
        "sessions": summaries,
    }


def handle_open_local(payload: dict) -> dict:
    """Open an iTerm2 window for a session, looked up by ticket_key."""
    ticket_key = payload.get("ticket_key", "")
    if not ticket_key:
        return {"ok": False, "error": "ticket_key is required"}

    # Find the session by ticket key
    from .session_manager import list_sessions
    sessions = list_sessions().get("sessions", [])
    match = next(
        (s for s in sessions if ticket_key.lower() in s.get("session_id", "").lower()),
        None,
    )
    if not match:
        return {"ok": False, "error": f"no session found for ticket {ticket_key}"}

    session_id = match["session_id"]

    # Open in local terminal (reuse the attach logic from worker.py)
    from ..runner import open_session_in_terminal
    opened = open_session_in_terminal({"session_name": session_id})
    if opened:
        return {
            "ok": True,
            "opened": session_id,
            "ticket_key": ticket_key,
            "method": "iterm2_or_terminal",
        }
    else:
        return {
            "ok": True,
            "opened": False,
            "ticket_key": ticket_key,
            "session_ref": _truncate_session_ref(session_id),
            "hint": "Could not auto-open terminal window.",
        }


def handle_refresh_config(_payload: dict) -> dict:
    """Write a trigger file that the heartbeat loop checks to re-fetch config."""
    trigger_path = Path.home() / ".swarmgrid" / "refresh_trigger"
    trigger_path.parent.mkdir(parents=True, exist_ok=True)
    trigger_path.write_text(
        datetime.now(UTC).isoformat(),
        encoding="utf-8",
    )
    return {
        "ok": True,
        "trigger_written": str(trigger_path),
        "triggered_at": datetime.now(UTC).isoformat(),
    }


COMMANDS = {
    "ping": handle_ping,
    "status": handle_status,
    "sessions_summary": handle_sessions_summary,
    "open_local": handle_open_local,
    "refresh_config": handle_refresh_config,
}


def main() -> None:
    try:
        line = sys.stdin.readline().strip()
        if not line:
            json.dump({"ok": False, "error": "empty input"}, sys.stdout)
            return
        payload = json.loads(line)
    except json.JSONDecodeError as exc:
        json.dump({"ok": False, "error": f"invalid JSON: {exc}"}, sys.stdout)
        return

    cmd = payload.get("cmd", "")
    handler = COMMANDS.get(cmd)
    if not handler:
        json.dump({"ok": False, "error": f"unknown command: {cmd}"}, sys.stdout)
        return

    try:
        result = handler(payload)
        json.dump(result, sys.stdout)
    except Exception as exc:
        json.dump({"ok": False, "error": str(exc)}, sys.stdout)


if __name__ == "__main__":
    main()
