from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from socket import socket
from threading import Lock, Thread
import asyncio
import logging
import re
import shlex
import shutil
import subprocess
import time
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import yaml

from .config import AppConfig, load_config, load_yaml
from .jira import JiraClient
from .operator_settings import OperatorSettings, load_operator_settings, save_operator_settings
from .runner import (
    capture_session_output,
    classify_process_row,
    launch_manual_tmux_shell,
    launch_decision,
    open_session_in_terminal,
    reconcile_processes,
    terminate_process,
    _claude_extra_args,
    _tmux_pane_pid,
    _tmux_session_exists,
    _capture_tmux_pane,
    _tmux_send_enter,
    _tmux_send_literal,
)
from .service import (
    _apply_launch_side_effects,
    _pre_launch_transition,
    get_status,
    heartbeat_status,
    run_heartbeat,
    utc_now,
)
from .state import StateStore
from .models import JiraIssue, LaunchRecord, RouteDecision

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "web_static"
BOARD_ROW_LIMIT = 25
TRIGGER_DIR = Path(__file__).resolve().parents[2] / "var" / "dagster-trigger"


class SetupUpdate(BaseModel):
    jira_email: str | None = None
    token_file: str | None = None
    claude_command: str | None = None
    claude_working_dir: str | None = None
    claude_max_parallel: int | None = None
    site_url: str | None = None
    project_key: str | None = None
    board_id: str | None = None
    poll_interval_minutes: int | None = None


class RouteUpdate(BaseModel):
    prompt_template: str | None = None
    transition_on_launch: str | None = None
    transition_on_success: str | None = None
    transition_on_failure: str | None = None
    allowed_issue_types: list[str] | None = None
    enabled: bool | None = None


class RouteCreate(BaseModel):
    status: str
    action: str = "claude_default"
    prompt_template: str = ""
    enabled: bool = False


class BoardCreate(BaseModel):
    site_url: str
    project_key: str
    board_id: str
    working_dir: str | None = None


class ScratchAttachRequest(BaseModel):
    issue_key: str


class ObserverRequest(BaseModel):
    session_name: str
    lines: int | None = 80


class ObserverInputRequest(BaseModel):
    session_name: str
    text: str = ""
    press_enter: bool = True


def dagster_is_active() -> bool:
    """Check whether the dagster daemon is running and driving heartbeats.

    Only trusts the sentinel file age.  The DAGSTER_HOME env var is NOT
    checked because it persists across shell sessions even when dagster
    is not running, causing the webapp to incorrectly defer heartbeats.
    """
    sentinel = Path(__file__).resolve().parents[2] / "var" / "dagster" / "daemon_active"
    if sentinel.exists():
        try:
            age = time.time() - sentinel.stat().st_mtime
            if age < 600:
                return True
        except OSError:
            pass
    return False


def write_dagster_trigger(label: str = "manual") -> Path:
    """Write a trigger file for the dagster manual_trigger_sensor."""
    TRIGGER_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
    path = TRIGGER_DIR / f"{label}-{stamp}"
    path.write_text(f"{label}\n", encoding="utf-8")
    return path


def _try_hub_checkin(config_path: str) -> None:
    """Best-effort hub checkin after a heartbeat tick.

    Reads operator settings for hub_ssh_connect and hub_dev_id.
    If configured, collects running tickets and sends a checkin via SSH.
    If the local hub is running, writes directly to SQLite instead.
    Never raises — failures are logged and swallowed.
    """
    try:
        config = load_config(config_path)
        settings = load_operator_settings(config.operator_settings_path)
        dev_id = settings.hub_dev_id
        if not dev_id:
            return  # No dev_id configured, skip

        # Collect running tickets
        store = StateStore(config.local_state_dir)
        from .runner import reconcile_processes
        reconcile_processes(store)
        running = store.list_running_processes()
        tickets = []
        for row in running:
            issue = store.get_issue_state(row["issue_key"])
            tickets.append({
                "key": row["issue_key"],
                "summary": (issue or {}).get("summary", ""),
                "status": (issue or {}).get("status_name", row.get("status_name", "")),
            })

        if not tickets:
            return  # Nothing to report

        from .hub import hub_checkin_via_ssh, _session_exists, HUB_SESSION, DB_PATH
        # If local hub is running, write directly (faster, no SSH roundtrip)
        if _session_exists(HUB_SESSION):
            import json
            from .hub_handler import handle_checkin
            handle_checkin({"dev_id": dev_id, "tickets": tickets})
            logger.debug("Hub checkin (local): %d tickets", len(tickets))
            return

        # Remote hub via SSH
        ssh_connect = settings.hub_ssh_connect
        if not ssh_connect:
            return  # No remote hub configured

        result = hub_checkin_via_ssh(ssh_connect, dev_id, tickets)
        if result.get("ok"):
            logger.debug("Hub checkin (ssh): %d tickets", len(tickets))
        else:
            logger.warning("Hub checkin failed: %s", result.get("error"))
    except Exception as exc:
        logger.debug("Hub checkin error (ignored): %s", exc)


class WebHeartbeatController:
    def __init__(self, config_path: str) -> None:
        self.config_path = config_path
        self.auto_enabled = True
        self.last_tick_result: dict[str, Any] | None = None
        self.last_tick_error: str | None = None
        self.next_run_at = datetime.now(UTC)
        self._lock = Lock()
        self._thread = Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while True:
            # If dagster is the driver, skip the built-in heartbeat loop.
            if dagster_is_active():
                time.sleep(5.0)
                continue

            with self._lock:
                config = load_config(self.config_path)
                enabled = self.auto_enabled
                due = datetime.now(UTC) >= self.next_run_at
            if enabled and due:
                try:
                    result = run_heartbeat(self.config_path)
                    with self._lock:
                        self.last_tick_result = result
                        self.last_tick_error = None
                        self.next_run_at = datetime.now(UTC) + timedelta(minutes=config.poll_interval_minutes)
                    # Auto-checkin to hub after successful heartbeat
                    _try_hub_checkin(self.config_path)
                except Exception as exc:
                    with self._lock:
                        self.last_tick_error = str(exc)
                        self.next_run_at = datetime.now(UTC) + timedelta(minutes=max(config.poll_interval_minutes, 1))
            time.sleep(1.0)

    def trigger_now(self) -> dict[str, Any]:
        """Trigger an immediate heartbeat.

        Always runs heartbeat directly for immediate UI feedback.
        When dagster is active, also writes a trigger file for the sensor.
        """
        dagster = dagster_is_active()
        if dagster:
            trigger_path = write_dagster_trigger("heartbeat-now")
            logger.info("Dagster active: wrote trigger %s (also running direct for UI)", trigger_path)

        result = run_heartbeat(self.config_path, force_reconsider=True)
        with self._lock:
            self.last_tick_result = result
            self.last_tick_error = None
            config = load_config(self.config_path)
            self.next_run_at = datetime.now(UTC) + timedelta(minutes=config.poll_interval_minutes)
        return result

    def toggle_auto(self) -> dict[str, Any]:
        with self._lock:
            self.auto_enabled = not self.auto_enabled
            if self.auto_enabled:
                config = load_config(self.config_path)
                self.next_run_at = datetime.now(UTC) + timedelta(minutes=config.poll_interval_minutes)
            return {
                "auto_enabled": self.auto_enabled,
                "dagster_active": dagster_is_active(),
                "next_run_at": self.next_run_at.isoformat(),
                "last_tick_result": self.last_tick_result,
                "last_tick_error": self.last_tick_error,
            }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "auto_enabled": self.auto_enabled,
                "dagster_active": dagster_is_active(),
                "next_run_at": self.next_run_at.isoformat(),
                "last_tick_result": self.last_tick_result,
                "last_tick_error": self.last_tick_error,
            }


class TtydManager:
    def __init__(self) -> None:
        self._lock = Lock()
        self._by_issue: dict[str, dict[str, Any]] = {}

    def ensure(self, issue_key: str, session_name: str, root: Path, host: str = "127.0.0.1") -> dict[str, Any]:
        with self._lock:
            current = self._by_issue.get(issue_key)
            if current and current.get("session_name") == session_name:
                process = current.get("process")
                if process and process.poll() is None:
                    return {"port": current["port"], "url": f"http://{host}:{current['port']}/"}

            port = self._reserve_port()
            command = [
                "ttyd",
                "-p",
                str(port),
                "-W",
                str(root / "scripts" / "ttyd_attach_wrapper_web.sh"),
                session_name,
            ]
            process = subprocess.Popen(
                command,
                cwd=root,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            url = f"http://{host}:{port}/"
            self._by_issue[issue_key] = {
                "session_name": session_name,
                "port": port,
                "url": url,
                "process": process,
            }
            return {"port": port, "url": url}

    def ensure_command(self, key: str, command: list[str], cwd: Path, host: str = "127.0.0.1") -> dict[str, Any]:
        with self._lock:
            current = self._by_issue.get(key)
            if current and current.get("command") == command:
                process = current.get("process")
                if process and process.poll() is None:
                    return {"port": current["port"], "url": f"http://{host}:{current['port']}/"}

            port = self._reserve_port()
            ttyd_command = ["ttyd", "-p", str(port), "-W", *command]
            process = subprocess.Popen(
                ttyd_command,
                cwd=cwd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            url = f"http://{host}:{port}/"
            self._by_issue[key] = {
                "command": command,
                "port": port,
                "url": url,
                "process": process,
            }
            return {"port": port, "url": url}

    @staticmethod
    def _reserve_port() -> int:
        with socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])


