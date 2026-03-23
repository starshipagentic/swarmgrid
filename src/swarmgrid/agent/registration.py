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


def register_edge(ssh_connect: str, frontdesk_connect: str | None = None) -> dict:
    """POST registration to the cloud API.

    Body: {"ssh_connect": "ssh abc@relay", "frontdesk_connect": "ssh ...", "hostname": "...", "os": "..."}
    ssh_connect is the phonebook agent (cloud-facing).
    frontdesk_connect is the front desk agent (team-facing).
    Returns the parsed JSON response or an error dict.
    """
    api_key = _api_key()
    if not api_key:
        return {"ok": False, "error": "no cloud API key configured"}

    base_url = _cloud_base_url()
    url = f"{base_url}/api/edge/register"

    payload: dict = {
        "ssh_connect": ssh_connect,
        "hostname": platform.node(),
        "os": platform.system(),
    }
    if frontdesk_connect:
        payload["frontdesk_connect"] = frontdesk_connect

    body = json.dumps(payload).encode()

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


def fetch_authorized_keys() -> dict:
    """Fetch authorized_keys and github_users from the cloud.

    Returns {"authorized_keys": [...], "github_users": [...]} on success.
    Returns {"authorized_keys": [], "github_users": []} on any failure
    so the agent can fall back to --accept.
    """
    empty: dict = {"authorized_keys": [], "github_users": []}

    api_key = _api_key()
    if not api_key:
        logger.warning("No API key — cannot fetch authorized_keys from cloud")
        return empty

    base_url = _cloud_base_url()
    url = f"{base_url}/api/edge/authorized-keys"

    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
        },
        method="GET",
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            return {
                "authorized_keys": data.get("authorized_keys", []),
                "github_users": data.get("github_users", []),
            }
    except urllib.error.HTTPError as exc:
        error_body = ""
        try:
            error_body = exc.read().decode()[:200]
        except Exception:
            pass
        logger.warning("Failed to fetch authorized_keys: HTTP %s: %s", exc.code, error_body)
        return empty
    except urllib.error.URLError as exc:
        logger.warning("Failed to fetch authorized_keys: connection failed: %s", exc.reason)
        return empty
    except Exception as exc:
        logger.warning("Failed to fetch authorized_keys: %s", exc)
        return empty


def fetch_team_config() -> dict:
    """Fetch team configuration from the cloud.

    Returns board-to-github-user mappings so the front desk agent knows
    which github users to allow.

    Returns {"boards": {"LMSV3": {"board_id": 1, "github_users": ["starshipagentic", ...]}, ...}}
    on success, or {"boards": {}} on failure.

    Caches to ~/.swarmgrid/team_config.json on success so the agent can
    start even when the cloud is unreachable.
    """
    cache_path = Path.home() / ".swarmgrid" / "team_config.json"
    empty: dict = {"boards": {}}

    api_key = _api_key()
    if not api_key:
        logger.warning("No API key — cannot fetch team config from cloud")
        return _load_cached_team_config(cache_path, empty)

    base_url = _cloud_base_url()
    url = f"{base_url}/api/edge/team-config"

    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
        },
        method="GET",
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            # Cache for offline starts
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(data, indent=2))
            logger.info("Fetched team config: %d board(s)", len(data.get("boards", {})))
            return data
    except urllib.error.HTTPError as exc:
        error_body = ""
        try:
            error_body = exc.read().decode()[:200]
        except Exception:
            pass
        logger.warning("Failed to fetch team config: HTTP %s: %s", exc.code, error_body)
        return _load_cached_team_config(cache_path, empty)
    except urllib.error.URLError as exc:
        logger.warning("Failed to fetch team config: connection failed: %s", exc.reason)
        return _load_cached_team_config(cache_path, empty)
    except Exception as exc:
        logger.warning("Failed to fetch team config: %s", exc)
        return _load_cached_team_config(cache_path, empty)


def _load_cached_team_config(cache_path: Path, default: dict) -> dict:
    """Load cached team config from disk, or return default."""
    if cache_path.exists():
        try:
            data = json.loads(cache_path.read_text())
            logger.info("Using cached team config from %s", cache_path)
            return data
        except Exception:
            pass
    return default


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
