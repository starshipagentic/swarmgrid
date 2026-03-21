"""Edge node registration with the SwarmGrid cloud.

On startup the edge daemon registers its SSH connect string with the cloud
so the cloud can dispatch commands via SSH.  Re-registers whenever the
upterm token rotates (e.g. on restart).
"""
from __future__ import annotations

import json
import logging
import platform
import urllib.request
import urllib.error
from pathlib import Path

from .credential_store import get_credential, CLOUD_API_KEY

logger = logging.getLogger(__name__)

DEFAULT_CLOUD_URL = "https://swarmgrid-api.fly.dev"
CONFIG_PATH = Path.home() / ".swarmgrid" / "config.yaml"


def _cloud_base_url() -> str:
    """Read cloud URL from config or use default."""
    if CONFIG_PATH.exists():
        try:
            import yaml
            data = yaml.safe_load(CONFIG_PATH.read_text())
            if data and data.get("cloud_url"):
                return data["cloud_url"].rstrip("/")
        except Exception:
            pass
    return DEFAULT_CLOUD_URL


def _api_key() -> str | None:
    """Get the cloud API key from credential store or config."""
    key = get_credential(CLOUD_API_KEY)
    if key:
        return key
    if CONFIG_PATH.exists():
        try:
            import yaml
            data = yaml.safe_load(CONFIG_PATH.read_text())
            if data:
                return data.get("api_key")
        except Exception:
            pass
    return None


def register_edge(ssh_connect: str) -> dict:
    """POST registration to the cloud API.

    Body: {"ssh_connect": "ssh abc@relay", "hostname": "...", "os": "..."}
    Returns the parsed JSON response or an error dict.
    """
    api_key = _api_key()
    if not api_key:
        return {"ok": False, "error": "no cloud API key configured"}

    base_url = _cloud_base_url()
    url = f"{base_url}/api/edge/register"

    body = json.dumps({
        "ssh_connect": ssh_connect,
        "hostname": platform.node(),
        "os": platform.system(),
    }).encode()

    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        error_body = ""
        try:
            error_body = exc.read().decode()[:200]
        except Exception:
            pass
        return {"ok": False, "error": f"HTTP {exc.code}: {error_body}"}
    except urllib.error.URLError as exc:
        return {"ok": False, "error": f"connection failed: {exc.reason}"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def report_heartbeat(board_id: int, tickets_found: list, sessions_launched: list) -> dict:
    """POST heartbeat results to the cloud."""
    api_key = _api_key()
    if not api_key:
        return {"ok": False, "error": "no cloud API key configured"}

    base_url = _cloud_base_url()
    url = f"{base_url}/api/edge/heartbeat"

    body = json.dumps({
        "board_id": board_id,
        "tickets_found": tickets_found,
        "sessions_launched": sessions_launched,
    }).encode()

    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def report_offline() -> dict:
    """Notify the cloud that this edge node is shutting down."""
    api_key = _api_key()
    if not api_key:
        return {"ok": False, "error": "no cloud API key configured"}

    base_url = _cloud_base_url()
    url = f"{base_url}/api/edge/offline"

    req = urllib.request.Request(
        url,
        data=b"{}",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
