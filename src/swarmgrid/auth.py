from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import subprocess

try:
    import keyring
except ImportError:  # pragma: no cover - fallback path
    keyring = None

from .config import AppConfig
from .operator_settings import load_operator_settings


KEYCHAIN_SERVICE = "swarmgrid-jira-token"


@dataclass(slots=True)
class AuthState:
    email: str | None
    token: str | None
    token_source: str

    @property
    def token_preview(self) -> str:
        if not self.token:
            return "(missing)"
        if len(self.token) <= 8:
            return "*" * len(self.token)
        return f"{self.token[:4]}...{self.token[-4:]}"


def resolve_auth_state(config: AppConfig) -> AuthState:
    settings = load_operator_settings(config.operator_settings_path)
    email = os.environ.get(config.jira.email_env) or settings.jira_email
    env_token = os.environ.get(config.jira.token_env)
    if env_token:
        return AuthState(email=email, token=env_token.strip(), token_source="env")

    if email:
        keychain_token = load_token_from_keychain(email)
        if keychain_token:
            return AuthState(email=email, token=keychain_token, token_source="keychain")

    token_path = Path(settings.token_file or config.jira.token_file).expanduser()
    if token_path.exists():
        token = token_path.read_text(encoding="utf-8").strip()
        if token:
            return AuthState(email=email, token=token, token_source=str(token_path))

    return AuthState(email=email, token=None, token_source="missing")


def load_token_from_keychain(email: str) -> str | None:
    if keyring is not None:
        value = keyring.get_password(KEYCHAIN_SERVICE, email)
        if value:
            return value.strip()

    security_bin = "/usr/bin/security"
    if not Path(security_bin).exists():
        return None
    result = subprocess.run(
        [security_bin, "find-generic-password", "-a", email, "-s", KEYCHAIN_SERVICE, "-w"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def save_token_to_keychain(email: str, token: str) -> None:
    if keyring is not None:
        keyring.set_password(KEYCHAIN_SERVICE, email, token)
        return

    security_bin = "/usr/bin/security"
    if not Path(security_bin).exists():
        raise RuntimeError("macOS keychain support is not available on this machine.")
    subprocess.run(
        [security_bin, "add-generic-password", "-a", email, "-s", KEYCHAIN_SERVICE, "-w", token, "-U"],
        check=True,
        capture_output=True,
        text=True,
    )