def _build_health(config_path: str, upterm_available: bool) -> dict[str, Any]:
    """Build a health-check dict.  Never raises — each check returns False on error."""
    result: dict[str, Any] = {
        "jira_connected": False,
        "jira_statuses_valid": [],
        "claude_available": False,
        "tmux_available": shutil.which("tmux") is not None,
        "upterm_available": upterm_available,
        "working_dir_valid": False,
        "working_dir": None,
    }

    try:
        config = load_config(config_path)
    except Exception:
        return result

    # 1. Jira connectivity
    try:
        jira = JiraClient(config)
        jira.validate_auth()
        result["jira_connected"] = True
    except Exception:
        pass

    # 2. Route status validation against the board map
    try:
        board_map = load_yaml(config.board_map_path)
        known_statuses = list(board_map.get("status_map", {}).keys())
        status_checks = []
        for route in config.routes:
            status_checks.append({
                "status": route.status,
                "valid": route.status in known_statuses,
                "enabled": route.enabled,
            })
        result["jira_statuses_valid"] = status_checks
    except Exception:
        pass

    # 3. Claude CLI on PATH
    try:
        result["claude_available"] = shutil.which("claude") is not None
    except Exception:
        pass

    # 4. Working dir exists
    try:
        settings = load_operator_settings(config.operator_settings_path)
        workdir = settings.claude_working_dir or config.llm.working_dir
        result["working_dir_valid"] = bool(workdir and Path(workdir).is_dir())
        result["working_dir"] = workdir
    except Exception:
        result["working_dir_valid"] = False

    # 5. Heartbeat diagnostics from shared backend
    try:
        store = StateStore(config.local_state_dir)
        result["heartbeat"] = heartbeat_status(config, store)
    except Exception:
        pass

    return result


