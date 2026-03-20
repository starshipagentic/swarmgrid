"""Hub lifecycle manager — start/stop the CGI-over-SSH hub via upterm.

The hub runs as a tmux session named ``swarmgrid-hub`` with upterm hosting
using ``--force-command`` to invoke ``hub_handler.py`` for each SSH connection.
"""
from __future__ import annotations

import logging
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

HUB_SESSION = "swarmgrid-hub"
HANDLER_PATH = Path(__file__).resolve().parent / "hub_handler.py"
DB_DIR = Path(__file__).resolve().parents[2] / "var" / "hub"
DB_PATH = DB_DIR / "hub.sqlite"
LOG_FILE = "/tmp/swarmgrid-hub-upterm.log"


def _session_exists(name: str) -> bool:
    result = subprocess.run(
        ["tmux", "has-session", "-t", name],
        check=False,
        capture_output=True,
    )
    return result.returncode == 0


def start_hub(
    *,
    upterm_server: str = "ssh://uptermd.upterm.dev:22",
    github_users: list[str] | None = None,
) -> dict[str, Any]:
    """Start the hub tmux session + upterm.

    Returns a dict with session_id and ssh_connect string.
    Raises RuntimeError on failure.
    """
    if not shutil.which("upterm"):
        raise RuntimeError("upterm is not installed. Run: brew install upterm")
    if not shutil.which("tmux"):
        raise RuntimeError("tmux is not installed")

    # Kill existing hub session
    subprocess.run(
        ["tmux", "kill-session", "-t", HUB_SESSION],
        check=False,
        capture_output=True,
    )

    # Find python interpreter — use the same one running this code
    python_path = _find_python()

    # Build upterm command with --force-command pointing to our handler
    force_cmd = f"{python_path} {HANDLER_PATH}"
    cmd_parts = [
        "upterm", "host",
        "--accept",
        "--skip-host-key-check",
        "--server", upterm_server,
        "--force-command", force_cmd,
    ]
    if github_users:
        for user in github_users:
            cmd_parts.extend(["--github-user", user])
    cmd_parts.extend(["--", "bash", "-c", "echo 'Hub is running. Ctrl-C to stop.'; sleep infinity"])

    shell_cmd = shlex.join(cmd_parts) + f" 2>&1 | tee {LOG_FILE}; sleep 999"

    # Launch in a tmux session
    subprocess.run(
        [
            "tmux", "new-session", "-d",
            "-s", HUB_SESSION,
            "-x", "120", "-y", "30",
            shell_cmd,
        ],
        check=True,
        capture_output=True,
    )

    # Wait for upterm to establish
    session_id = None
    ssh_connect = None
    for _ in range(20):
        time.sleep(0.5)
        try:
            with open(LOG_FILE) as f:
                content = f.read()
            match = re.search(r"Session:\s+(\S+)", content)
            if match:
                session_id = match.group(1)
            ssh_match = re.search(r"ssh\s+(\S+@\S+(?:\s+-p\s+\d+)?)", content)
            if ssh_match:
                ssh_connect = f"ssh {ssh_match.group(1)}"
            if session_id and ssh_connect:
                break
        except FileNotFoundError:
            continue

    if not session_id or not ssh_connect:
        subprocess.run(
            ["tmux", "kill-session", "-t", HUB_SESSION],
            check=False,
            capture_output=True,
        )
        log_content = ""
        try:
            with open(LOG_FILE) as f:
                log_content = f.read()
        except FileNotFoundError:
            pass
        raise RuntimeError(f"Hub upterm failed to start. Log: {log_content[:500]}")

    logger.info("Hub started: %s", ssh_connect)
    return {
        "session_id": session_id,
        "ssh_connect": ssh_connect,
        "tmux_session": HUB_SESSION,
    }


def stop_hub() -> bool:
    """Stop the hub session. Returns True if it was running."""
    if not _session_exists(HUB_SESSION):
        return False
    subprocess.run(
        ["tmux", "kill-session", "-t", HUB_SESSION],
        check=False,
        capture_output=True,
    )
    logger.info("Hub stopped")
    return True


def hub_status() -> dict[str, Any]:
    """Return hub status including connection info and DB summary."""
    running = _session_exists(HUB_SESSION)
    result: dict[str, Any] = {
        "running": running,
        "tmux_session": HUB_SESSION,
        "ssh_connect": None,
        "db_exists": DB_PATH.exists(),
        "checkin_count": 0,
        "unique_devs": 0,
    }

    # Try to read SSH connect from log
    if running:
        try:
            with open(LOG_FILE) as f:
                content = f.read()
            ssh_match = re.search(r"ssh\s+(\S+@\S+(?:\s+-p\s+\d+)?)", content)
            if ssh_match:
                result["ssh_connect"] = f"ssh {ssh_match.group(1)}"
        except FileNotFoundError:
            pass

    # DB summary
    if DB_PATH.exists():
        try:
            conn = sqlite3.connect(str(DB_PATH), timeout=5)
            row = conn.execute("SELECT COUNT(*) FROM checkins").fetchone()
            result["checkin_count"] = row[0] if row else 0
            row = conn.execute("SELECT COUNT(DISTINCT dev_id) FROM checkins").fetchone()
            result["unique_devs"] = row[0] if row else 0
            conn.close()
        except Exception:
            pass

    return result


def hub_team_data() -> dict[str, Any]:
    """Read hub.sqlite for the team view."""
    if not DB_PATH.exists():
        return {"checkins": [], "db_exists": False}

    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=5)
        rows = conn.execute(
            "SELECT dev_id, ticket_key, summary, status, checked_in_at, ssh_client "
            "FROM checkins ORDER BY checked_in_at DESC LIMIT 200"
        ).fetchall()
        conn.close()
    except Exception as exc:
        return {"checkins": [], "error": str(exc)}

    checkins = [
        {
            "dev_id": r[0],
            "ticket_key": r[1],
            "summary": r[2],
            "status": r[3],
            "checked_in_at": r[4],
            "ssh_client": r[5],
        }
        for r in rows
    ]
    return {"checkins": checkins, "db_exists": True}


def hub_checkin_via_ssh(
    ssh_connect: str,
    dev_id: str,
    tickets: list[dict[str, str]],
) -> dict[str, Any]:
    """Send a checkin to a remote hub via SSH.

    ``ssh_connect`` is the full command, e.g. ``ssh abc@uptermd.upterm.dev``.
    Returns the parsed JSON response or an error dict.
    """
    import json

    payload = json.dumps({
        "cmd": "checkin",
        "dev_id": dev_id,
        "tickets": tickets,
    })

    # Parse the ssh command into parts
    parts = ssh_connect.strip().split()
    if parts and parts[0] == "ssh":
        parts = parts[1:]  # drop the leading "ssh"

    try:
        result = subprocess.run(
            ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10", *parts],
            input=payload,
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            return {"ok": False, "error": f"ssh exit {result.returncode}: {result.stderr[:200]}"}
        if result.stdout.strip():
            return json.loads(result.stdout)
        return {"ok": False, "error": "empty response"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "ssh timeout (15s)"}
    except json.JSONDecodeError:
        return {"ok": False, "error": f"invalid JSON response: {result.stdout[:200]}"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _find_python() -> str:
    """Find the Python interpreter path."""
    import sys
    return sys.executable or shutil.which("python3") or "python3"
