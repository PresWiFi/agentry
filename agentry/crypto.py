"""Encrypts stored secrets (API keys, tokens) with a key derived from the app secret."""
from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from .config import settings

_fernet: Fernet | None = None


def _f() -> Fernet:
    global _fernet
    if _fernet is None:
        raw = hashlib.sha256(("agentry-secrets:" + settings.resolve_secret()).encode()).digest()
        _fernet = Fernet(base64.urlsafe_b64encode(raw))
    return _fernet


def encrypt(value: str) -> str:
    return _f().encrypt(value.encode()).decode() if value else ""


def decrypt(value: str) -> str:
    if not value:
        return ""
    try:
        return _f().decrypt(value.encode()).decode()
    except InvalidToken:
        raise RuntimeError("A stored secret can't be decrypted. Was AGENTRY_SECRET_KEY changed?")
