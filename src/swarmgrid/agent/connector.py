"""Connector — SSH into a front desk agent and query session info.

This is the CLIENT side of the front desk protocol. A teammate runs
``swarmgrid connect`` on their own machine, which SSHs into the session
owner's front desk upterm session (using the teammate's own SSH key,
authenticated via --github-user), sends a JSON command, parses the
response from noisy PTY output, and returns the parsed dict.

Unlike relay.py (which uses the cloud's identity key via -i), this
module uses the user's default SSH key — no -i flag.
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys

logger = logging.getLogger(__name__)


def frontdesk_query(
    frontdesk_connect: str,
    command: dict,
    timeout: int = 30,
) -> dict:
    """SSH into a front desk connect string and send a JSON command.

    Uses the caller's own SSH key (no -i flag).
    Parses JSON response from noisy PTY output.

    Args:
        frontdesk_connect: SSH connect string, e.g. "ssh TOKEN@uptermd.upterm.dev"
        command: JSON-serializable command dict
        timeout: SSH timeout in seconds

    Returns:
        Parsed JSON response dict. On failure, returns {"ok": False, "error": "..."}.
    """
    # Parse the ssh connect string into args
    parts = frontdesk_connect.strip().split()
    if parts and parts[0] == "ssh":
        ssh_args = parts[1:]
    else:
        ssh_args = parts

    cmd_json = json.dumps(command)

    try:
        # upterm requires PTY (-tt) and stdin must stay open briefly.
        # Use bash process substitution to feed the JSON and keep connection alive.
        # NO -i flag: uses the user's own default SSH key.
        bash_cmd = (
            f"ssh -tt -o StrictHostKeyChecking=no -o ConnectTimeout=10 "
            f"{' '.join(ssh_args)} "
            f"< <(echo '{cmd_json}'; sleep 2)"
        )
        result = subprocess.run(
            ["bash", "-c", bash_cmd],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            stderr = result.stderr.strip()
            logger.warning("SSH to front desk failed (rc=%d): %s", result.returncode, stderr)
            return {"ok": False, "error": f"SSH failed (rc={result.returncode}): {stderr}"}

        # With -tt, output includes echoed input and PTY noise.
        # Find the JSON response line.
        response_text = result.stdout.strip()
        if not response_text:
            return {"ok": False, "error": "Empty response from front desk"}

        # Extract JSON from potentially noisy output
        for line in response_text.splitlines():
            line = line.strip()
            # Remove ANSI escape codes
            import re
            clean = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", line)
            clean = clean.strip()
            if clean.startswith("{") and clean.endswith("}"):
                try:
                    parsed = json.loads(clean)
                    if "ok" in parsed:
                        return parsed
                except json.JSONDecodeError:
                    continue

        # Fallback: try to find any JSON object in the output
        import re
        for match in re.finditer(r"\{[^{}]+\}", response_text):
            try:
                parsed = json.loads(match.group())
                if "ok" in parsed:
                    return parsed
            except json.JSONDecodeError:
                continue

        return {"ok": False, "error": f"No valid JSON in response: {response_text[:200]}"}

    except subprocess.TimeoutExpired:
        logger.warning("SSH to front desk timed out after %ds", timeout)
        return {"ok": False, "error": "timeout"}
    except Exception as e:
        logger.error("Front desk query error: %s", e)
        return {"ok": False, "error": str(e)}


def get_session_connect(
    frontdesk_connect: str,
    github_user: str,
    ticket_key: str,
) -> dict:
    """Query the front desk for a ticket's real session SSH connect string.

    Returns:
        {"ok": True, "ssh_connect": "ssh TOKEN@...", "session_id": "..."}
        or {"ok": False, "error": "..."}
    """
    return frontdesk_query(
        frontdesk_connect,
        {
            "cmd": "get_session_connect",
            "github_user": github_user,
            "ticket_key": ticket_key,
        },
    )


def open_iterm2_ssh(ssh_connect: str) -> bool:
    """Open an iTerm2 window with an SSH session.

    Args:
        ssh_connect: Full SSH connect string, e.g. "ssh TOKEN@uptermd.upterm.dev"

    Returns:
        True if the AppleScript succeeded.
    """
    if sys.platform != "darwin":
        logger.error("open_iterm2_ssh only works on macOS")
        return False

    # Escape single quotes in the connect string for AppleScript
    escaped = ssh_connect.replace("'", "'\\''")
    applescript = (
        f'tell application "iTerm2" to create window with default profile '
        f'command "{escaped}"'
    )

    try:
        result = subprocess.run(
            ["osascript", "-e", applescript],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            logger.warning("osascript failed: %s", result.stderr.strip())
            return False
        return True
    except Exception as e:
        logger.error("Failed to open iTerm2: %s", e)
        return False


def discover_frontdesk(
    ticket_key: str,
    cloud_url: str | None = None,
    api_key: str | None = None,
) -> dict:
    """Auto-discover the front desk connect string for a ticket via the cloud API.

    Queries:
      GET /api/edge/nodes -> find which node has an active session for ticket_key
      GET /api/edge/nodes/{id}/frontdesk -> get the front desk connect string

    Args:
        ticket_key: Jira ticket key, e.g. "LMSV3-857"
        cloud_url: Cloud API base URL (reads from config if None)
        api_key: Cloud API key (reads from config if None)

    Returns:
        {"ok": True, "frontdesk_connect": "ssh ...", "hostname": "...", "node_id": N}
        or {"ok": False, "error": "..."}
    """
    import urllib.request
    import urllib.error

    if not cloud_url or not api_key:
        from .registration import _cloud_base_url, _api_key
        cloud_url = cloud_url or _cloud_base_url()
        api_key = api_key or _api_key()

    if not api_key:
        return {"ok": False, "error": "No cloud API key configured. Set api_key in ~/.swarmgrid/config.yaml"}

    headers = {
        "Authorization": f"Bearer {api_key}",
    }

    # Step 1: Get all visible nodes
    try:
        req = urllib.request.Request(
            f"{cloud_url}/api/edge/nodes",
            headers=headers,
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            nodes_data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        error_body = ""
        try:
            error_body = exc.read().decode()[:200]
        except Exception:
            pass
        return {"ok": False, "error": f"Failed to list nodes: HTTP {exc.code}: {error_body}"}
    except Exception as exc:
        return {"ok": False, "error": f"Failed to list nodes: {exc}"}

    nodes = nodes_data.get("nodes", [])
    if not nodes:
        return {"ok": False, "error": "No edge nodes found"}

    # Step 2: Find a node that has a front desk and is online
    # Try nodes with frontdesk that are online
    candidates = [n for n in nodes if n.get("online") and n.get("has_frontdesk")]
    if not candidates:
        candidates = [n for n in nodes if n.get("has_frontdesk")]
    if not candidates:
        return {"ok": False, "error": "No nodes with front desk agent found"}

    # Step 3: Get the front desk connect string from the first candidate
    # (In a multi-node setup, we'd need to check which node owns the ticket.
    #  For now, try the first online node with a front desk.)
    for candidate in candidates:
        node_id = candidate["id"]
        try:
            req = urllib.request.Request(
                f"{cloud_url}/api/edge/nodes/{node_id}/frontdesk",
                headers=headers,
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                fd_data = json.loads(resp.read())
                if fd_data.get("ok") and fd_data.get("frontdesk_connect"):
                    return {
                        "ok": True,
                        "frontdesk_connect": fd_data["frontdesk_connect"],
                        "hostname": candidate.get("hostname", "unknown"),
                        "node_id": node_id,
                    }
        except Exception as exc:
            logger.debug("Failed to get frontdesk for node %d: %s", node_id, exc)
            continue

    return {"ok": False, "error": "Could not retrieve front desk connect string from any node"}
