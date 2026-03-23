"""Front-desk worker — team-facing CGI handler invoked per SSH connection.

This is the force-command for the "front desk" upterm session that faces
teammates.  Upterm's --github-user has already verified the caller's SSH
key matches a GitHub user on the allow-list.  The worker then checks
board-level access using the team config before returning session info.

Protocol is identical to worker.py: read a single JSON line from stdin,
dispatch the command, write a JSON response to stdout, and exit.

Run as:  python -m swarmgrid.agent.frontdesk_worker
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path


TEAM_CONFIG_PATH = Path.home() / ".swarmgrid" / "team_config.json"

# Session names are "swarmgrid-{ticket_key_lower}-{timestamp_slug}"
# e.g. "swarmgrid-lmsv3-857-20260322t140000000z"
SESSION_PREFIX = "swarmgrid-"


def _load_team_config() -> dict:
    """Load the team config written by the daemon during heartbeat."""
    try:
        return json.loads(TEAM_CONFIG_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _board_prefix(ticket_key: str) -> str:
    """Extract the board prefix from a ticket key.

    "LMSV3-857" -> "LMSV3"
    "ACME-42"   -> "ACME"
    """
    parts = ticket_key.rsplit("-", 1)
    if len(parts) == 2 and parts[1].isdigit():
        return parts[0].upper()
    # Might be a board key with multiple hyphens like "MY-PROJ-123"
    # Walk from the right to find the numeric issue number
    segments = ticket_key.split("-")
    if segments and segments[-1].isdigit():
        return "-".join(segments[:-1]).upper()
    return ticket_key.upper()


def _check_board_access(github_user: str, ticket_key: str) -> bool:
    """Return True if github_user is authorized for the board that owns ticket_key."""
    config = _load_team_config()
    boards = config.get("boards", {})
    prefix = _board_prefix(ticket_key)

    board = boards.get(prefix)
    if not board:
        return False
    return github_user in board.get("github_users", [])


def _extract_ticket_key(session_id: str) -> str | None:
    """Extract the ticket key from a swarmgrid session name.

    Session names follow: "swarmgrid-{ticket_key_lower}-{timestamp_slug}"
    The timestamp slug is a run of digits (and possibly 't'/'z' chars).
    The ticket key itself contains letters, digits, and hyphens.

    Examples:
        "swarmgrid-lmsv3-857-20260322t140000000z" -> "lmsv3-857"
        "swarmgrid-acme-42-20260322t140000000z"    -> "acme-42"
    """
    if not session_id.startswith(SESSION_PREFIX):
        return None

    remainder = session_id[len(SESSION_PREFIX):]
    # The timestamp slug is the last hyphen-separated segment that is
    # purely digits/t/z and at least 10 chars long (ISO-ish compact).
    parts = remainder.split("-")
    # Walk from the right to find where the timestamp starts
    ticket_parts = []
    found_ticket = False
    for i in range(len(parts) - 1, -1, -1):
        segment = parts[i]
        # Timestamp segments are long digit strings (possibly with t/z)
        if not found_ticket and len(segment) >= 10 and all(c in "0123456789tzTZ" for c in segment):
            continue  # skip timestamp slug
        else:
            found_ticket = True
            ticket_parts.insert(0, segment)

    if not ticket_parts:
        return None
    return "-".join(ticket_parts)


def _parse_upterm_connect_string(ticket_key: str) -> str | None:
    """Parse the SSH connect string from the per-ticket upterm log."""
    log_file = Path(f"/tmp/upterm-{ticket_key.lower()}.log")
    try:
        content = log_file.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return None
    match = re.search(r"ssh\s+(\S+@\S+(?:\s+-p\s+\d+)?)", content)
    if match:
        return f"ssh {match.group(1)}"
    return None


# ---------- Command handlers ----------


def handle_ping(_payload: dict) -> dict:
    import platform
    return {
        "ok": True,
        "pong": True,
        "hostname": platform.node(),
    }


def handle_list_sessions(payload: dict) -> dict:
    """List all sessions the caller has access to."""
    from .session_manager import list_sessions

    github_user = payload.get("github_user", "")
    if not github_user:
        return {"ok": False, "error": "github_user required"}

    result = list_sessions()
    all_sessions = result.get("sessions", [])

    accessible = []
    for sess in all_sessions:
        session_id = sess.get("session_id", "")
        ticket_key = _extract_ticket_key(session_id)
        if not ticket_key:
            continue
        if _check_board_access(github_user, ticket_key):
            accessible.append({
                "session_id": session_id,
                "state": sess.get("state", "unknown"),
                "ticket_key": ticket_key,
            })

    return {"ok": True, "sessions": accessible}


def handle_get_session_connect(payload: dict) -> dict:
    """Return the real upterm SSH connect string for a ticket's session."""
    from .session_manager import list_sessions

    github_user = payload.get("github_user", "")
    ticket_key = payload.get("ticket_key", "")
    if not github_user:
        return {"ok": False, "error": "github_user required"}
    if not ticket_key:
        return {"ok": False, "error": "ticket_key required"}

    if not _check_board_access(github_user, ticket_key):
        return {"ok": False, "error": f"access denied: {github_user} is not authorized for {_board_prefix(ticket_key)}"}

    # Find the matching session
    result = list_sessions()
    all_sessions = result.get("sessions", [])
    match = None
    for sess in all_sessions:
        sid = sess.get("session_id", "")
        tk = _extract_ticket_key(sid)
        if tk and tk.lower() == ticket_key.lower():
            match = sess
            break

    if not match:
        return {"ok": False, "error": f"no active session found for {ticket_key}"}

    # Parse the upterm connect string from the log
    ssh_connect = _parse_upterm_connect_string(ticket_key)
    if not ssh_connect:
        return {"ok": False, "error": "session not shared via upterm"}

    return {
        "ok": True,
        "ssh_connect": ssh_connect,
        "session_id": match["session_id"],
    }


