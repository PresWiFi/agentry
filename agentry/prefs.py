"""Settings managed in the UI (Settings page), stored in SQLite. Secrets are encrypted.

On first start, values found in the matching environment variables are imported once; after that the
database is the source of truth.
"""
from __future__ import annotations

import json
import logging
import os
import threading

from . import crypto, db

log = logging.getLogger("agentry.prefs")

# key: (default, env var, kind)   kind: str | bool | list | secret
SPEC: dict[str, tuple] = {
    "public_url": ("", "AGENTRY_PUBLIC_URL", "str"),
    "trusted_proxies": ("127.0.0.1", "AGENTRY_TRUSTED_PROXIES", "str"),
    "local_login": (True, "AGENTRY_LOCAL_LOGIN", "bool"),
    "oidc_issuer": ("", "OIDC_ISSUER", "str"),
    "oidc_client_id": ("", "OIDC_CLIENT_ID", "str"),
    "oidc_client_secret": ("", "OIDC_CLIENT_SECRET", "secret"),
    "oidc_scopes": ("openid email profile", "OIDC_SCOPES", "str"),
    "oidc_label": ("Sign in with SSO", "OIDC_LABEL", "str"),
    "oidc_groups_claim": ("groups", "OIDC_GROUPS_CLAIM", "str"),
    "oidc_admin_groups": ([], "OIDC_ADMIN_GROUPS", "list"),
    "oidc_reviewer_groups": ([], "OIDC_REVIEWER_GROUPS", "list"),
    "oidc_viewer_groups": ([], "OIDC_VIEWER_GROUPS", "list"),
    "oidc_default_role": ("none", "OIDC_DEFAULT_ROLE", "str"),
    "oidc_allowed_domains": ([], "OIDC_ALLOWED_DOMAINS", "list"),
}
ROLES = ("none", "viewer", "reviewer", "admin")

_cache: dict | None = None
_lock = threading.Lock()
version = 0  # bumps on every write; consumers rebuild clients when it changes


def _coerce(key: str, value):
    kind = SPEC[key][2]
    if kind == "bool":
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    if kind == "list":
        if isinstance(value, str):
            value = value.split(",")
        out = [str(v).strip() for v in (value or []) if str(v).strip()]
        return [v.lower() for v in out] if key == "oidc_allowed_domains" else out
    value = "" if value is None else str(value).strip()
    if key in ("public_url", "oidc_issuer"):
        value = value.rstrip("/")
    return value


def _load() -> dict:
    out = {k: v[0] for k, v in SPEC.items()}
    for row in db.q("SELECT key, value FROM prefs"):
        if row["key"] not in SPEC:
            continue
        v = json.loads(row["value"])
        out[row["key"]] = crypto.decrypt(v) if SPEC[row["key"]][2] == "secret" else v
    return out


def all() -> dict:
    global _cache
    with _lock:
        if _cache is None:
            _cache = _load()
        return dict(_cache)


def get(key: str):
    return all()[key]


def update(values: dict) -> None:
    global _cache, version
    for key, value in values.items():
        if key not in SPEC:
            raise KeyError(key)
        value = _coerce(key, value)
        stored = crypto.encrypt(value) if SPEC[key][2] == "secret" else value
        db.ex("INSERT INTO prefs(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
              (key, json.dumps(stored)))
    with _lock:
        _cache = None
        version += 1


def import_env() -> list[str]:
    """Copies env values into the database for keys never set there. Returns the keys imported."""
    have = {r["key"] for r in db.q("SELECT key FROM prefs")}
    found = {k: os.environ[spec[1]] for k, spec in SPEC.items()
             if k not in have and os.environ.get(spec[1], "").strip() != ""}
    if found:
        update(found)
        log.info("Imported from environment: %s. Manage these on the Settings page from now on.",
                 ", ".join(SPEC[k][1] for k in found))
    return list(found)


def oidc_enabled() -> bool:
    p = all()
    return bool(p["oidc_issuer"] and p["oidc_client_id"])


def groups_mapped() -> bool:
    p = all()
    return bool(p["oidc_admin_groups"] or p["oidc_reviewer_groups"] or p["oidc_viewer_groups"])


def public(include_secret_flags: bool = True) -> dict:
    """For the Settings page: secrets replaced by a set/not-set flag."""
    p = all()
    out = {k: v for k, v in p.items() if SPEC[k][2] != "secret"}
    if include_secret_flags:
        out["oidc_client_secret_set"] = bool(p["oidc_client_secret"])
    return out
