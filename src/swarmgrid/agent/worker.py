"""Edge worker — CGI handler invoked per SSH connection.

Follows the same pattern as hub_handler.py: reads a single JSON line
from stdin, dispatches the command, writes a JSON response to stdout,
and exits.

This script is invoked by upterm's --force-command for each incoming
SSH connection from the cloud (or another edge node).
"""
from __future__ import annotations

import json
import sys


def handle_ping(_payload: dict) -> dict:
    import platform
    return {
        "ok": True,
        "pong": True,
        "hostname": platform.node(),
        "os": platform.system(),
    }


def handle_launch(payload: dict) -> dict:
    from .session_manager import launch_session

    ticket_key = payload.get("ticket_key", "")
    prompt = payload.get("prompt", "")
    if not ticket_key or not prompt:
        return {"ok": False, "error": "ticket_key and prompt are required"}

    session_config = payload.get("session_config") or {}
    return launch_session(
        ticket_key,
        prompt,
        session_config=session_config,
    )


def handle_status(payload: dict) -> dict:
    from .session_manager import session_status

    session_id = payload.get("session_id", "")
    if not session_id:
        return {"ok": False, "error": "session_id required"}
    return session_status(session_id)


def handle_capture(payload: dict) -> dict:
    from .session_manager import capture_output

    session_id = payload.get("session_id", "")
    if not session_id:
        return {"ok": False, "error": "session_id required"}
    lines = payload.get("lines", 50)
    return capture_output(session_id, lines=lines)


def handle_kill(payload: dict) -> dict:
    from .session_manager import kill_session

    session_id = payload.get("session_id", "")
    if not session_id:
        return {"ok": False, "error": "session_id required"}
    return kill_session(session_id)


def handle_list(_payload: dict) -> dict:
    from .session_manager import list_sessions
    return list_sessions()


def handle_config(payload: dict) -> dict:
    """Receive route/template updates from the cloud.

    Writes updated config to the local board-routes.yaml or a
    cloud-managed overlay file.
    """
    routes = payload.get("routes")
    if routes is None:
        return {"ok": False, "error": "no routes provided"}

    # Store cloud-pushed config as an overlay
    from pathlib import Path
    overlay_dir = Path.home() / ".swarmgrid"
    overlay_dir.mkdir(parents=True, exist_ok=True)
    overlay_path = overlay_dir / "cloud-routes.json"
    overlay_path.write_text(json.dumps({
        "routes": routes,
        "templates": payload.get("templates", []),
    }, indent=2), encoding="utf-8")

    return {"ok": True, "stored": str(overlay_path)}


def handle_credential_receive(payload: dict) -> dict:
    """Receive a credential from another edge node (edge-to-edge sync)."""
    from .credential_sync import receive_credential

    key = payload.get("key", "")
    value = payload.get("value", "")
    if not key or not value:
        return {"ok": False, "error": "key and value required"}
    return receive_credential(key, value)


COMMANDS = {
    "ping": handle_ping,
    "launch": handle_launch,
    "status": handle_status,
    "capture": handle_capture,
    "kill": handle_kill,
    "list": handle_list,
    "config": handle_config,
    "credential_receive": handle_credential_receive,
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
