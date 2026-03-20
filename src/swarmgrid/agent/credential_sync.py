"""Edge-to-edge credential transfer via SSH.

When a second machine needs a credential (e.g. Jira token), this module
handles the transfer through the existing upterm SSH tunnel so the cloud
never sees plaintext secrets.
"""
from __future__ import annotations

import json
import logging
import subprocess

from .credential_store import get_credential, set_credential

logger = logging.getLogger(__name__)


def send_credential(ssh_connect: str, key: str) -> dict:
    """Send a locally-stored credential to a remote edge node via SSH.

    The remote worker must handle the ``credential_receive`` command.
    Returns the remote response dict.
    """
    value = get_credential(key)
    if not value:
        return {"ok": False, "error": f"credential {key!r} not found locally"}

    payload = json.dumps({
        "cmd": "credential_receive",
        "key": key,
        "value": value,
    })

    parts = ssh_connect.strip().split()
    if parts and parts[0] == "ssh":
        parts = parts[1:]

    try:
        result = subprocess.run(
            ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10", *parts],
            input=payload,
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            return {"ok": False, "error": f"ssh exit {result.returncode}: {result.stderr[:200]}"}
        if result.stdout.strip():
            return json.loads(result.stdout)
        return {"ok": False, "error": "empty response"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "ssh timeout (15s)"}
    except json.JSONDecodeError:
        return {"ok": False, "error": f"invalid JSON: {result.stdout[:200]}"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def receive_credential(key: str, value: str) -> dict:
    """Store a credential received from a remote edge node."""
    set_credential(key, value)
    logger.info("Received and stored credential: %s", key)
    return {"ok": True, "stored": key}
