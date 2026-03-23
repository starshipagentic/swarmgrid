"""Edge agent daemon — the main process running on the user's machine.

Starts TWO upterm sessions:

1. Phonebook agent (cloud-facing): --authorized-keys, force-command -> phonebook_worker.py
2. Front desk agent (team-facing): --github-user for teammates, force-command -> frontdesk_worker.py

Lifecycle:
1. Fetch authorized_keys (cloud key) and team config (github users)
2. Start phonebook upterm (cloud-facing, authorized_keys only)
3. Start front desk upterm (team-facing, github_users only)
4. Parse both SSH connect strings
5. Register both connect strings with the cloud
6. Start the heartbeat loop in a background thread
7. Monitor both sessions — re-register if tokens rotate
8. On shutdown, notify the cloud and clean up
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

from .registration import fetch_authorized_keys, fetch_team_config, register_edge, report_offline

logger = logging.getLogger(__name__)

# Two separate tmux sessions for the two agents
PHONEBOOK_SESSION = "swarmgrid-phonebook"
FRONTDESK_SESSION = "swarmgrid-frontdesk"
AGENT_SESSION = PHONEBOOK_SESSION  # backwards compat alias

PHONEBOOK_LOG = "/tmp/swarmgrid-phonebook-upterm.log"
FRONTDESK_LOG = "/tmp/swarmgrid-frontdesk-upterm.log"
LOG_FILE = PHONEBOOK_LOG  # backwards compat alias


def start_agent(
    *,
    config_path: str = "board-routes.yaml",
    upterm_server: str = "ssh://uptermd.upterm.dev:22",
    github_users: list[str] | None = None,
    foreground: bool = True,
) -> dict[str, Any]:
    """Start the edge agent daemon with TWO upterm sessions.

    - Phonebook agent: cloud-facing, --authorized-keys (cloud key only)
    - Front desk agent: team-facing, --github-user (teammates only)

    In foreground mode (default), this blocks until interrupted.
    Returns a status dict when the agent stops.
    """
    if not shutil.which("upterm"):
        raise RuntimeError("upterm is not installed. Run: brew install upterm")
    if not shutil.which("tmux"):
        raise RuntimeError("tmux is not installed")

    logger.info("Starting SwarmGrid edge agent (two-agent architecture)...")

    # Kill existing sessions if any
    for session in (PHONEBOOK_SESSION, FRONTDESK_SESSION):
        subprocess.run(
            ["tmux", "kill-session", "-t", session],
            check=False,
            capture_output=True,
        )

    # Find python path for force-commands
    if getattr(sys, 'frozen', False):
        python_path = shutil.which("python3") or "python3"
    else:
        python_path = sys.executable or shutil.which("python3") or "python3"

    # ── 1. Phonebook agent setup (cloud-facing) ──────────────────────
    # Fetch authorized_keys from the cloud (cloud SSH key only).
    auth_keys_path = Path.home() / ".swarmgrid" / "authorized_keys"
    auth_data = fetch_authorized_keys()
    cloud_keys = auth_data.get("authorized_keys", [])

    if cloud_keys:
        auth_keys_path.parent.mkdir(parents=True, exist_ok=True)
        auth_keys_path.write_text("\n".join(cloud_keys) + "\n")
        os.chmod(str(auth_keys_path), 0o600)
        logger.info("Wrote %d authorized key(s) to %s", len(cloud_keys), auth_keys_path)
    elif auth_keys_path.exists():
        logger.warning("Cloud unreachable — using cached authorized_keys from %s", auth_keys_path)
    else:
        logger.warning("No authorized keys available — phonebook agent will run without --authorized-keys")

    phonebook_force_cmd = f"{python_path} -m swarmgrid.agent.phonebook_worker"
    phonebook_cmd_parts = [
        "upterm", "host",
        "--accept",
        "--skip-host-key-check",
        "--server", upterm_server,
        "--force-command", phonebook_force_cmd,
    ]
    if auth_keys_path.exists():
        phonebook_cmd_parts.extend(["--authorized-keys", str(auth_keys_path)])
    # NO --github-user on phonebook — cloud uses authorized_keys only
    phonebook_cmd_parts.extend([
        "--", "bash", "-c",
        "echo 'Phonebook agent running.'; while true; do sleep 86400; done",
    ])

    phonebook_shell = (
        shlex.join(phonebook_cmd_parts)
        + f" 2>&1 | tee {PHONEBOOK_LOG}; echo 'Upterm exited.'; while true; do sleep 86400; done"
    )

    # ── 2. Front desk agent setup (team-facing) ──────────────────────
    # Fetch team config to discover github_users for all boards
    team_config = fetch_team_config()
    all_github_users = _collect_github_users(team_config, github_users)

    frontdesk_force_cmd = f"{python_path} -m swarmgrid.agent.frontdesk_worker"
    frontdesk_cmd_parts = [
        "upterm", "host",
        "--accept",
        "--skip-host-key-check",
        "--server", upterm_server,
        "--force-command", frontdesk_force_cmd,
    ]
    # NO --authorized-keys on front desk — teammates use github auth only
    if all_github_users:
        for user in all_github_users:
            frontdesk_cmd_parts.extend(["--github-user", user])
    else:
        logger.warning("No github users configured for front desk agent")
    frontdesk_cmd_parts.extend([
        "--", "bash", "-c",
        "echo 'Front desk agent running.'; while true; do sleep 86400; done",
    ])

    frontdesk_shell = (
        shlex.join(frontdesk_cmd_parts)
        + f" 2>&1 | tee {FRONTDESK_LOG}; echo 'Upterm exited.'; while true; do sleep 86400; done"
    )

    # ── 3. Launch both tmux sessions ─────────────────────────────────
    for session_name, shell_cmd in [
        (PHONEBOOK_SESSION, phonebook_shell),
        (FRONTDESK_SESSION, frontdesk_shell),
    ]:
        subprocess.run(
            [
                "tmux", "new-session", "-d",
                "-s", session_name,
                "-x", "120", "-y", "30",
                shell_cmd,
            ],
            check=True,
            capture_output=True,
        )

    # ── 4. Wait for both connect strings ─────────────────────────────
    phonebook_connect = _wait_for_connect_string(PHONEBOOK_LOG)
    if not phonebook_connect:
        _kill_both_sessions()
        log_content = _read_log(PHONEBOOK_LOG)
        raise RuntimeError(f"Phonebook upterm failed to start. Log: {log_content[:500]}")
    logger.info("Phonebook upterm connected: %s", phonebook_connect)

    frontdesk_connect = _wait_for_connect_string(FRONTDESK_LOG)
    if not frontdesk_connect:
        _kill_both_sessions()
        log_content = _read_log(FRONTDESK_LOG)
        raise RuntimeError(f"Front desk upterm failed to start. Log: {log_content[:500]}")
    logger.info("Front desk upterm connected: %s", frontdesk_connect)

    # ── 5. Register both with the cloud ──────────────────────────────
    reg_result = register_edge(phonebook_connect, frontdesk_connect=frontdesk_connect)
    if reg_result.get("ok"):
        logger.info("Registered both agents with cloud")
    else:
        logger.warning("Cloud registration failed (will retry): %s", reg_result.get("error"))

    if not foreground:
        return {
            "ok": True,
            "phonebook_connect": phonebook_connect,
            "frontdesk_connect": frontdesk_connect,
            "ssh_connect": phonebook_connect,  # backwards compat
            "phonebook_session": PHONEBOOK_SESSION,
            "frontdesk_session": FRONTDESK_SESSION,
            "tmux_session": PHONEBOOK_SESSION,  # backwards compat
            "registered": reg_result.get("ok", False),
        }

    # ── 6. Foreground mode: heartbeat + monitor ──────────────────────
    stop_event = threading.Event()

    def _signal_handler(sig, frame):
        logger.info("Received signal %s, shutting down...", sig)
        stop_event.set()

    try:
        signal.signal(signal.SIGINT, _signal_handler)
        signal.signal(signal.SIGTERM, _signal_handler)
    except ValueError:
        pass  # Not in main thread — signals handled by parent

    # Start heartbeat in background thread
    heartbeat_thread = threading.Thread(
        target=_run_heartbeat_safe,
        args=(config_path, stop_event),
        daemon=True,
    )
    heartbeat_thread.start()

    # Monitor loop: check both sessions alive, re-register on token rotation
    last_phonebook = phonebook_connect
    last_frontdesk = frontdesk_connect
    while not stop_event.is_set():
        stop_event.wait(timeout=30)
        if stop_event.is_set():
            break

        # Check phonebook session
        if not _session_exists(PHONEBOOK_SESSION):
            logger.error("Phonebook tmux session died — exiting")
            stop_event.set()
            break

        # Check front desk session
        if not _session_exists(FRONTDESK_SESSION):
            logger.error("Front desk tmux session died — exiting")
            stop_event.set()
            break

        # Check for token rotation on either session
        current_phonebook = _parse_connect_string(PHONEBOOK_LOG)
        current_frontdesk = _parse_connect_string(FRONTDESK_LOG)
        rotated = False

        if current_phonebook and current_phonebook != last_phonebook:
            logger.info("Phonebook upterm token rotated: %s", current_phonebook)
            last_phonebook = current_phonebook
            rotated = True
        if current_frontdesk and current_frontdesk != last_frontdesk:
            logger.info("Front desk upterm token rotated: %s", current_frontdesk)
            last_frontdesk = current_frontdesk
            rotated = True

        if rotated:
            register_edge(last_phonebook, frontdesk_connect=last_frontdesk)

    # Graceful shutdown
    logger.info("Shutting down agent...")
    try:
        report_offline()
    except Exception:
        pass
    stop_hub()

    return {"ok": True, "stopped": True}


def stop_hub() -> bool:
    """Stop both agent tmux sessions."""
    stopped_any = False
    for session in (PHONEBOOK_SESSION, FRONTDESK_SESSION):
        if _session_exists(session):
            subprocess.run(
                ["tmux", "kill-session", "-t", session],
                check=False,
                capture_output=True,
            )
            logger.info("Stopped session: %s", session)
            stopped_any = True
    if stopped_any:
        logger.info("Agent stopped")
    return stopped_any


def agent_status() -> dict[str, Any]:
    """Return status of both agent sessions."""
    phonebook_running = _session_exists(PHONEBOOK_SESSION)
    frontdesk_running = _session_exists(FRONTDESK_SESSION)

    result: dict[str, Any] = {
        "running": phonebook_running or frontdesk_running,
        "phonebook": {
            "running": phonebook_running,
            "tmux_session": PHONEBOOK_SESSION,
            "ssh_connect": _parse_connect_string(PHONEBOOK_LOG) if phonebook_running else None,
        },
        "frontdesk": {
            "running": frontdesk_running,
            "tmux_session": FRONTDESK_SESSION,
            "ssh_connect": _parse_connect_string(FRONTDESK_LOG) if frontdesk_running else None,
        },
        # Backwards compat fields
        "tmux_session": PHONEBOOK_SESSION,
        "ssh_connect": _parse_connect_string(PHONEBOOK_LOG) if phonebook_running else None,
    }
    return result


def _collect_github_users(team_config: dict, cli_users: list[str] | None = None) -> list[str]:
    """Collect ALL unique github users across all boards in team config, plus CLI overrides."""
    users: list[str] = list(cli_users or [])
    boards = team_config.get("boards", {})
    for board_key, board_info in boards.items():
        for gu in board_info.get("github_users", []):
            if gu not in users:
                users.append(gu)
    if users:
        logger.info("Front desk github users: %s", ", ".join(users))
    return users


def _kill_both_sessions() -> None:
    """Kill both tmux sessions (cleanup on startup failure)."""
    for session in (PHONEBOOK_SESSION, FRONTDESK_SESSION):
        subprocess.run(
            ["tmux", "kill-session", "-t", session],
            check=False,
            capture_output=True,
        )


def _run_heartbeat_safe(config_path: str, stop_event: threading.Event) -> None:
    """Run heartbeat loop, catching all exceptions."""
    try:
        from .heartbeat import run_heartbeat_loop
        run_heartbeat_loop(config_path, stop_event=stop_event)
    except Exception as exc:
        logger.error("Heartbeat thread crashed: %s", exc)


def _wait_for_connect_string(log_file: str, timeout: float = 10.0) -> str | None:
    """Wait for upterm to write its SSH connect string to the given log file."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.5)
        connect = _parse_connect_string(log_file)
        if connect:
            return connect
    return None


def _parse_connect_string(log_file: str) -> str | None:
    """Parse the SSH connect string from an upterm log file."""
    content = _read_log(log_file)
    if not content:
        return None
    match = re.search(r"ssh\s+(\S+@\S+(?:\s+-p\s+\d+)?)", content)
    if match:
        return f"ssh {match.group(1)}"
    return None


def _read_log(log_file: str) -> str:
    try:
        with open(log_file) as f:
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
