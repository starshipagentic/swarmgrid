"""macOS status bar app for SwarmGrid using rumps.

Shows a tiny icon in the menu bar with connection status.
Starts the agent daemon in a background thread and periodically
updates the menu with active tickets and agent state.
"""
from __future__ import annotations

import logging
import os
import subprocess
import threading
import webbrowser
from pathlib import Path

import rumps

from ..agent.daemon import start_agent, stop_hub, agent_status, AGENT_SESSION
from ..agent.session_manager import list_sessions, SESSION_PREFIX

logger = logging.getLogger(__name__)

DASHBOARD_URL = "https://swarmgrid.org"
AGENT_LOG = "/tmp/swarmgrid-agent-upterm.log"
UPDATE_INTERVAL = 10  # seconds

# Paths to icon PNGs (set at runtime by _resolve_icons)
_ICON_CONNECTED: str | None = None
_ICON_DISCONNECTED: str | None = None


def _resolve_icons() -> tuple[str | None, str | None]:
    """Find icon files relative to package or in resources/."""
    candidates = [
        Path(__file__).resolve().parent.parent.parent.parent / "resources",
        Path(__file__).resolve().parent / "resources",
    ]
    connected = disconnected = None
    for d in candidates:
        c = d / "icon_connected.png"
        dc = d / "icon_disconnected.png"
        if c.exists():
            connected = str(c)
        if dc.exists():
            disconnected = str(dc)
        if connected and disconnected:
            break
    return connected, disconnected


def _ticket_key_from_session(session_id: str) -> str:
    """Extract 'GRID-142' from 'swarmgrid-grid-142-20260320...'."""
    rest = session_id[len(SESSION_PREFIX):]
    # Find the last segment that looks like a timestamp slug (all digits)
    parts = rest.rsplit("-", 1)
    if len(parts) == 2 and parts[1].isdigit():
        key_part = parts[0]
    else:
        key_part = rest
    return key_part.upper()


