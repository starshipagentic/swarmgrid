"""Edge agent daemon — the main process running on the user's machine.

Lifecycle:
1. Start upterm host with --force-command pointing to the worker
2. Parse the SSH connect string from upterm output
3. Register the connect string with the cloud
4. Start the heartbeat loop in a background thread
5. Monitor upterm — re-register if the token rotates
6. On shutdown, notify the cloud and clean up

Follows the same upterm lifecycle pattern as hub.py.
"""
from __future__ import annotations

import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import threading
from pathlib import Path
from typing import Any

from .registration import register_edge, report_offline

logger = logging.getLogger(__name__)

AGENT_SESSION = "swarmgrid-agent"
LOG_FILE = "/tmp/swarmgrid-agent-upterm.log"


def start_agent(
    *,
    config_path: str = "board-routes.yaml",
    upterm_server: str = "ssh://uptermd.upterm.dev:22",
    github_users: list[str] | None = None,
    foreground: bool = True,
) -> dict[str, Any]:
    """Start the edge agent daemon.

    In foreground mode (default), this blocks until interrupted.
    Returns a status dict when the agent stops.
    """
    if not shutil.which("upterm"):
        raise RuntimeError("upterm is not installed. Run: brew install upterm")
    if not shutil.which("tmux"):
        raise RuntimeError("tmux is not installed")

    logger.info("Starting SwarmGrid edge agent...")

    # Kill existing agent session if any
    subprocess.run(
        ["tmux", "kill-session", "-t", AGENT_SESSION],
        check=False,
        capture_output=True,
    )

    # Find python and build the force-command
    python_path = sys.executable or shutil.which("python3") or "python3"
    force_cmd = f"{python_path} -m swarmgrid.agent.worker"

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
    cmd_parts.extend(["--", "bash", "-c", "echo 'Agent is running. Ctrl-C to stop.'; sleep infinity"])

    shell_cmd = shlex.join(cmd_parts) + f" 2>&1 | tee {LOG_FILE}; sleep 999"

    # Launch in a tmux session
    subprocess.run(
        [
            "tmux", "new-session", "-d",
            "-s", AGENT_SESSION,
            "-x", "120", "-y", "30",
            shell_cmd,
        ],
        check=True,
        capture_output=True,
    )

    # Wait for upterm to establish and parse connection info
    ssh_connect = _wait_for_connect_string()
    if not ssh_connect:
        subprocess.run(
            ["tmux", "kill-session", "-t", AGENT_SESSION],
            check=False,
            capture_output=True,
        )
        log_content = _read_log()
        raise RuntimeError(f"Agent upterm failed to start. Log: {log_content[:500]}")

    logger.info("Agent upterm connected: %s", ssh_connect)

    # Register with the cloud
    reg_result = register_edge(ssh_connect)
    if reg_result.get("ok"):
        logger.info("Registered with cloud")
    else:
        logger.warning("Cloud registration failed (will retry): %s", reg_result.get("error"))

    if not foreground:
        return {
            "ok": True,
            "ssh_connect": ssh_connect,
            "tmux_session": AGENT_SESSION,
            "registered": reg_result.get("ok", False),
        }

    # Foreground mode: run heartbeat + monitor upterm
    stop_event = threading.Event()

    def _signal_handler(sig, frame):
        logger.info("Received signal %s, shutting down...", sig)
        stop_event.set()

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    # Start heartbeat in background thread
    heartbeat_thread = threading.Thread(
        target=_run_heartbeat_safe,
        args=(config_path, stop_event),
        daemon=True,
    )
    heartbeat_thread.start()

    # Monitor loop: check upterm is alive, re-register on token rotation
    last_connect = ssh_connect
    while not stop_event.is_set():
        stop_event.wait(timeout=30)
        if stop_event.is_set():
            break

        if not _session_exists(AGENT_SESSION):
            logger.error("Agent tmux session died — exiting")
            stop_event.set()
            break

        # Check if connect string changed (token rotation)
        current = _parse_connect_string()
        if current and current != last_connect:
            logger.info("Upterm token rotated, re-registering: %s", current)
            register_edge(current)
            last_connect = current

    # Graceful shutdown
    logger.info("Shutting down agent...")
    try:
        report_offline()
    except Exception:
        pass
    stop_hub()

    return {"ok": True, "stopped": True}


def stop_hub() -> bool:
    """Stop the agent tmux session."""
    if not _session_exists(AGENT_SESSION):
        return False
    subprocess.run(
        ["tmux", "kill-session", "-t", AGENT_SESSION],
        check=False,
        capture_output=True,
    )
    logger.info("Agent stopped")
    return True


def agent_status() -> dict[str, Any]:
    """Return agent status."""
    running = _session_exists(AGENT_SESSION)
    result: dict[str, Any] = {
        "running": running,
        "tmux_session": AGENT_SESSION,
        "ssh_connect": None,
    }
    if running:
        result["ssh_connect"] = _parse_connect_string()
    return result


def _run_heartbeat_safe(config_path: str, stop_event: threading.Event) -> None:
    """Run heartbeat loop, catching all exceptions."""
    try:
        from .heartbeat import run_heartbeat_loop
        run_heartbeat_loop(config_path, stop_event=stop_event)
    except Exception as exc:
        logger.error("Heartbeat thread crashed: %s", exc)


def _wait_for_connect_string(timeout: float = 10.0) -> str | None:
    """Wait for upterm to write its SSH connect string to the log."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.5)
        connect = _parse_connect_string()
        if connect:
            return connect
    return None


def _parse_connect_string() -> str | None:
    """Parse the SSH connect string from the upterm log file."""
    content = _read_log()
    if not content:
        return None
    match = re.search(r"ssh\s+(\S+@\S+(?:\s+-p\s+\d+)?)", content)
    if match:
        return f"ssh {match.group(1)}"
    return None


def _read_log() -> str:
    try:
        with open(LOG_FILE) as f:
            return f.read()
    except FileNotFoundError:
        return ""


def _session_exists(name: str) -> bool:
    result = subprocess.run(
        ["tmux", "has-session", "-t", name],
        check=False,
        capture_output=True,
    )
    return result.returncode == 0
