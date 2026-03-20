"""Credential encryption for SwarmGrid cloud.

Encrypts Jira tokens and other secrets at rest using Fernet (AES-128-CBC).
The encryption key is derived from JWT_SECRET — so the cloud CAN decrypt
(needed while cloud fetches Jira data directly, before edge nodes take over).

When edge nodes handle all Jira polling, this module will be replaced with
true zero-knowledge encryption where the cloud CANNOT decrypt.
"""
from __future__ import annotations

import base64
import hashlib
import os

from cryptography.fernet import Fernet


def _derive_key() -> bytes:
    """Derive a Fernet key from JWT_SECRET."""
    secret = os.environ.get("JWT_SECRET", "swarmgrid-dev-secret-change-me")
    # Fernet needs a 32-byte URL-safe base64 key
    raw = hashlib.sha256(secret.encode()).digest()
    return base64.urlsafe_b64encode(raw)


def encrypt(plaintext: str) -> str:
    """Encrypt a string. Returns base64-encoded ciphertext."""
    if not plaintext:
        return ""
    f = Fernet(_derive_key())
    return f.encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    """Decrypt a string. Returns plaintext."""
    if not ciphertext:
        return ""
    try:
        f = Fernet(_derive_key())
        return f.decrypt(ciphertext.encode()).decode()
    except Exception:
        # If decryption fails (old/corrupted data), return empty
        return ""