class SwarmGridApp(rumps.App):
    def __init__(
        self,
        *,
        config_path: str = "board-routes.yaml",
        upterm_server: str = "ssh://uptermd.upterm.dev:22",
        github_users: list[str] | None = None,
    ):
        global _ICON_CONNECTED, _ICON_DISCONNECTED
        _ICON_CONNECTED, _ICON_DISCONNECTED = _resolve_icons()

        super().__init__(
            "SwarmGrid",
            icon=_ICON_DISCONNECTED,
            quit_button=None,  # we add our own
        )

        self._config_path = config_path
        self._upterm_server = upterm_server
        self._github_users = github_users
        self._paused = False
        self._agent_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._connected = False

        # Build initial menu
        self._status_item = rumps.MenuItem("Offline", callback=None)
        self._status_item.set_callback(None)
        self._separator1 = rumps.separator
        self._tickets_placeholder = rumps.MenuItem("No active tickets", callback=None)
        self._tickets_placeholder.set_callback(None)
        self._separator2 = rumps.separator
        self._dashboard_item = rumps.MenuItem("Open Dashboard", callback=self._open_dashboard)
        self._pause_item = rumps.MenuItem("Pause", callback=self._toggle_pause)
        self._logs_item = rumps.MenuItem("View Logs", callback=self._view_logs)
        self._separator3 = rumps.separator
        self._quit_item = rumps.MenuItem("Quit SwarmGrid", callback=self._quit)

        self.menu = [
            self._status_item,
            self._separator1,
            self._tickets_placeholder,
            self._separator2,
            self._dashboard_item,
            self._pause_item,
            self._logs_item,
            self._separator3,
            self._quit_item,
        ]

        # Start agent daemon in background
        self._start_agent_thread()

        # Periodic update timer
        self._timer = rumps.Timer(self._update_menu, UPDATE_INTERVAL)
        self._timer.start()

    def _start_agent_thread(self):
        """Launch the agent daemon in a background thread."""
        def _run():
            try:
                start_agent(
                    config_path=self._config_path,
                    upterm_server=self._upterm_server,
                    github_users=self._github_users,
                    foreground=True,
                )
            except Exception as exc:
                logger.error("Agent thread error: %s", exc)
            finally:
                self._connected = False

        self._agent_thread = threading.Thread(target=_run, daemon=True, name="swarmgrid-agent")
        self._agent_thread.start()

    def _update_menu(self, _sender=None):
        """Periodic callback to refresh menu state."""
        try:
            status = agent_status()
            self._connected = status.get("running", False)
        except Exception:
            self._connected = False

        # Update icon
        if self._connected:
            if _ICON_CONNECTED:
                self.icon = _ICON_CONNECTED
            self.title = ""
        else:
            if _ICON_DISCONNECTED:
                self.icon = _ICON_DISCONNECTED
            self.title = ""

        # Get active sessions
        try:
            sessions_data = list_sessions()
            sessions = sessions_data.get("sessions", [])
        except Exception:
            sessions = []

        # Update status line
        if self._paused:
            status_text = "Paused"
        elif self._connected:
            n = len(sessions)
            agents_word = "agent" if n == 1 else "agents"
            status_text = f"Connected \u00b7 {n} {agents_word} running"
        else:
            status_text = "Offline"

        self._status_item.title = status_text

        # Rebuild ticket list — clear old dynamic items and re-add
        # Remove any existing ticket items (between separator1 and separator2)
        keys_to_remove = [
            k for k in self.menu.keys()
            if isinstance(k, str) and k not in {
                self._status_item.title,
                "Open Dashboard", "Pause", "Resume",
                "View Logs", "Quit SwarmGrid",
                "No active tickets",
            }
        ]
        for k in keys_to_remove:
            try:
                del self.menu[k]
            except KeyError:
                pass

        # Remove placeholder if we have real sessions
        if sessions:
            try:
                del self.menu["No active tickets"]
            except KeyError:
                pass

            for sess in sessions:
                sid = sess.get("session_id", "")
                state = sess.get("state", "unknown")
                ticket = _ticket_key_from_session(sid)
                icon = "\u2739" if state == "running" else "\u25cc"
                item_title = f"{ticket} {icon} {state}"
                item = rumps.MenuItem(item_title, callback=self._make_session_callback(sid))
                # Insert after status item
                self.menu.insert_after(self._status_item.title, item)
        else:
            # Ensure placeholder is present
            if "No active tickets" not in self.menu:
                placeholder = rumps.MenuItem("No active tickets", callback=None)
                placeholder.set_callback(None)
                self.menu.insert_after(self._status_item.title, placeholder)

    def _make_session_callback(self, session_id):
        """Return a callback that opens iTerm2 attached to a tmux session."""
        def _cb(_sender):
            from ..runner import open_session_in_terminal
            opened = open_session_in_terminal({"session_name": session_id})
            if not opened:
                # Fallback: copy attach command to clipboard
                cmd = f"tmux attach -t {session_id}"
                subprocess.run(["pbcopy"], input=cmd.encode(), check=False)
                rumps.notification("SwarmGrid", "Copied to clipboard", cmd)
        return _cb

    def _open_dashboard(self, _sender):
        webbrowser.open(DASHBOARD_URL)

    def _toggle_pause(self, _sender):
        self._paused = not self._paused
        if self._paused:
            self._pause_item.title = "Resume"
            # Signal the heartbeat to pause by setting the stop event
            self._stop_event.set()
        else:
            self._pause_item.title = "Pause"
            # Restart agent if it stopped
            self._stop_event.clear()
            if self._agent_thread and not self._agent_thread.is_alive():
                self._start_agent_thread()

    def _view_logs(self, _sender):
        """Open the agent log file in Console.app or default text viewer."""
        log_path = AGENT_LOG
        if os.path.exists(log_path):
            subprocess.Popen(["open", "-a", "Console", log_path])
        else:
            # Create empty log so there's something to open
            Path(log_path).touch()
            subprocess.Popen(["open", "-a", "Console", log_path])

    def _quit(self, _sender):
        """Graceful shutdown."""
        self._stop_event.set()
        try:
            stop_hub()
        except Exception:
            pass
        rumps.quit_application()


def run_menubar_app(
    *,
    config_path: str = "board-routes.yaml",
    upterm_server: str = "ssh://uptermd.upterm.dev:22",
    github_users: list[str] | None = None,
):
    """Entry point to launch the menu bar app."""
    app = SwarmGridApp(
        config_path=config_path,
        upterm_server=upterm_server,
        github_users=github_users,
    )
    app.run()
