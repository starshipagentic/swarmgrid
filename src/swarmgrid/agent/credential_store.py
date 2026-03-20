"""Credential store — read/write secrets from macOS keychain or file fallback.

Uses the ``keyring`` library when available (macOS Keychain, GNOME Keyring,
Windows Credential Locker).  Falls back to ``~/.swarmgrid/credentials`` as
a JSON file when keyring is unavailable.
"""
from __future__ import annotations

import json
import logging
import os
import stat
from pathlib import Path

logger = logging.getLogger(__name__)

SERVICE_NAME = "swarmgrid"
FALLBACK_DIR = Path.home() / ".swarmgrid"
FALLBACK_PATH = FALLBACK_DIR / "credentials"

# Credential keys
JIRA_TOKEN_KEY = "jira_api_token"
JIRA_EMAIL_KEY = "jira_email"
CLAUDE_API_KEY = "claude_api_key"
CLOUD_API_KEY = "cloud_api_key"


def _keyring_available() -> bool:
    try:
        import keyring
        # Smoke-test: some backends raise on first call
        keyring.get_credential(SERVICE_NAME, "probe")
        return True
    except Exception:
        return False


def _read_fallback() -> dict[str, str]:
    if not FALLBACK_PATH.exists():
        return {}
    try:
        return json.loads(FALLBACK_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _write_fallback(data: dict[str, str]) -> None:
    FALLBACK_DIR.mkdir(parents=True, exist_ok=True)
    FALLBACK_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    # Restrict permissions: owner-only read/write
    os.chmod(FALLBACK_PATH, stat.S_IRUSR | stat.S_IWUSR)


def get_credential(key: str) -> str | None:
    """Retrieve a credential by key. Returns None if not stored."""
    if _keyring_available():
        import keyring
        return keyring.get_password(SERVICE_NAME, key)
    return _read_fallback().get(key)


def set_credential(key: str, value: str) -> None:
    """Store a credential."""
    if _keyring_available():
        import keyring
        keyring.set_password(SERVICE_NAME, key, value)
        logger.info("Stored %s in system keychain", key)
        return
    data = _read_fallback()
    data[key] = value
    _write_fallback(data)
    logger.info("Stored %s in %s", key, FALLBACK_PATH)


def delete_credential(key: str) -> bool:
    """Delete a credential. Returns True if it existed."""
    if _keyring_available():
        import keyring
        try:
            keyring.delete_password(SERVICE_NAME, key)
            return True
        except keyring.errors.PasswordDeleteError:
            return False
    data = _read_fallback()
    if key in data:
        del data[key]
        _write_fallback(data)
        return True
    return False


def get_all_credentials() -> dict[str, str]:
    """Return all stored credentials (key -> value). For sync purposes."""
    if _keyring_available():
        import keyring
        result = {}
        for key in [JIRA_TOKEN_KEY, JIRA_EMAIL_KEY, CLAUDE_API_KEY, CLOUD_API_KEY]:
            val = keyring.get_password(SERVICE_NAME, key)
            if val:
                result[key] = val
        return result
    return _read_fallback()
