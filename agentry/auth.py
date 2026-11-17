"""Sign-in: OpenID Connect (any provider) and a local admin account. Roles: admin > reviewer > viewer.

With OIDC_*_GROUPS set, the role comes from the groups claim at every sign-in. Without group mapping,
new SSO users get OIDC_DEFAULT_ROLE and an admin can change it on the Users page.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time

from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import Depends, HTTPException, Request

from . import db
from .config import settings

ROLES = {"none": 0, "viewer": 1, "reviewer": 2, "admin": 3}

oauth = OAuth()
if settings.oidc_enabled:
    oauth.register("oidc", client_id=settings.oidc_client_id, client_secret=settings.oidc_client_secret or None,
                   server_metadata_url=settings.oidc_issuer + "/.well-known/openid-configuration",
                   client_kwargs={"scope": settings.oidc_scopes, "code_challenge_method": "S256"})


# ---------- passwords ----------

def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 14, r=8, p=1)
    return f"scrypt${salt.hex()}${h.hex()}"


def check_password(pw: str, stored: str | None) -> bool:
    try:
        _, salt, h = (stored or "").split("$")
    except ValueError:
        return False
    calc = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=2 ** 14, r=8, p=1)
    return hmac.compare_digest(calc.hex(), h)


def ensure_local_admin() -> None:
    """Creates or updates the local 'admin' account from AGENTRY_ADMIN_PASSWORD."""
    if not settings.admin_password:
        return
    row = db.one("SELECT * FROM users WHERE username='admin' AND local=1")
    if not row:
        db.ex("INSERT INTO users(username, name, role, local, password_hash, created_at) VALUES "
              "('admin', 'Local admin', 'admin', 1, ?, ?)", (hash_password(settings.admin_password), time.time()))
    elif not check_password(settings.admin_password, row["password_hash"]):
        db.ex("UPDATE users SET password_hash=? WHERE id=?", (hash_password(settings.admin_password), row["id"]))


# ---------- login rate limit ----------

_fails: dict[str, list[float]] = {}


def rate_limited(ip: str) -> bool:
    now = time.time()
    hits = [t for t in _fails.get(ip, []) if now - t < 300]
    _fails[ip] = hits
    return len(hits) >= 10


def note_failure(ip: str) -> None:
    _fails.setdefault(ip, []).append(time.time())


# ---------- SSO ----------

def role_from_claims(claims: dict) -> str | None:
    """None means 'no group mapping configured; keep whatever role the user has'."""
    if not settings.groups_mapped:
        return None
    groups = claims.get(settings.oidc_groups_claim) or []
    if isinstance(groups, str):
        groups = [groups]
    groups = {str(g) for g in groups}
    for role, mapped in (("admin", settings.oidc_admin_groups), ("reviewer", settings.oidc_reviewer_groups),
                         ("viewer", settings.oidc_viewer_groups)):
        if groups & set(mapped):
            return role
    return settings.oidc_default_role


def upsert_sso_user(claims: dict) -> dict:
    iss, sub = claims.get("iss", ""), claims.get("sub", "")
    if not sub:
        raise HTTPException(400, "The identity provider didn't send a subject.")
    email = (claims.get("email") or claims.get("preferred_username") or "").lower()
    if settings.oidc_allowed_domains:
        domain = email.rsplit("@", 1)[-1] if "@" in email else ""
        if domain not in settings.oidc_allowed_domains:
            raise HTTPException(403, f"Accounts from {domain or 'this provider'} can't sign in here.")
    username = f"{iss}|{sub}"
    name = claims.get("name") or claims.get("preferred_username") or email
    mapped = role_from_claims(claims)
    row = db.one("SELECT * FROM users WHERE username=?", (username,))
    now = time.time()
    if not row:
        role = mapped if mapped is not None else settings.oidc_default_role
        # The first SSO user on a fresh install with no local admin becomes admin, so someone can manage it.
        if mapped is None and not db.one("SELECT 1 FROM users WHERE role='admin'"):
            role = "admin"
        db.ex("INSERT INTO users(username, email, name, role, local, created_at, last_login) VALUES (?,?,?,?,0,?,?)",
              (username, email, name, role, now, now))
    else:
        role = mapped if mapped is not None else row["role"]
        db.ex("UPDATE users SET email=?, name=?, role=?, last_login=? WHERE id=?", (email, name, role, now, row["id"]))
    user = db.one("SELECT * FROM users WHERE username=?", (username,))
    if ROLES.get(user["role"], 0) < 1:
        raise HTTPException(403, "Your account doesn't have access to Agentry. Ask an admin.")
    return user


# ---------- request guards ----------

def current_user(request: Request) -> dict | None:
    uid = request.session.get("uid")
    if not uid:
        return None
    u = db.one("SELECT id, username, email, name, role, local FROM users WHERE id=?", (uid,))
    if not u or ROLES.get(u["role"], 0) < 1:
        return None
    if u["local"] and not settings.local_login:
        return None
    return u


def need(role: str):
    def dep(request: Request) -> dict:
        u = current_user(request)
        if not u:
            raise HTTPException(401, "Sign in first.")
        if ROLES[u["role"]] < ROLES[role]:
            raise HTTPException(403, f"This needs the {role} role.")
        return u
    return Depends(dep)


__all__ = ["oauth", "OAuthError", "need", "current_user"]
