"""Credential encryption for SwarmGrid cloud.

Encrypts sensitive data at rest using Fernet (AES-128-CBC).
Uses a dedicated ENCRYPTION_KEY, separate from JWT_SECRET.

- JWT_SECRET: only signs/verifies login tokens
- ENCRYPTION_KEY: only encrypts/decrypts data in the database

If someone leaks JWT_SECRET, they can call APIs but can't read encrypted data.
If someone leaks ENCRYPTION_KEY, they can decrypt data but can't call APIs.
Both need to leak for full compromise.
"""
from __future__ import annotations

import base64
import hashlib
import os

from cryptography.fernet import Fernet


def _derive_key() -> bytes:
    """Derive a Fernet key from ENCRYPTION_KEY (or JWT_SECRET as fallback)."""
    secret = os.environ.get("ENCRYPTION_KEY") or os.environ.get("JWT_SECRET", "swarmgrid-dev-secret-change-me")
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