def create_app(
    config_path: str = "board-routes.yaml",
    extra_config_paths: list[str] | None = None,
) -> FastAPI:
    app = FastAPI(title="SwarmGrid")
    app.state.controller = WebHeartbeatController(config_path)
    app.state.config_path = config_path
    # Multi-board: store all known config paths (primary first)
    # Auto-discover configs in boards/ directory next to primary config
    all_paths = [config_path]
    for p in (extra_config_paths or []):
        if p not in all_paths:
            all_paths.append(p)
    boards_dir = Path(config_path).resolve().parent / "boards"
    if boards_dir.is_dir():
        for bp in sorted(boards_dir.glob("*.yaml")) + sorted(boards_dir.glob("*.yml")):
            bp_str = str(bp)
            if bp_str not in all_paths:
                all_paths.append(bp_str)
    app.state.board_config_paths = all_paths
    app.state.ttyd = TtydManager()
    from .upterm import UptermManager
    _config = load_config(config_path)
    _settings = load_operator_settings(_config.operator_settings_path)
    _upterm_server = _settings.upterm_server or "ssh://uptermd.upterm.dev:22"
    app.state.upterm = UptermManager(server=_upterm_server)
    app.state.upterm_server = _upterm_server

    @app.middleware("http")
    async def no_store(request: Request, call_next):
        response = await call_next(request)
        if request.url.path == "/" or request.url.path in {"/board", "/routes", "/setup", "/team"} or request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response

    @app.get("/")
    @app.get("/board")
    @app.get("/routes")
    @app.get("/setup")
    @app.get("/sharing")
    @app.get("/team")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/webui2")
    def webui2() -> FileResponse:
        return FileResponse(STATIC_DIR / "webui2.html")

    @app.get("/testwebtmux1")
    def testwebtmux1() -> FileResponse:
        return FileResponse(STATIC_DIR / "testwebtmux1.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/api/snapshot")
    def snapshot() -> dict[str, Any]:
        data = build_snapshot(app.state.config_path, app.state.controller.snapshot())
        # Inject upterm share status into ticket rows
        sharing_available = app.state.upterm.available
        data["sharing_available"] = sharing_available
        if sharing_available:
            shares = {s.issue_key: s for s in app.state.upterm.list_shares()}
            for col in data.get("columns", []):
                for ticket in col.get("tickets", []):
                    share = shares.get(ticket["key"])
                    if share:
                        ticket["shared"] = True
                        ticket["ssh_connect"] = share.ssh_connect
                        ticket["share_clients"] = app.state.upterm.get_client_count(ticket["key"])
                    else:
                        ticket["shared"] = False
        # Attach health check so the UI has it every refresh cycle
        data["health"] = _build_health(app.state.config_path, app.state.upterm.available)
        # Top-level heartbeat source for easy UI consumption
        data["heartbeat_source"] = "dagster" if dagster_is_active() else "web"
        return data

    @app.get("/api/boards")
    def list_boards() -> dict[str, Any]:
        """List all configured boards (multi-board support)."""
        from .config import board_name_from_config
        boards = []
        for idx, bpath in enumerate(app.state.board_config_paths):
            try:
                cfg = load_config(bpath)
                boards.append({
                    "index": idx,
                    "name": board_name_from_config(cfg),
                    "config_path": bpath,
                    "project_key": cfg.project_key,
                    "site_url": cfg.site_url,
                    "board_id": cfg.board_id,
                    "active": bpath == app.state.config_path,
                })
            except Exception as exc:
                boards.append({
                    "index": idx,
                    "name": Path(bpath).stem,
                    "config_path": bpath,
                    "error": str(exc),
                    "active": bpath == app.state.config_path,
                })
        return {"boards": boards}

    @app.get("/api/boards/{index}/snapshot")
    def board_snapshot(index: int) -> dict[str, Any]:
        """Get snapshot for a specific board by index."""
        paths = app.state.board_config_paths
        if index < 0 or index >= len(paths):
            raise HTTPException(status_code=404, detail=f"Board index {index} out of range")
        board_path = paths[index]
        ctrl = app.state.controller.snapshot() if board_path == app.state.config_path else {}
        return build_snapshot(board_path, ctrl)

    @app.post("/api/boards/{index}/switch")
    def switch_board(index: int) -> dict[str, Any]:
        """Switch the active board (primary config path)."""
        paths = app.state.board_config_paths
        if index < 0 or index >= len(paths):
            raise HTTPException(status_code=404, detail=f"Board index {index} out of range")
        new_path = paths[index]
        app.state.config_path = new_path
        # Restart the controller for the new board
        app.state.controller = WebHeartbeatController(new_path)
        return {"ok": True, "active_config": new_path}

    @app.post("/api/boards")
    def create_board(body: BoardCreate) -> dict[str, Any]:
        """Create a new board config in the boards/ directory."""
        # Load the primary config as template for jira/llm/jira_actions settings
        primary_path = Path(app.state.board_config_paths[0])
        primary_raw = yaml.safe_load(primary_path.read_text(encoding="utf-8")) or {}

        # Create boards/ directory next to the primary config
        boards_dir = primary_path.parent / "boards"
        boards_dir.mkdir(parents=True, exist_ok=True)

        # Generate config filename
        slug = body.project_key.lower().replace(" ", "-")
        new_filename = f"board-routes-{slug}.yaml"
        new_path = boards_dir / new_filename

        if new_path.exists():
            raise HTTPException(
                status_code=409,
                detail=f"Board config already exists: {new_path}",
            )

        # Build new config YAML, copying jira/llm settings from primary.
        # Paths are relative to the boards/ directory where the config lives.
        new_raw: dict[str, Any] = {
            "site_url": body.site_url.rstrip("/"),
            "project_key": body.project_key,
            "board_id": body.board_id,
            "board_map_path": f"../{body.project_key}.jira-map.yaml",
            "operator_settings_path": "../" + str(primary_raw.get("operator_settings_path", "./operator-settings.yaml")).lstrip("./"),
            "poll_interval_minutes": primary_raw.get("poll_interval_minutes", 5),
            "stale_display_minutes": primary_raw.get("stale_display_minutes", 1440),
            "local_state_dir": f"../var/heartbeat-{slug}",
            "jira": dict(primary_raw.get("jira", {})),
            "llm": {**dict(primary_raw.get("llm", {})), **({"working_dir": body.working_dir} if body.working_dir else {})},
            "jira_actions": dict(primary_raw.get("jira_actions", {"enabled": False})),
            "routes": [],
        }

        new_path.write_text(yaml.safe_dump(new_raw, sort_keys=False), encoding="utf-8")

        # Register with the app
        new_path_str = str(new_path)
        if new_path_str not in app.state.board_config_paths:
            app.state.board_config_paths.append(new_path_str)

        new_index = app.state.board_config_paths.index(new_path_str)
        return {
            "ok": True,
            "index": new_index,
            "config_path": new_path_str,
            "project_key": body.project_key,
            "board_id": body.board_id,
            "site_url": body.site_url,
        }

    @app.post("/api/heartbeat")
    def heartbeat_now() -> dict[str, Any]:
        result = app.state.controller.trigger_now()
        return {"ok": True, "result": result}

    @app.post("/api/auto/toggle")
    def toggle_auto() -> dict[str, Any]:
        return {"ok": True, "controller": app.state.controller.toggle_auto()}

    @app.post("/api/routes/{status}/toggle")
    def toggle_route(status: str) -> dict[str, Any]:
        config = load_config(app.state.config_path)
        raw = yaml.safe_load(Path(config.config_path).read_text(encoding="utf-8")) or {}
        routes = raw.get("routes", [])
        updated = False
        for route in routes:
            if route.get("status") == status:
                route["enabled"] = not bool(route.get("enabled"))
                updated = True
                break
        if not updated:
            raise HTTPException(status_code=404, detail=f"Unknown route status: {status}")
        raw["routes"] = routes
        Path(config.config_path).write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
        return {"ok": True, "status": status}

    @app.put("/api/routes/{status}")
    def update_route(status: str, update: RouteUpdate) -> dict[str, Any]:
        config = load_config(app.state.config_path)
        raw = yaml.safe_load(Path(config.config_path).read_text(encoding="utf-8")) or {}
        routes = raw.get("routes", [])
        target = None
        for route in routes:
            if route.get("status") == status:
                target = route
                break
        if target is None:
            raise HTTPException(status_code=404, detail=f"Unknown route status: {status}")
        if update.prompt_template is not None:
            target["prompt_template"] = update.prompt_template
        if update.transition_on_launch is not None:
            target["transition_on_launch"] = update.transition_on_launch
        if update.transition_on_success is not None:
            target["transition_on_success"] = update.transition_on_success
        if update.transition_on_failure is not None:
            target["transition_on_failure"] = update.transition_on_failure
        if update.allowed_issue_types is not None:
            target["allowed_issue_types"] = update.allowed_issue_types
        if update.enabled is not None:
            target["enabled"] = update.enabled
        raw["routes"] = routes
        Path(config.config_path).write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
        return {"ok": True, "status": status}

    @app.get("/api/board/columns")
    def board_columns() -> dict[str, Any]:
        """Discover actual board columns from Jira and cross-reference with configured routes."""
        config = load_config(app.state.config_path)
        try:
            jira = JiraClient(config)
            columns = jira.fetch_board_columns()
        except Exception as exc:
            logger.warning("Failed to fetch board columns: %s", exc)
            columns = []

        # All status names present on the board
        board_status_names: set[str] = set()
        for col in columns:
            for st in col.get("statuses", []):
                board_status_names.add(st.get("name", ""))

        configured_routes = [route.status for route in config.routes]
        valid_routes = [s for s in configured_routes if s in board_status_names]
        invalid_routes = [s for s in configured_routes if s not in board_status_names]

        # Fetch project issue types
        issue_types: list[str] = []
        try:
            resp = jira._session.get(f"{config.site_url}/rest/api/3/project/{config.project_key}")
            if resp.ok:
                issue_types = [t["name"] for t in resp.json().get("issueTypes", [])]
        except Exception:
            pass

        return {
            "columns": columns,
            "configured_routes": configured_routes,
            "valid_routes": valid_routes,
            "invalid_routes": invalid_routes,
            "issue_types": issue_types,
        }

    @app.post("/api/routes")
    def create_route(body: RouteCreate) -> dict[str, Any]:
        """Create a new route in the config YAML."""
        config = load_config(app.state.config_path)
        raw = yaml.safe_load(Path(config.config_path).read_text(encoding="utf-8")) or {}
        routes = raw.get("routes", [])

        # Check for duplicate
        for route in routes:
            if route.get("status") == body.status:
                raise HTTPException(
                    status_code=409,
                    detail=f"Route for status '{body.status}' already exists",
                )

        new_route = {
            "status": body.status,
            "action": body.action,
            "prompt_template": body.prompt_template,
            "enabled": body.enabled,
            "allowed_issue_types": [],
            "fire_on_first_seen": True,
            "transition_on_launch": None,
            "transition_on_success": None,
            "transition_on_failure": None,
        }
        routes.append(new_route)
        raw["routes"] = routes
        Path(config.config_path).write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
        return {"ok": True, "route": new_route}

    @app.delete("/api/routes/{status}")
    def delete_route(status: str) -> dict[str, Any]:
        """Remove a route from the config YAML."""
        config = load_config(app.state.config_path)
        raw = yaml.safe_load(Path(config.config_path).read_text(encoding="utf-8")) or {}
        routes = raw.get("routes", [])

        original_len = len(routes)
        routes = [r for r in routes if r.get("status") != status]
        if len(routes) == original_len:
            raise HTTPException(status_code=404, detail=f"No route found for status '{status}'")

        raw["routes"] = routes
        Path(config.config_path).write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
        return {"ok": True, "deleted_status": status}

    @app.get("/api/setup")
    def get_setup() -> dict[str, Any]:
        config = load_config(app.state.config_path)
        settings = load_operator_settings(config.operator_settings_path)
        return {
            "jira_email": settings.jira_email or "",
            "token_file": settings.token_file or config.jira.token_file,
            "claude_command": settings.claude_command or config.llm.command,
            "claude_working_dir": settings.claude_working_dir or config.llm.working_dir or "",
            "claude_max_parallel": settings.claude_max_parallel or config.llm.max_parallel,
            "site_url": config.site_url,
            "project_key": config.project_key,
            "board_id": config.board_id or "",
            "poll_interval_minutes": config.poll_interval_minutes,
        }

    @app.post("/api/setup")
    def save_setup(update: SetupUpdate) -> dict[str, Any]:
        config = load_config(app.state.config_path)
        settings = load_operator_settings(config.operator_settings_path)
        merged = OperatorSettings(
            jira_email=update.jira_email if update.jira_email is not None else settings.jira_email,
            token_file=update.token_file if update.token_file is not None else (settings.token_file or config.jira.token_file),
            claude_command=update.claude_command if update.claude_command is not None else (settings.claude_command or config.llm.command),
            claude_working_dir=update.claude_working_dir if update.claude_working_dir is not None else (settings.claude_working_dir or config.llm.working_dir),
            claude_max_parallel=update.claude_max_parallel if update.claude_max_parallel is not None else (settings.claude_max_parallel or config.llm.max_parallel),
        )
        save_operator_settings(config.operator_settings_path, merged)

        raw = yaml.safe_load(Path(config.config_path).read_text(encoding="utf-8")) or {}
        if update.site_url is not None:
            raw["site_url"] = update.site_url
        if update.project_key is not None:
            raw["project_key"] = update.project_key
        if update.board_id is not None:
            raw["board_id"] = update.board_id
        if update.poll_interval_minutes is not None:
            raw["poll_interval_minutes"] = update.poll_interval_minutes
        Path(config.config_path).write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
        return {"ok": True}

    @app.post("/api/tickets/{issue_key}/open")
    def open_ticket(issue_key: str) -> dict[str, Any]:
        row = _find_process_row(app.state.config_path, issue_key)
        if not row:
            raise HTTPException(status_code=404, detail=f"No local session for {issue_key}")
        if not open_session_in_terminal(row):
            raise HTTPException(status_code=500, detail=f"Could not open terminal for {issue_key}")
        return {"ok": True}

    @app.post("/api/tickets/{issue_key}/kill")
    def kill_ticket(issue_key: str) -> dict[str, Any]:
        config = load_config(app.state.config_path)
        store = StateStore(config.local_state_dir)
        row = _find_process_row(app.state.config_path, issue_key)
        if not row:
            raise HTTPException(status_code=404, detail=f"No local session for {issue_key}")
        terminated = terminate_process(store, row)
        return {"ok": True, "terminated": terminated}

    @app.post("/api/tickets/{issue_key}/run-now")
    def run_ticket_now(issue_key: str) -> dict[str, Any]:
        return manual_launch_issue(app.state.config_path, issue_key)

    @app.post("/api/tickets/{issue_key}/ttyd")
    def ttyd_ticket(issue_key: str, request: Request) -> dict[str, Any]:
        row = _find_process_row(app.state.config_path, issue_key)
        if not row or not row.get("session_name"):
            raise HTTPException(status_code=404, detail=f"No local session for {issue_key}")
        host = request.url.hostname or "127.0.0.1"
        info = app.state.ttyd.ensure(issue_key, row["session_name"], Path(__file__).resolve().parents[2], host=host)
        return {"ok": True, **info}

    @app.get("/api/tickets/{issue_key}/timeline")
    def ticket_timeline(issue_key: str) -> dict[str, Any]:
        config = load_config(app.state.config_path)
        store = StateStore(config.local_state_dir)
        transitions = store.get_transitions(issue_key)
        if not transitions:
            # Try a live fetch if nothing stored
            try:
                jira = JiraClient(config)
                transitions = jira.fetch_issue_changelog(issue_key)
                bot_account_id: str | None = None
                try:
                    myself = jira.validate_auth()
                    bot_account_id = myself.get("account_id")
                except Exception:
                    pass
                for t in transitions:
                    t["is_bot"] = bool(bot_account_id and t.get("author_id") == bot_account_id)
                if transitions:
                    store.store_transitions(issue_key, transitions)
            except Exception as exc:
                logger.warning("Live changelog fetch failed for %s: %s", issue_key, exc)
        return {"issue_key": issue_key, "transitions": transitions}

    @app.post("/api/observer")
    def observe_session(payload: ObserverRequest) -> dict[str, Any]:
        session_name = payload.session_name
        if not _tmux_session_exists(session_name):
            raise HTTPException(status_code=404, detail=f"tmux session {session_name} is not running")
        lines = max(20, min(int(payload.lines or 80), 300))
        alt = _capture_tmux_pane(session_name, lines=lines, alternate=True)
        normal = _capture_tmux_pane(session_name, lines=lines, alternate=False)
        output = alt.strip() or normal.strip()
        return {
            "ok": True,
            "session_name": session_name,
            "output": output,
        }

    @app.post("/api/observer/input")
    def observer_input(payload: ObserverInputRequest) -> dict[str, Any]:
        session_name = payload.session_name
        if not _tmux_session_exists(session_name):
            raise HTTPException(status_code=404, detail=f"tmux session {session_name} is not running")
        if payload.text:
            _tmux_send_literal(session_name, payload.text)
        if payload.press_enter:
            _tmux_send_enter(session_name)
        return {"ok": True}

    @app.post("/api/debug/ttyd/plain-shell")
    def ttyd_plain_shell(request: Request) -> dict[str, Any]:
        host = request.url.hostname or "127.0.0.1"
        info = app.state.ttyd.ensure_command(
            "debug-plain-shell",
            ["/bin/zsh", "-l"],
            Path(__file__).resolve().parents[2],
            host=host,
        )
        return {"ok": True, **info}

    @app.post("/api/debug/ttyd/tmux-shell")
    def ttyd_tmx_shell(request: Request) -> dict[str, Any]:
        host = request.url.hostname or "127.0.0.1"
        session = "webui2-shell-test"
        if not _tmux_session_exists(session):
            subprocess.run(
                ["tmux", "new-session", "-d", "-s", session, "-x", "160", "-y", "45", "/bin/zsh", "-l"],
                check=False,
            )
        info = app.state.ttyd.ensure_command(
            "debug-tmux-shell",
            [str(Path(__file__).resolve().parents[2] / "scripts" / "ttyd_attach_wrapper_web.sh"), session],
            Path(__file__).resolve().parents[2],
            host=host,
        )
        return {"ok": True, **info}

    # --- Scratch terminal endpoints ---

    @app.post("/api/scratch-terminal")
    def create_scratch_terminal() -> dict[str, Any]:
        """Create a new scratch tmux session with Claude running inside."""
        if shutil.which("tmux") is None:
            raise HTTPException(status_code=400, detail="tmux is not installed")

        compact_ts = (
            datetime.now(UTC)
            .isoformat()
            .replace(":", "")
            .replace("-", "")
            .replace("+00:00", "z")
            .replace(".", "")
        )[:20]
        name = f"scratch-{compact_ts}"

        config = load_config(app.state.config_path)
        settings = load_operator_settings(config.operator_settings_path)
        working_dir = settings.claude_working_dir or config.llm.working_dir or None

        tmux_cmd = ["tmux", "new-session", "-d", "-x", "185", "-y", "55", "-s", name]
        if working_dir:
            tmux_cmd.extend(["-c", working_dir])
        tmux_cmd.append("/bin/zsh")
        tmux_cmd.append("-l")
        subprocess.run(tmux_cmd, check=True)

        subprocess.run(["tmux", "set-option", "-t", name, "window-size", "manual"], check=True)
        subprocess.run(["tmux", "resize-window", "-t", name, "-x", "185", "-y", "55"], check=True)
        subprocess.run(["tmux", "set-option", "-t", name, "mouse", "on"], check=True)
        subprocess.run(["tmux", "set-option", "-t", name, "set-clipboard", "on"], check=True)

        # Launch claude inside the session
        command_name = settings.claude_command or config.llm.command
        extra_args = _claude_extra_args(command_name)
        claude_cmd = shlex.join([command_name, *extra_args])
        _tmux_send_literal(name, claude_cmd)
        _tmux_send_enter(name)

        return {"ok": True, "session_name": name}

    @app.get("/api/scratch-terminals")
    def list_scratch_terminals() -> dict[str, Any]:
        """List all scratch tmux sessions."""
        sessions = _list_scratch_sessions()
        return {"ok": True, "sessions": sessions}

    @app.delete("/api/scratch-terminals/{session_name}")
    def kill_scratch_terminal(session_name: str) -> dict[str, Any]:
        """Kill a scratch tmux session."""
        if not session_name.startswith("scratch-"):
            raise HTTPException(status_code=400, detail="Can only kill scratch-* sessions")
        if not _tmux_session_exists(session_name):
            raise HTTPException(status_code=404, detail=f"Session {session_name} not found")
        subprocess.run(
            ["tmux", "kill-session", "-t", session_name],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return {"ok": True, "session_name": session_name}

    @app.post("/api/scratch-terminals/{session_name}/attach")
    def attach_scratch_to_ticket(session_name: str, body: ScratchAttachRequest) -> dict[str, Any]:
        """Rename a scratch session to a ticket session and record it."""
        if not session_name.startswith("scratch-"):
            raise HTTPException(status_code=400, detail="Can only attach scratch-* sessions")
        if not _tmux_session_exists(session_name):
            raise HTTPException(status_code=404, detail=f"Session {session_name} not found")

        issue_key = body.issue_key.strip().upper()
        if not issue_key:
            raise HTTPException(status_code=400, detail="issue_key is required")

        config = load_config(app.state.config_path)
        store = StateStore(config.local_state_dir)

        compact_ts = (
            datetime.now(UTC)
            .isoformat()
            .replace(":", "")
            .replace("-", "")
            .replace("+00:00", "z")
            .replace(".", "")
        )[:24]
        new_name = f"swarmgrid-{issue_key.lower()}-{compact_ts}"

        # Rename the tmux session
        subprocess.run(
            ["tmux", "rename-session", "-t", session_name, new_name],
            check=True,
        )

        # Record in state store
        pid = _tmux_pane_pid(new_name)
        created_at = datetime.now(UTC).isoformat()

        launch = LaunchRecord(
            run_id=None,
            issue_key=issue_key,
            status_name="",
            action="scratch_attached",
            prompt="",
            state="running",
            pid=pid,
            log_path="",
            command_line=f"scratch terminal attached from {session_name}",
            run_dir="",
            artifact_globs=[],
            session_name=new_name,
            launch_mode="tmux",
        )
        launch.run_id = store.record_process_run(launch, created_at=created_at)

        return {"ok": True, "session_name": new_name, "issue_key": issue_key}

    @app.websocket("/ws/scratch/{session_name}/terminal")
    async def scratch_terminal_socket(websocket: WebSocket, session_name: str) -> None:
        """WebSocket terminal for scratch sessions."""
        await websocket.accept()
        if not session_name.startswith("scratch-"):
            await websocket.send_json({"type": "error", "message": "Not a scratch session"})
            await websocket.close(code=4400)
            return
        if not _tmux_session_exists(session_name):
            await websocket.send_json({"type": "error", "message": f"Session {session_name} not found"})
            await websocket.close(code=4404)
            return
        await _mirror_tmux_terminal(websocket, session_name)

    # --- Upterm sharing endpoints ---

    @app.post("/api/tickets/{issue_key}/share")
    def share_ticket(issue_key: str, request: Request) -> dict[str, Any]:
        """Start sharing a tmux session via upterm."""
        if not app.state.upterm.available:
            raise HTTPException(status_code=400, detail="upterm is not installed. Run: brew install upterm")
        row = _find_process_row(app.state.config_path, issue_key)
        if not row or not row.get("session_name"):
            raise HTTPException(status_code=404, detail=f"No local session for {issue_key}")
        body: dict[str, Any] = {}
        try:
            import asyncio
            body = asyncio.get_event_loop().run_in_executor(None, lambda: {})  # type: ignore
            body = {}
        except Exception:
            pass
        share = app.state.upterm.share(
            issue_key,
            row["session_name"],
            read_only=body.get("read_only", False),
        )
        return {"ok": True, **share.to_dict()}

    @app.post("/api/tickets/{issue_key}/unshare")
    def unshare_ticket(issue_key: str) -> dict[str, Any]:
        """Stop sharing a session."""
        stopped = app.state.upterm.unshare(issue_key)
        return {"ok": True, "stopped": stopped}

    @app.get("/api/tickets/{issue_key}/share")
    def get_share_info(issue_key: str) -> dict[str, Any]:
        """Get share status for a ticket."""
        share = app.state.upterm.get_share(issue_key)
        if not share:
            return {"shared": False}
        clients = app.state.upterm.get_client_count(issue_key)
        return {"shared": True, "clients": clients, **share.to_dict()}

    @app.get("/api/shares")
    def list_shares() -> dict[str, Any]:
        """List all active upterm shares."""
        shares = app.state.upterm.list_shares()
        return {"shares": [s.to_dict() for s in shares]}

    @app.get("/api/upterm/status")
    def upterm_status() -> dict[str, Any]:
        """Get upterm relay server configuration and status."""
        server = app.state.upterm_server
        is_public = "uptermd.upterm.dev" in server
        # Check if uptermd is running locally
        result = subprocess.run(["pgrep", "-f", "uptermd"], capture_output=True)
        local_running = result.returncode == 0
        pid = result.stdout.decode().strip().split("\n")[0] if local_running else None
        return {
            "server": server,
            "self_hosted": not is_public,
            "running": local_running,
            "pid": pid,
            "upterm_installed": app.state.upterm.available,
        }

    @app.get("/api/search")
    def search_tickets(q: str = "") -> dict[str, Any]:
        """Search for tickets by key or summary.

        Results sorted by relevance: active tmux > idle > stale > archived > no tmux.
        """
        query = q.strip()
        if not query:
            return {"results": []}
        config = load_config(app.state.config_path)
        store = StateStore(config.local_state_dir)
        reconcile_processes(store)

        query_lower = query.lower()
        running_rows = store.list_running_processes()
        archived_rows = store.list_archived_processes(limit=200)
        issue_states = store.list_issue_states()

        # Priority order for sorting
        MODE_PRIORITY = {"active": 0, "idle": 1, "stale": 2, "zombie": 3, "archived": 4, "none": 5}

        seen_keys: set[str] = set()
        results: list[dict[str, Any]] = []

        # Running processes
        for row in running_rows:
            key = row["issue_key"]
            if key in seen_keys:
                continue
            issue_info = store.get_issue_state(key)
            summary = (issue_info or {}).get("summary", "") if issue_info else ""
            if query_lower in key.lower() or query_lower in summary.lower():
                seen_keys.add(key)
                mode = classify_process_row(row)
                results.append({
                    "key": key,
                    "summary": summary,
                    "status_name": (issue_info or {}).get("status_name", row.get("status_name", "")),
                    "issue_type": (issue_info or {}).get("issue_type", ""),
                    "session_name": row.get("session_name"),
                    "local_mode": mode,
                    "source": "running",
                    "has_tmux": True,
                    "_priority": MODE_PRIORITY.get(mode, 3),
                })

        # Archived processes (had tmux at some point)
        for row in archived_rows:
            key = row["issue_key"]
            if key in seen_keys:
                continue
            issue_info = store.get_issue_state(key)
            summary = (issue_info or {}).get("summary", "") if issue_info else ""
            if query_lower in key.lower() or query_lower in summary.lower():
                seen_keys.add(key)
                results.append({
                    "key": key,
                    "summary": summary,
                    "status_name": (issue_info or {}).get("status_name", row.get("status_name", "")),
                    "issue_type": (issue_info or {}).get("issue_type", ""),
                    "session_name": None,
                    "local_mode": "archived",
                    "source": "archived",
                    "has_tmux": False,
                    "_priority": MODE_PRIORITY["archived"],
                })

        # Issue state (never had tmux) — shown last
        for issue in issue_states:
            key = issue["issue_key"]
            if key in seen_keys:
                continue
            summary = issue.get("summary", "")
            if query_lower in key.lower() or query_lower in summary.lower():
                seen_keys.add(key)
                results.append({
                    "key": key,
                    "summary": summary,
                    "status_name": issue.get("status_name", ""),
                    "issue_type": issue.get("issue_type", ""),
                    "session_name": None,
                    "local_mode": "none",
                    "source": "jira",
                    "has_tmux": False,
                    "_priority": MODE_PRIORITY["none"],
                })

        # Sort: active first, then idle, stale, archived, never-had-tmux last
        results.sort(key=lambda r: r["_priority"])
        for r in results:
            r.pop("_priority", None)

        return {"results": results[:25]}

    @app.get("/api/status")
    def status() -> dict[str, Any]:
        return get_status(app.state.config_path)

    @app.get("/api/dagster/status")
    def dagster_status() -> dict[str, Any]:
        active = dagster_is_active()
        sentinel = Path(__file__).resolve().parents[2] / "var" / "dagster" / "daemon_active"
        sentinel_age = None
        if sentinel.exists():
            try:
                sentinel_age = round(time.time() - sentinel.stat().st_mtime, 1)
            except OSError:
                pass
        pending_triggers = 0
        if TRIGGER_DIR.exists():
            pending_triggers = sum(1 for f in TRIGGER_DIR.iterdir() if f.is_file())
        return {
            "dagster_active": active,
            "sentinel_exists": sentinel.exists(),
            "sentinel_age_seconds": sentinel_age,
            "pending_triggers": pending_triggers,
        }

    @app.get("/api/health")
    def health_check() -> dict[str, Any]:
        return _build_health(app.state.config_path, app.state.upterm.available)

    @app.websocket("/ws/tickets/{issue_key}/terminal")
    async def terminal_socket(websocket: WebSocket, issue_key: str) -> None:
        await websocket.accept()
        row = _find_process_row(app.state.config_path, issue_key)
        if not row or not row.get("session_name"):
            await websocket.send_json({"type": "error", "message": f"No local tmux session for {issue_key}"})
            await websocket.close(code=4404)
            return
        session_name = row["session_name"]
        if not _tmux_session_exists(session_name):
            await websocket.send_json({"type": "error", "message": f"tmux session {session_name} is not running"})
            await websocket.close(code=4404)
            return
        await _mirror_tmux_terminal(websocket, session_name)

    @app.websocket("/ws/raw/tickets/{issue_key}/terminal")
    async def raw_terminal_socket(websocket: WebSocket, issue_key: str) -> None:
        await websocket.accept()
        row = _find_process_row(app.state.config_path, issue_key)
        if not row or not row.get("session_name"):
            await websocket.send_json({"type": "error", "message": f"No local tmux session for {issue_key}"})
            await websocket.close(code=4404)
            return
        session_name = row["session_name"]
        if not _tmux_session_exists(session_name):
            await websocket.send_json({"type": "error", "message": f"tmux session {session_name} is not running"})
            await websocket.close(code=4404)
            return
        await _attach_tmux_terminal(websocket, session_name)

    # --- Team ticket view ---

    @app.get("/api/team/tickets")
    def team_tickets() -> dict[str, Any]:
        """Return tickets that passed through any trigger column across all boards."""
        all_trigger_statuses: set[str] = set()
        board_paths = app.state.board_config_paths

        for bpath in board_paths:
            try:
                cfg = load_config(bpath)
                for route in cfg.routes:
                    all_trigger_statuses.add(route.status)
            except Exception:
                continue

        if not all_trigger_statuses:
            return {"tickets": [], "trigger_statuses": []}

        # Use the active board's Jira client for the query
        config = load_config(app.state.config_path)
        jira = JiraClient(config)
        try:
            issues = jira.search_issues_by_status_history(list(all_trigger_statuses))
        except Exception as exc:
            logger.warning("Team tickets query failed: %s", exc)
            return {"tickets": [], "error": str(exc)}

        # Build ticket list — skip per-ticket changelog (too slow for N tickets).
        # The "updated" timestamp from Jira serves as the "last moved" proxy.
        tickets: list[dict[str, Any]] = []
        for issue in issues:
            tickets.append({
                "key": issue.key,
                "summary": issue.summary,
                "assignee": issue.assignee,
                "status": issue.status_name,
                "issue_type": issue.issue_type,
                "updated": issue.updated,
                "browse_url": issue.browse_url,
            })

        return {
            "tickets": tickets,
            "trigger_statuses": sorted(all_trigger_statuses),
        }

    # --- Hub endpoints ---

    @app.post("/api/hub/start")
    def hub_start() -> dict[str, Any]:
        """Start the hub tmux session with upterm."""
        from .hub import start_hub, hub_status as get_hub_status
        config = load_config(app.state.config_path)
        settings = load_operator_settings(config.operator_settings_path)
        upterm_server = settings.upterm_server or app.state.upterm_server
        github_users = settings.hub_github_users or []
        try:
            result = start_hub(upterm_server=upterm_server, github_users=github_users)
            return {"ok": True, "github_users": github_users, **result}
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    @app.post("/api/hub/stop")
    def hub_stop() -> dict[str, Any]:
        """Stop the hub tmux session."""
        from .hub import stop_hub
        stopped = stop_hub()
        return {"ok": True, "stopped": stopped}

    @app.get("/api/hub/status")
    def hub_status_endpoint() -> dict[str, Any]:
        """Get hub connection info and DB summary."""
        from .hub import hub_status as get_hub_status
        return get_hub_status()

    @app.get("/api/hub/team")
    def hub_team() -> dict[str, Any]:
        """Read hub.sqlite directly for team checkin data."""
        from .hub import hub_team_data
        return hub_team_data()

    @app.get("/api/team/members")
    def get_team_members() -> dict[str, Any]:
        """Get the configured team GitHub usernames and hub connection."""
        config = load_config(app.state.config_path)
        settings = load_operator_settings(config.operator_settings_path)
        return {
            "github_users": settings.hub_github_users or [],
            "hub_dev_id": settings.hub_dev_id or "",
            "hub_ssh_connect": settings.hub_ssh_connect or "",
        }

    @app.post("/api/team/members")
    def save_team_members(body: dict[str, Any]) -> dict[str, Any]:
        """Save the team GitHub usernames list and hub config."""
        config = load_config(app.state.config_path)
        settings = load_operator_settings(config.operator_settings_path)
        if "github_users" in body:
            github_users = body["github_users"]
            github_users = [u.strip().lstrip("@").lower() for u in github_users if u.strip()]
            settings.hub_github_users = github_users
        if body.get("hub_dev_id") is not None:
            settings.hub_dev_id = body["hub_dev_id"].strip()
        if body.get("hub_ssh_connect") is not None:
            settings.hub_ssh_connect = body["hub_ssh_connect"].strip()
        save_operator_settings(config.operator_settings_path, settings)
        return {
            "ok": True,
            "github_users": settings.hub_github_users or [],
            "hub_dev_id": settings.hub_dev_id or "",
            "hub_ssh_connect": settings.hub_ssh_connect or "",
        }

    return app


def build_snapshot(config_path: str, controller: dict[str, Any] | None = None) -> dict[str, Any]:
    config = load_config(config_path)
    store = StateStore(config.local_state_dir)
    reconcile_processes(store)
    jira = JiraClient(config)
    # Query all display statuses (not just trigger statuses) — matches TUI behavior
    issues = jira.search_issues_by_statuses(_display_statuses(config))
    issue_rows = [asdict(issue) for issue in issues]

    running_rows = store.list_running_processes()
    running_by_issue = {row["issue_key"]: row for row in running_rows}
    board_rows, effective_statuses = _board_rows(config, store, issue_rows, running_by_issue)
    # Strip internal sort keys before sending to the frontend
    for row in board_rows:
        row.pop("_tmux_activity", None)
    columns = []
    for status in effective_statuses:
        tickets = [row for row in board_rows if row["status_name"] == status]
        columns.append(
            {
                "status": status,
                "count": len(tickets),
                "tickets": tickets,
            }
        )

    routes = [
        {
            "status": route.status,
            "enabled": route.enabled,
            "action": route.action,
            "prompt_template": route.prompt_template,
            "allowed_issue_types": route.allowed_issue_types,
            "transition_on_launch": route.transition_on_launch,
            "transition_on_success": route.transition_on_success,
            "transition_on_failure": route.transition_on_failure,
            "board_count": sum(1 for row in board_rows if row["status_name"] == route.status),
        }
        for route in config.routes
    ]

    active_count = sum(1 for row in running_rows if classify_process_row(row) == "active")
    settings = load_operator_settings(config.operator_settings_path)
    visible_total = len(board_rows)
    all_rows, _ = _board_rows(config, store, issue_rows, running_by_issue, limit=None)
    for row in all_rows:
        row.pop("_tmux_activity", None)
    capped_total = max(0, len(all_rows) - visible_total)

    # Include dagster sentinel info so the UI can compute countdowns
    sentinel = Path(__file__).resolve().parents[2] / "var" / "dagster" / "daemon_active"
    sentinel_age = None
    if sentinel.exists():
        try:
            sentinel_age = round(time.time() - sentinel.stat().st_mtime, 1)
        except OSError:
            pass

    # Add scratch sessions as a pseudo-column
    scratch_sessions = _list_scratch_sessions()
    scratch_column = None
    if scratch_sessions:
        scratch_column = {
            "status": "Scratch",
            "count": len(scratch_sessions),
            "sessions": scratch_sessions,
        }

    return {
        "config": {
            "site_url": config.site_url,
            "project_key": config.project_key,
            "board_id": config.board_id,
            "poll_interval_minutes": config.poll_interval_minutes,
            "watched_statuses": config.watched_statuses,
            "workdir": settings.claude_working_dir or config.llm.working_dir or "",
            "max_parallel": settings.claude_max_parallel or config.llm.max_parallel,
        },
        "controller": controller or {},
        "dagster": {
            "active": dagster_is_active(),
            "sentinel_age_seconds": sentinel_age,
        },
        "counts": {
            "live_rows": len(running_rows),
            "active_rows": active_count,
            "visible_rows": visible_total,
            "capped_not_shown": capped_total,
        },
        "columns": columns,
        "routes": routes,
        "scratch_column": scratch_column,
        "session_legend": {
            "active": "✹",
            "idle": "◌",
            "stale": "◇",
            "archived": "✦",
        },
    }


def manual_launch_issue(config_path: str, issue_key: str) -> dict[str, Any]:
    config = load_config(config_path)
    store = StateStore(config.local_state_dir)
    jira = JiraClient(config)
    issue = jira.fetch_issue(issue_key)
    if not issue:
        raise HTTPException(status_code=404, detail=f"{issue_key} not found in Jira")

    store.upsert_issue_state(issue, seen_at=utc_now())

    decision = _manual_route_decision(config, issue)
    if decision:
        _pre_launch_transition(config, jira, decision)
        launch = launch_decision(config, store, decision)
        _apply_launch_side_effects(config, store, jira, launch)
        return {"ok": True, "mode": "route", "launch": asdict(launch)}

    launch = launch_manual_tmux_shell(config, store, issue)
    return {"ok": True, "mode": "plain_tmux", "launch": asdict(launch)}


def _manual_route_decision(config: AppConfig, issue: JiraIssue) -> RouteDecision | None:
    route = next((item for item in config.routes if item.status == issue.status_name), None)
    if route is None:
        return None
    if route.allowed_issue_types and issue.issue_type not in route.allowed_issue_types:
        return None
    if not config.llm.enabled or config.llm.dry_run:
        return None

    prompt = route.prompt_template.format(
        issue_key=issue.key,
        summary=issue.summary,
        status=issue.status_name,
        issue_type=issue.issue_type,
    )
    artifact_globs = [
        pattern.format(
            issue_key=issue.key,
            summary=issue.summary,
            status=issue.status_name,
            issue_type=issue.issue_type,
            prompt=prompt,
        )
        for pattern in route.artifact_globs
    ]
    return RouteDecision(
        issue_key=issue.key,
        status_name=issue.status_name,
        action=route.action,
        prompt=prompt,
        should_launch=True,
        reason="manual_route_launch",
        transition_on_launch=route.transition_on_launch,
        transition_on_success=route.transition_on_success,
        transition_on_failure=route.transition_on_failure,
        comment_on_launch=route.comment_on_launch_template,
        comment_on_success=route.comment_on_success_template,
        comment_on_failure=route.comment_on_failure_template,
        artifact_globs=artifact_globs,
    )


def _tmux_session_activities() -> dict[str, float]:
    """Batch-fetch activity timestamps for all tmux sessions.

    Returns a dict mapping session name -> last activity unix timestamp.
    Uses a single ``tmux list-sessions`` call instead of per-session queries.
    """
    result = subprocess.run(
        ["tmux", "list-sessions", "-F", "#{session_name} #{session_activity}"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return {}
    activities: dict[str, float] = {}
    for line in result.stdout.strip().splitlines():
        parts = line.rsplit(" ", 1)
        if len(parts) == 2:
            try:
                activities[parts[0]] = float(parts[1])
            except ValueError:
                pass
    return activities


def _list_scratch_sessions() -> list[dict[str, Any]]:
    """List tmux sessions with the scratch- prefix and classify their activity."""
    result = subprocess.run(
        ["tmux", "list-sessions", "-F", "#{session_name} #{session_activity}"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return []
    now = time.time()
    sessions: list[dict[str, Any]] = []
    for line in result.stdout.strip().splitlines():
        parts = line.rsplit(" ", 1)
        if len(parts) != 2:
            continue
        name, activity_str = parts
        if not name.startswith("scratch-"):
            continue
        try:
            activity = float(activity_str)
        except ValueError:
            activity = now
        idle_seconds = now - activity
        if idle_seconds <= 300:
            mode = "active"
        elif idle_seconds <= 1800:
            mode = "idle"
        else:
            mode = "stale"
        sessions.append({
            "session_name": name,
            "activity": activity,
            "idle_seconds": round(idle_seconds),
            "mode": mode,
        })
    # Sort by activity descending (most recent first)
    sessions.sort(key=lambda s: s["activity"], reverse=True)
    return sessions


def _display_statuses(config: AppConfig) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for route in config.routes:
        for value in [
            route.status,
            route.transition_on_launch,
            route.transition_on_success,
            route.transition_on_failure,
        ]:
            if not value or value in seen:
                continue
            seen.add(value)
            ordered.append(value)
    if "Done" not in seen:
        ordered.append("Done")
    return ordered


def _board_rows(
    config: AppConfig,
    store: StateStore,
    issue_rows: list[dict[str, Any]],
    running_by_issue: dict[str, dict[str, Any]],
    limit: int | None = BOARD_ROW_LIMIT,
) -> tuple[list[dict[str, Any]], list[str]]:
    display_statuses = _display_statuses(config)
    trigger_statuses = {route.status for route in config.routes}

    # Pipeline statuses: any status that is a transition target from a route
    pipeline_statuses: set[str] = set()
    for route in config.routes:
        for val in [route.transition_on_launch, route.transition_on_success, route.transition_on_failure]:
            if val:
                pipeline_statuses.add(val)

    jira_keys = {row["key"] for row in issue_rows if row.get("key")}
    issue_map = {row["key"]: dict(row) for row in issue_rows if row.get("key")}
    _hydrate_issue_map_from_local_state(store, issue_map, set(running_by_issue))

    # Batch-fetch tmux session activity times (one shell-out for all sessions)
    session_activities = _tmux_session_activities()
    now_ts = time.time()
    stale_cutoff = config.stale_display_minutes * 60 if config.stale_display_minutes else None

    # For running tickets not in our Jira query, fetch their real status.
    # They might be in a status we don't display (like QA) — that's not a zombie.
    # They're only zombies if Jira can't find them at all (archived/deleted).
    missing_keys = {k for k in running_by_issue if k not in jira_keys}
    real_statuses: dict[str, str] = {}
    if missing_keys:
        try:
            jira = JiraClient(config)
            real_statuses = jira.fetch_issue_statuses(list(missing_keys))
        except Exception:
            pass  # If fetch fails, don't mark as zombie — be conservative

    rows: list[dict[str, Any]] = []
    for row in issue_map.values():
        process_row = running_by_issue.get(row["key"])
        key = row["key"]

        # For tickets not in our Jira query results:
        if key not in jira_keys:
            if key not in running_by_issue:
                continue  # No Jira record AND no live session — drop it
            real_status = real_statuses.get(key)
            if real_status:
                # Jira knows about it — update to real status, show normally
                row["status_name"] = real_status
                if real_status not in display_statuses:
                    display_statuses.append(real_status)  # Add so it gets a column
            else:
                # Jira can't find it — true zombie (archived/deleted)
                row["local_mode"] = "zombie"
                rows.append(_build_board_row(row, process_row, config))
                continue

        if row["status_name"] not in display_statuses:
            continue
        if row["status_name"] not in trigger_statuses and key not in running_by_issue:
            continue

        status = row["status_name"]
        is_other = status not in trigger_statuses and status not in pipeline_statuses

        # Stale filter: only applies to "other" columns (non-trigger, non-pipeline)
        if is_other and stale_cutoff and process_row:
            mode = classify_process_row(process_row)
            # Active sessions always show
            if mode != "active":
                session_name = process_row.get("session_name")
                activity_ts = session_activities.get(session_name) if session_name else None
                if activity_ts:
                    idle_seconds = now_ts - activity_ts
                    if idle_seconds > stale_cutoff:
                        continue  # Stale — hide from board
                else:
                    # No tmux activity data — fall back to updated_at from DB
                    updated_at = process_row.get("updated_at")
                    if updated_at:
                        try:
                            updated_dt = datetime.fromisoformat(updated_at)
                            idle_seconds = (datetime.now(UTC) - updated_dt).total_seconds()
                            if idle_seconds > stale_cutoff:
                                continue
                        except (ValueError, TypeError):
                            pass

        built = _build_board_row(row, process_row, config)
        # Attach tmux activity timestamp for sorting
        if process_row and process_row.get("session_name"):
            built["_tmux_activity"] = session_activities.get(process_row["session_name"])
        rows.append(built)

    rows = _sort_issue_rows(rows, display_statuses)
    if limit is not None:
        rows = rows[:limit]
    return rows, display_statuses


def _build_board_row(
    row: dict[str, Any],
    process_row: dict[str, Any] | None,
    config: AppConfig,
) -> dict[str, Any]:
    route = next((item for item in config.routes if item.status == row["status_name"]), None)
    if "local_mode" not in row:
        row["local_mode"] = classify_process_row(process_row) if process_row else "none"
    row["session_name"] = process_row.get("session_name") if process_row else None
    row["prompt"] = process_row.get("prompt") if process_row else (
        route.prompt_template.format(
            issue_key=row["key"],
            summary=row["summary"],
            status=row["status_name"],
            issue_type=row.get("issue_type", ""),
        )
        if route else None
    )
    row["latest_output"] = capture_session_output(process_row, lines=18).strip() if process_row else ""
    return row


def _find_process_row(config_path: str, issue_key: str) -> dict[str, Any] | None:
    config = load_config(config_path)
    store = StateStore(config.local_state_dir)
    reconcile_processes(store)
    rows = store.list_running_processes() + store.list_archived_processes(limit=100)
    return next((row for row in rows if row["issue_key"] == issue_key), None)


def _hydrate_issue_map_from_local_state(
    store: StateStore,
    issue_map: dict[str, dict[str, Any]],
    issue_keys: set[str],
) -> None:
    for key in issue_keys:
        if key in issue_map:
            continue
        row = store.get_issue_state(key)
        if not row:
            continue
        issue_map[key] = {
            "key": row["issue_key"],
            "summary": row["summary"],
            "issue_type": row["issue_type"],
            "status_name": row["status_name"],
            "status_id": row["status_id"],
            "updated": row["updated"],
            "assignee": row.get("assignee"),
            "browse_url": row["browse_url"],
            "parent_key": row.get("parent_key"),
            "parent_issue_type": row.get("parent_issue_type"),
            "parent_summary": row.get("parent_summary"),
            "epic_story_count": row.get("epic_story_count"),
            "labels": [value for value in (row.get("labels") or "").splitlines() if value],
        }


def _sort_issue_rows(rows: list[dict[str, Any]], statuses: list[str]) -> list[dict[str, Any]]:
    status_order = {status: index for index, status in enumerate(statuses)}

    def _recency_key(row: dict[str, Any]) -> float:
        """Return a recency score (higher = more recent).

        Prefers tmux session activity time if available, otherwise falls back
        to the Jira ``updated`` ISO timestamp.
        """
        tmux_ts = row.get("_tmux_activity")
        if tmux_ts:
            return float(tmux_ts)
        updated = row.get("updated", "")
        if updated:
            try:
                return datetime.fromisoformat(updated).timestamp()
            except (ValueError, TypeError):
                pass
        return 0.0

    # Primary: group by status column order
    # Secondary: within each group, most recent first
    rows = sorted(
        rows,
        key=lambda row: _recency_key(row),
        reverse=True,
    )
    return sorted(
        rows,
        key=lambda row: status_order.get(row.get("status_name"), len(status_order)),
    )


async def _mirror_tmux_terminal(websocket: WebSocket, session_name: str) -> None:
    async def pump_output() -> None:
        last_screen = None
        while True:
            pane_target = await asyncio.to_thread(_active_pane_target, session_name)
            if not pane_target:
                await websocket.send_json({"type": "error", "message": f"No active pane for {session_name}"})
                break
            # Capture visible pane with ANSI colors (no scrollback — that's Observer's job)
            raw = await asyncio.to_thread(_capture_visible_pane, pane_target, True)
            if not raw:
                raw = await asyncio.to_thread(_capture_visible_pane, pane_target, False)
            if not raw:
                raw = ""
            # Strip any reverse-video sequences (Claude's cursor and other TUI cursors)
            cleaned = re.sub(r"\033\[7m.\033\[(?:0|27)m", " ", raw)
            plain = _ANSI_RE.sub("", cleaned)
            # Cursor: try prompt detection first, fall back to tmux position
            cursor = _find_input_cursor(plain)
            if not cursor:
                cx, cy = await asyncio.to_thread(_get_cursor_position, pane_target)
                cursor = (cx, cy)
            screen_html = _ansi_to_html(cleaned)
            if cursor:
                screen_html = _insert_html_cursor(screen_html, plain, cursor[0], cursor[1])
            if raw != last_screen:
                last_screen = raw
                await websocket.send_json({"type": "snapshot", "screen": screen_html, "html": True})
            await asyncio.sleep(0.25)

    async def pump_input() -> None:
        while True:
            message = await websocket.receive_json()
            kind = message.get("type")
            if kind == "input":
                payload = message.get("data", "")
                if payload:
                    pane_target = await asyncio.to_thread(_active_pane_target, session_name)
                    if pane_target:
                        await asyncio.to_thread(_send_input_to_tmux, pane_target, payload)
            elif kind == "resize":
                # No-op: session windows are already managed by tmux and other clients.
                continue
            elif kind == "ping":
                await websocket.send_json({"type": "pong"})

    output_task = asyncio.create_task(pump_output())
    input_task = asyncio.create_task(pump_input())
    try:
        done, pending = await asyncio.wait(
            {output_task, input_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        for task in done:
            exc = task.exception()
            if exc and not isinstance(exc, WebSocketDisconnect):
                raise exc
    except WebSocketDisconnect:
        pass
    finally:
        for task in (output_task, input_task):
            if not task.done():
                task.cancel()


def _capture_tmux_target(target: str, lines: int = 0, alternate: bool = False) -> str:
    """Capture tmux pane content. Uses -S - -E - for full scrollback.
    If lines > 0, return only the last `lines` non-blank lines."""
    command = ["tmux", "capture-pane", "-p", "-J", "-S", "-", "-E", "-"]
    if alternate:
        command.append("-a")
    command.extend(["-t", target])
    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return ""
    return _trim_tmux_output(result.stdout, lines)


def _trim_tmux_output(screen: str, lines: int = 0) -> str:
    """Strip trailing blank lines. If lines > 0, keep only the last N rows."""
    rows = screen.splitlines()
    while rows and not rows[-1].strip():
        rows.pop()
    if not rows:
        return ""
    if lines > 0:
        rows = rows[-lines:]
    return "\n".join(rows) + "\n"


def _active_pane_target(session_name: str) -> str | None:
    result = subprocess.run(
        ["tmux", "display-message", "-p", "-t", session_name, "#{pane_id}"],
        check=False,
        capture_output=True,
        text=True,
    )
    target = result.stdout.strip()
    return target or None


def _get_pane_height(target: str) -> int:
    """Get the actual height of a tmux pane. Falls back to 50 if unavailable."""
    result = subprocess.run(
        ["tmux", "display-message", "-p", "-t", target, "#{pane_height}"],
        check=False,
        capture_output=True,
        text=True,
    )
    try:
        return int(result.stdout.strip())
    except (ValueError, AttributeError):
        return 50


def _get_cursor_position(target: str) -> tuple[int, int]:
    """Get cursor (x, y) from tmux. y=0 is top of visible pane."""
    result = subprocess.run(
        ["tmux", "display-message", "-p", "-t", target, "#{cursor_x}:#{cursor_y}"],
        check=False,
        capture_output=True,
        text=True,
    )
    try:
        parts = result.stdout.strip().split(":")
        return int(parts[0]), int(parts[1])
    except (ValueError, IndexError, AttributeError):
        return 0, 0


def _find_input_cursor(screen: str) -> tuple[int, int] | None:
    """Find where the user's input cursor likely is in captured screen output.
    For Claude Code: find the last ❯ prompt line and put cursor at end of text.
    For plain shells: find the last $ or % prompt and do the same.
    Returns None if no prompt found (caller should use tmux cursor position)."""
    lines = screen.split("\n")
    # Search backward for a prompt line
    for i in range(len(lines) - 1, -1, -1):
        stripped = lines[i].lstrip()
        # Claude prompt
        if stripped.startswith("❯"):
            text = lines[i].rstrip()
            return len(text), i
        # Shell prompts: "$ " at start, or "% " anywhere (zsh has hostname before %)
        if stripped.startswith("$ "):
            text = lines[i].rstrip()
            return len(text), i
        if " % " in lines[i] or lines[i].rstrip().endswith(" %"):
            text = lines[i].rstrip()
            return len(text), i
    return None


def _capture_visible_pane(target: str, alternate: bool = False) -> str:
    """Capture just the visible pane content with ANSI color codes.
    Cursor position maps directly to lines in this output."""
    command = ["tmux", "capture-pane", "-p", "-J", "-e"]
    if alternate:
        command.append("-a")
    command.extend(["-t", target])
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        return ""
    return result.stdout


def _capture_live_pane(target: str) -> str:
    """Capture pane with scrollback history + ANSI colors for live terminal.
    Tries alternate screen first (Claude uses it), falls back to normal.
    Strips trailing blank lines but keeps content for scroll history."""
    for flag in ["-e -a", "-e"]:
        command = ["tmux", "capture-pane", "-p", "-J", "-S", "-300", "-E", "-"]
        command.extend(flag.split())
        command.extend(["-t", target])
        result = subprocess.run(command, check=False, capture_output=True, text=True)
        if result.returncode == 0 and result.stdout.strip():
            # Strip trailing blank lines
            lines = result.stdout.split("\n")
            while lines and not lines[-1].strip():
                lines.pop()
            return "\n".join(lines) + "\n" if lines else ""
    return ""


_ANSI_RE = re.compile(r"\033\[([0-9;]*)m")
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _insert_html_cursor(html: str, plain: str, cx: int, cy: int) -> str:
    """Insert a cursor span into the HTML at the visual position (cx, cy) based on plain text."""
    plain_lines = plain.split("\n")
    if cy >= len(plain_lines):
        return html
    # Find the character offset in plain text for line cy, column cx
    char_at = plain_lines[cy][cx] if cx < len(plain_lines[cy]) else " "
    cursor_html = f'<span class="live-cursor">{char_at if char_at.strip() else " "}</span>'

    # Walk the HTML line-by-line to find the right insertion point
    html_lines = html.split("\n")
    if cy >= len(html_lines):
        return html
    line_html = html_lines[cy]
    # Count visible characters in the HTML line to find position cx
    visible = 0
    i = 0
    insert_pos = len(line_html)
    while i < len(line_html):
        if line_html[i] == "<":
            # Skip HTML tag
            end = line_html.find(">", i)
            if end == -1:
                break
            i = end + 1
            continue
        if line_html[i] == "&":
            # HTML entity — counts as 1 visible char
            end = line_html.find(";", i)
            if end == -1:
                break
            if visible == cx:
                # Replace this entity with cursor
                entity = line_html[i : end + 1]
                html_lines[cy] = line_html[:i] + cursor_html + line_html[end + 1 :]
                return "\n".join(html_lines)
            visible += 1
            i = end + 1
            continue
        if visible == cx:
            html_lines[cy] = line_html[:i] + cursor_html + line_html[i + 1 :]
            return "\n".join(html_lines)
        visible += 1
        i += 1
    # cx is past the end — append cursor
    html_lines[cy] = line_html + cursor_html
    return "\n".join(html_lines)


def _ansi_to_html(text: str) -> str:
    """Convert ANSI escape sequences to HTML spans with inline color styles."""
    parts = _ANSI_RE.split(text)
    html: list[str] = []
    span_open = False
    i = 0
    while i < len(parts):
        if i % 2 == 0:
            # Plain text — HTML-escape it
            chunk = parts[i].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            html.append(chunk)
        else:
            # ANSI code
            code = parts[i]
            if span_open:
                html.append("</span>")
                span_open = False
            if code in ("0", "39", ""):
                pass  # reset
            elif code == "1":
                html.append('<span style="font-weight:bold">')
                span_open = True
            elif code == "2":
                html.append('<span style="opacity:0.6">')
                span_open = True
            elif code == "7":
                html.append('<span style="background:var(--text);color:var(--bg)">')
                span_open = True
            elif code.startswith("38;2;"):
                # 24-bit RGB foreground
                rgb = code.split(";")[2:]
                if len(rgb) == 3:
                    html.append(f'<span style="color:rgb({rgb[0]},{rgb[1]},{rgb[2]})">')
                    span_open = True
            elif code.startswith("48;2;"):
                # 24-bit RGB background
                rgb = code.split(";")[2:]
                if len(rgb) == 3:
                    html.append(f'<span style="background:rgb({rgb[0]},{rgb[1]},{rgb[2]})">')
                    span_open = True
            elif code.startswith("38;5;"):
                # 256-color — pass through as a class
                html.append(f'<span class="ansi-fg-{code.split(";")[2]}">')
                span_open = True
            elif len(code) == 2 and code.startswith("3") and code[1].isdigit():
                # Basic 8-color foreground (30-37)
                colors = ["#000", "#c00", "#0a0", "#ca0", "#00c", "#c0c", "#0cc", "#ccc"]
                idx = int(code[1])
                if idx < len(colors):
                    html.append(f'<span style="color:{colors[idx]}">')
                    span_open = True
        i += 1
    if span_open:
        html.append("</span>")
    # Ensure blank lines render with height in the browser
    result = "".join(html)
    result = re.sub(r"(?m)^$", " ", result)
    return result


def _send_input_to_tmux(target: str, payload: str) -> None:
    index = 0
    while index < len(payload):
        if payload.startswith("\x1b[A", index):
            subprocess.run(["tmux", "send-keys", "-t", target, "Up"], check=False)
            index += 3
            continue
        if payload.startswith("\x1b[B", index):
            subprocess.run(["tmux", "send-keys", "-t", target, "Down"], check=False)
            index += 3
            continue
        if payload.startswith("\x1b[C", index):
            subprocess.run(["tmux", "send-keys", "-t", target, "Right"], check=False)
            index += 3
            continue
        if payload.startswith("\x1b[D", index):
            subprocess.run(["tmux", "send-keys", "-t", target, "Left"], check=False)
            index += 3
            continue
        char = payload[index]
        if char in {"\r", "\n"}:
            subprocess.run(["tmux", "send-keys", "-t", target, "C-m"], check=False)
        elif char == "\x7f":
            subprocess.run(["tmux", "send-keys", "-t", target, "BSpace"], check=False)
        elif char == "\t":
            subprocess.run(["tmux", "send-keys", "-t", target, "Tab"], check=False)
        elif char == "\x03":
            subprocess.run(["tmux", "send-keys", "-t", target, "C-c"], check=False)
        elif char == "\x1b":
            subprocess.run(["tmux", "send-keys", "-t", target, "Escape"], check=False)
        else:
            subprocess.run(["tmux", "send-keys", "-l", "-t", target, char], check=False)
        index += 1


async def _attach_tmux_terminal(websocket: WebSocket, session_name: str) -> None:
    import fcntl
    import os
    import pty
    import termios
    import struct

    master_fd, slave_fd = pty.openpty()
    process = subprocess.Popen(
        ["tmux", "attach-session", "-f", "ignore-size", "-t", session_name],
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        start_new_session=True,
        close_fds=True,
        text=False,
    )
    os.close(slave_fd)

    def _set_size(cols: int, rows: int) -> None:
        if cols <= 0 or rows <= 0:
            return
        size = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(master_fd, termios.TIOCSWINSZ, size)

    async def pump_output() -> None:
        while True:
            chunk = await asyncio.to_thread(os.read, master_fd, 4096)
            if not chunk:
                break
            await websocket.send_bytes(chunk)

    async def pump_input() -> None:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                raise WebSocketDisconnect
            if message.get("bytes"):
                await asyncio.to_thread(os.write, master_fd, message["bytes"])
                continue
            if message.get("text"):
                try:
                    payload = yaml.safe_load(message["text"])
                except Exception:
                    payload = None
                if isinstance(payload, dict) and payload.get("type") == "resize":
                    _set_size(int(payload.get("cols", 0)), int(payload.get("rows", 0)))
                elif isinstance(payload, dict) and payload.get("type") == "input":
                    data = payload.get("data", "")
                    if data:
                        await asyncio.to_thread(os.write, master_fd, data.encode())

    output_task = asyncio.create_task(pump_output())
    input_task = asyncio.create_task(pump_input())
    try:
        done, pending = await asyncio.wait({output_task, input_task}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in done:
            exc = task.exception()
            if exc and not isinstance(exc, WebSocketDisconnect):
                raise exc
    except WebSocketDisconnect:
        pass
    finally:
        for task in (output_task, input_task):
            if not task.done():
                task.cancel()
        try:
            os.close(master_fd)
        except OSError:
            pass
        if process.poll() is None:
            process.terminate()
            try:
                await asyncio.to_thread(process.wait, 1.0)
            except Exception:
                process.kill()
