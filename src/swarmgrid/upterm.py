"""Upterm session sharing for swarmgrid tmux sessions.

Manages upterm host processes that expose tmux sessions via SSH relay.
Each shared session gets a unique session ID and SSH connect string.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import re
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Any

logger = logging.getLogger(__name__)


def _upterm_socket_dir() -> Path:
    """Return the directory where upterm stores admin sockets."""
    system = platform.system()
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "upterm"
    # Linux / other: XDG_RUNTIME_DIR or /tmp
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        return Path(xdg) / "upterm"
    return Path("/tmp") / f"upterm-{os.getuid()}"


@dataclass
class SharedSession:
    """An active upterm shared session."""

    issue_key: str
    tmux_session: str
    session_id: str
    ssh_connect: str
    admin_socket: str
    tmux_wrapper_session: str  # the tmux session running upterm host
    read_only: bool = False
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "issue_key": self.issue_key,
            "tmux_session": self.tmux_session,
            "session_id": self.session_id,
            "ssh_connect": self.ssh_connect,
            "read_only": self.read_only,
            "created_at": self.created_at,
        }


class UptermManager:
    """Manages upterm sharing sessions.

    Each issue_key can have at most one active share.
    Upterm host runs inside a dedicated tmux session so it has a TTY.
    """

    def __init__(self, server: str = "ssh://uptermd.upterm.dev:22") -> None:
        self._lock = Lock()
        self._shares: dict[str, SharedSession] = {}  # issue_key -> SharedSession
        self._server = server
        self._available: bool | None = None  # lazy-checked

    @property
    def available(self) -> bool:
        """Check if the upterm CLI is installed."""
        if self._available is None:
            import shutil
            self._available = shutil.which("upterm") is not None
        return self._available

    def share(
        self,
        issue_key: str,
        tmux_session: str,
        *,
        read_only: bool = False,
        github_users: list[str] | None = None,
        authorized_keys: str | None = None,
    ) -> SharedSession:
        """Start sharing a tmux session via upterm.

        Returns the SharedSession with connection info.
        Raises RuntimeError if upterm fails to start.
        """
        with self._lock:
            existing = self._shares.get(issue_key)
            if existing and self._is_alive(existing):
                return existing

            # Clean up dead share if any
            if existing:
                self._cleanup(existing)

            return self._start_share(
                issue_key,
                tmux_session,
                read_only=read_only,
                github_users=github_users,
                authorized_keys=authorized_keys,
            )

    def unshare(self, issue_key: str) -> bool:
        """Stop sharing a session. Returns True if a share was stopped."""
        with self._lock:
            share = self._shares.pop(issue_key, None)
            if share:
                self._cleanup(share)
                return True
            return False

    def get_share(self, issue_key: str) -> SharedSession | None:
        """Get the active share for an issue, or None."""
        with self._lock:
            share = self._shares.get(issue_key)
            if share and self._is_alive(share):
                return share
            if share:
                self._cleanup(share)
                self._shares.pop(issue_key, None)
            return None

    def list_shares(self) -> list[SharedSession]:
        """List all active shares."""
        with self._lock:
            alive = []
            dead_keys = []
            for key, share in self._shares.items():
                if self._is_alive(share):
                    alive.append(share)
                else:
                    dead_keys.append(key)
            for key in dead_keys:
                self._cleanup(self._shares.pop(key))
            return alive

    def get_client_count(self, issue_key: str) -> int:
        """Get the number of connected clients for a share."""
        share = self.get_share(issue_key)
        if not share:
            return 0
        info = self._query_session(share)
        return info.get("clientCount", 0) if info else 0

    def _start_share(
        self,
        issue_key: str,
        tmux_session: str,
        *,
        read_only: bool = False,
        github_users: list[str] | None = None,
        authorized_keys: str | None = None,
    ) -> SharedSession:
        wrapper_name = f"upterm-{issue_key.lower()}"

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
            "--server", self._server,
            "--force-command", f"tmux attach-session -t {tmux_session}",
        ]
        if read_only:
            cmd_parts.append("--read-only")
        if github_users:
            for user in github_users:
                cmd_parts.extend(["--github-user", user])
        if authorized_keys:
            cmd_parts.extend(["--authorized-keys", authorized_keys])

        cmd_parts.extend(["--", "tmux", "attach-session", "-t", tmux_session])

        log_file = f"/tmp/upterm-{issue_key.lower()}.log"
        shell_cmd = shlex.join(cmd_parts) + f" 2>&1 | tee {log_file}; sleep 999"

        # Launch upterm inside a tmux session (needs a TTY)
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

        # Find admin socket
        socket_dir = _upterm_socket_dir()
        admin_socket = str(socket_dir / f"{session_id}.sock")

        share = SharedSession(
            issue_key=issue_key,
            tmux_session=tmux_session,
            session_id=session_id,
            ssh_connect=ssh_connect,
            admin_socket=admin_socket,
            tmux_wrapper_session=wrapper_name,
            read_only=read_only,
        )
        self._shares[issue_key] = share
        logger.info("Started upterm share for %s: %s", issue_key, ssh_connect)
        return share

    def _is_alive(self, share: SharedSession) -> bool:
        """Check if the upterm wrapper tmux session is still running."""
        result = subprocess.run(
            ["tmux", "has-session", "-t", share.tmux_wrapper_session],
            check=False,
            capture_output=True,
        )
        return result.returncode == 0

    def _cleanup(self, share: SharedSession) -> None:
        """Kill the upterm wrapper session."""
        subprocess.run(
            ["tmux", "kill-session", "-t", share.tmux_wrapper_session],
            check=False,
            capture_output=True,
        )
        logger.info("Cleaned up upterm share for %s", share.issue_key)

    def _query_session(self, share: SharedSession) -> dict[str, Any] | None:
        """Query session info via the admin socket."""
        try:
            result = subprocess.run(
                [
                    "upterm", "session", "current",
                    "--admin-socket", share.admin_socket,
                    "-o", "json",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            return json.loads(result.stdout)
        except (subprocess.CalledProcessError, json.JSONDecodeError):
            return None