def handle_attach(payload: dict) -> dict:
    """Open a tmux session in iTerm2/Terminal.app with board access check."""
    from .session_manager import list_sessions
    from ..runner import open_session_in_terminal

    github_user = payload.get("github_user", "")
    ticket_key = payload.get("ticket_key", "")
    if not github_user:
        return {"ok": False, "error": "github_user required"}
    if not ticket_key:
        return {"ok": False, "error": "ticket_key required"}

    if not _check_board_access(github_user, ticket_key):
        return {"ok": False, "error": f"access denied: {github_user} is not authorized for {_board_prefix(ticket_key)}"}

    # Find the session by ticket key
    result = list_sessions()
    all_sessions = result.get("sessions", [])
    match = None
    for sess in all_sessions:
        sid = sess.get("session_id", "")
        tk = _extract_ticket_key(sid)
        if tk and tk.lower() == ticket_key.lower():
            match = sess
            break

    if not match:
        return {"ok": False, "error": f"no active session found for {ticket_key}"}

    session_id = match["session_id"]
    opened = open_session_in_terminal({"session_name": session_id})
    if opened:
        return {"ok": True, "opened": session_id, "method": "iterm2_or_terminal"}
    else:
        return {
            "ok": True,
            "opened": False,
            "session_id": session_id,
            "attach_command": f"tmux attach -t {session_id}",
            "hint": "Could not auto-open terminal. Run the attach_command manually.",
        }


def handle_status(payload: dict) -> dict:
    """Full status for boards the caller has access to."""
    from .session_manager import list_sessions

    github_user = payload.get("github_user", "")
    if not github_user:
        return {"ok": False, "error": "github_user required"}

    config = _load_team_config()
    boards_config = config.get("boards", {})

    # Determine which boards this user can see
    accessible_boards = set()
    for board_name, board_info in boards_config.items():
        if github_user in board_info.get("github_users", []):
            accessible_boards.add(board_name)

    if not accessible_boards:
        return {"ok": True, "boards": {}, "sessions": []}

    # Get all sessions and filter to accessible boards
    result = list_sessions()
    all_sessions = result.get("sessions", [])

    sessions = []
    for sess in all_sessions:
        session_id = sess.get("session_id", "")
        ticket_key = _extract_ticket_key(session_id)
        if not ticket_key:
            continue
        board = _board_prefix(ticket_key)
        if board in accessible_boards:
            ssh_connect = _parse_upterm_connect_string(ticket_key)
            sessions.append({
                "session_id": session_id,
                "state": sess.get("state", "unknown"),
                "ticket_key": ticket_key,
                "board": board,
                "pid": sess.get("pid"),
                "upterm_shared": ssh_connect is not None,
            })

    # Build board summaries
    boards_summary = {}
    for board_name in accessible_boards:
        board_info = boards_config[board_name]
        board_sessions = [s for s in sessions if s["board"] == board_name]
        boards_summary[board_name] = {
            "board_id": board_info.get("board_id"),
            "team": board_info.get("github_users", []),
            "active_sessions": len(board_sessions),
        }

    return {
        "ok": True,
        "github_user": github_user,
        "boards": boards_summary,
        "sessions": sessions,
    }


COMMANDS = {
    "ping": handle_ping,
    "list_sessions": handle_list_sessions,
    "get_session_connect": handle_get_session_connect,
    "attach": handle_attach,
    "status": handle_status,
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
