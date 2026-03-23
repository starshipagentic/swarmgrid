"""SSH command relay — sends JSON commands to edge nodes via their upterm connect string."""
from __future__ import annotations

import json
import logging
import os
import subprocess

logger = logging.getLogger(__name__)


def send_command(ssh_connect: str, command: dict, timeout: int = 30) -> dict:
    """Send a JSON command to an edge node via SSH.

    Args:
        ssh_connect: The SSH connect string (e.g. "ssh abc@relay.example.com")
        command: The command dict (e.g. {"cmd": "ping"})
        timeout: SSH command timeout in seconds

    Returns:
        Parsed JSON response from the edge worker.
    """
    # Parse the ssh connect string into args
    # Format: "ssh <token>@<relay>" or full "ssh -o StrictHostKeyChecking=no <token>@<relay>"
    parts = ssh_connect.strip().split()
    if parts and parts[0] == "ssh":
        ssh_args = parts[1:]
    else:
        ssh_args = parts

    cmd_json = json.dumps(command)

    try:
        # upterm requires PTY (-tt) and stdin must stay open briefly.
        # Use bash process substitution to feed the JSON and keep connection alive.
        # -i identifies the cloud with its persistent SSH key (for --authorized-keys on edge).
        key_path = "/data/.ssh/id_ed25519"
        identity_flag = f"-i {key_path}" if os.path.exists(key_path) else ""
        bash_cmd = f"ssh -tt -o StrictHostKeyChecking=no -o ConnectTimeout=10 {identity_flag} {' '.join(ssh_args)} < <(echo '{cmd_json}'; sleep 2)"
        result = subprocess.run(
            ["bash", "-c", bash_cmd],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            logger.warning("SSH command failed (rc=%d): %s", result.returncode, result.stderr.strip())
            return {"ok": False, "error": f"SSH failed (rc={result.returncode}): {result.stderr.strip()}"}

        # With -tt, output may include echoed input and PTY noise.
        # Find the JSON response (starts with { and ends with })
        response_text = result.stdout.strip()
        if not response_text:
            return {"ok": False, "error": "Empty response from edge node"}

        # Extract JSON from potentially noisy output
        for line in response_text.splitlines():
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                try:
                    parsed = json.loads(line)
                    if "ok" in parsed or "pong" in parsed:
                        return parsed
                except json.JSONDecodeError:
                    continue

        # Fallback: try parsing the whole thing
        return json.loads(response_text)

    except subprocess.TimeoutExpired:
        logger.warning("SSH command timed out after %ds to %s", timeout, ssh_connect)
        return {"ok": False, "error": "timeout"}
    except json.JSONDecodeError as e:
        logger.warning("Invalid JSON response from edge: %s", e)
        return {"ok": False, "error": f"Invalid JSON from edge: {e}"}
    except Exception as e:
        logger.error("SSH relay error: %s", e)
        return {"ok": False, "error": str(e)}


def ping(ssh_connect: str) -> bool:
    """Ping an edge node. Returns True if alive."""
    result = send_command(ssh_connect, {"cmd": "ping"}, timeout=10)
    return result.get("ok", False)


def launch_session(ssh_connect: str, ticket_key: str, prompt: str, session_config: dict | None = None) -> dict:
    """Send a launch command to an edge node."""
    cmd = {"cmd": "launch", "ticket_key": ticket_key, "prompt": prompt}
    if session_config:
        cmd["session_config"] = session_config
    return send_command(ssh_connect, cmd)


def get_session_status(ssh_connect: str, session_id: str) -> dict:
    """Get the status of a specific session on an edge node."""
    return send_command(ssh_connect, {"cmd": "status", "session_id": session_id})


def capture_output(ssh_connect: str, session_id: str, lines: int = 50) -> dict:
    """Capture terminal output from a session on an edge node."""
    return send_command(ssh_connect, {"cmd": "capture", "session_id": session_id, "lines": lines})


def kill_session(ssh_connect: str, session_id: str) -> dict:
    """Kill a session on an edge node."""
    return send_command(ssh_connect, {"cmd": "kill", "session_id": session_id})


def list_sessions(ssh_connect: str) -> dict:
    """List all active sessions on an edge node."""
    return send_command(ssh_connect, {"cmd": "list"})


def phonebook_status(ssh_connect: str) -> dict:
    """Get status from the phonebook agent."""
    return send_command(ssh_connect, {"cmd": "status"})


def phonebook_sessions(ssh_connect: str) -> dict:
    """Get partial session summary from the phonebook agent."""
    return send_command(ssh_connect, {"cmd": "sessions_summary"})


def attach_session(ssh_connect: str, ticket_key: str = "", session_id: str = "") -> dict:
    """Tell an edge node to open a tmux session in iTerm2/Terminal.

    Uses the phonebook agent's open_local command (limited, cloud-facing).
    Falls back to the old 'attach' command for backwards compatibility.
    """
    cmd: dict = {"cmd": "open_local"}
    if ticket_key:
        cmd["ticket_key"] = ticket_key
    elif session_id:
        cmd["ticket_key"] = session_id  # open_local uses ticket_key
    else:
        return {"ok": False, "error": "ticket_key or session_id required"}
    return send_command(ssh_connect, cmd)
