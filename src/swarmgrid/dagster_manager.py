from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, UTC
from pathlib import Path
import os
import shutil
import signal
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen

from .config import AppConfig, resolve_jira_auth
from .operator_settings import load_operator_settings
from .state import pid_is_alive


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(slots=True)
class DagsterStatus:
    running: bool
    pid: int | None
    log_path: str
    url: str


def _pid_file(config: AppConfig) -> Path:
    return config.local_state_dir / "dagster.pid"


def _log_file(config: AppConfig) -> Path:
    return config.local_state_dir / "dagster.log"


def _dagster_home(config: AppConfig) -> Path:
    return config.local_state_dir / "dagster_home"


def get_dagster_status(config: AppConfig) -> DagsterStatus:
    pid_path = _pid_file(config)
    pid: int | None = None
    if pid_path.exists():
        try:
            pid = int(pid_path.read_text(encoding="utf-8").strip())
        except ValueError:
            pid = None
    running = pid is not None and pid_is_alive(pid)
    if pid is not None and not running and pid_path.exists():
        pid_path.unlink(missing_ok=True)
        pid = None
    return DagsterStatus(
        running=running,
        pid=pid,
        log_path=str(_log_file(config)),
        url="http://127.0.0.1:3000",
    )


def start_dagster(config: AppConfig) -> DagsterStatus:
    status = get_dagster_status(config)
    if status.running:
        return status

    dagster_bin = shutil.which("dagster")
    if not dagster_bin:
        raise RuntimeError("Dagster CLI is not installed in the current environment.")

    email, _token = resolve_jira_auth(config)
    env = os.environ.copy()
    env.setdefault("ATLASSIAN_EMAIL", email)
    env["TRAV_JIRA_HEARTBEAT_CONFIG"] = str(config.config_path)
    dagster_home = _dagster_home(config)
    dagster_home.mkdir(parents=True, exist_ok=True)
    env["DAGSTER_HOME"] = str(dagster_home)

    config.local_state_dir.mkdir(parents=True, exist_ok=True)
    log_path = _log_file(config)
    with log_path.open("a", encoding="utf-8") as log_handle:
        process = subprocess.Popen(
            [
                dagster_bin,
                "dev",
                "-m",
                "swarmgrid.definitions",
                "-p",
                "3000",
            ],
            cwd=str(config.config_path.parent),
            env=env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            text=True,
        )
    _pid_file(config).write_text(str(process.pid), encoding="utf-8")
    _wait_for_http(status_url="http://127.0.0.1:3000/server_info")
    return get_dagster_status(config)


def stop_dagster(config: AppConfig) -> DagsterStatus:
    status = get_dagster_status(config)
    if not status.running or status.pid is None:
        return status
    os.killpg(status.pid, signal.SIGTERM)
    _pid_file(config).unlink(missing_ok=True)
    return get_dagster_status(config)


def _wait_for_http(status_url: str, timeout_seconds: float = 20.0) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            with urlopen(status_url, timeout=1.5) as response:
                if 200 <= response.status < 500:
                    return
        except URLError:
            time.sleep(0.5)
            continue
        except Exception:
            time.sleep(0.5)
            continue
    raise RuntimeError(f"Dagster did not become ready within {timeout_seconds:.0f}s.")
