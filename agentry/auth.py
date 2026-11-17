"""Sign-in: OpenID Connect (any provider) and local accounts. Roles: admin > reviewer > viewer.

SSO settings live on the Settings page. With group mapping set, the role comes from the groups claim at
every sign-in; without it, new SSO users get the default role and an admin changes it on the Users page.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time

import httpx
from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import Depends, HTTPException, Request

from . import db, prefs
from .config import settings

log = logging.getLogger("agentry.auth")
ROLES = {"none": 0, "viewer": 1, "reviewer": 2, "admin": 3}
MIN_PASSWORD = 10

# ---------- SSO client, rebuilt when the settings change ----------

_client = None
_client_version = -1


def oidc_client():
    global _client, _client_version
    if _client_version != prefs.version or _client is None:
        p = prefs.all()
        if not prefs.oidc_enabled():
            _client = None
        else:
            oauth = OAuth()
            oauth.register("oidc", client_id=p["oidc_client_id"], client_secret=p["oidc_client_secret"] or None,
                           server_metadata_url=p["oidc_issuer"] + "/.well-known/openid-configuration",
                           client_kwargs={"scope": p["oidc_scopes"] or "openid email profile",
                                          "code_challenge_method": "S256"})
            _client = oauth.oidc
        _client_version = prefs.version
    return _client


async def check_issuer(issuer: str) -> dict:
    """Fetches the provider's discovery document. Raises ValueError with a readable reason."""
    issuer = issuer.rstrip("/")
    if not issuer.startswith("https://") and not issuer.startswith(("http://localhost", "http://127.0.0.1")):
        raise ValueError("The issuer must be an https:// address.")
    url = issuer + "/.well-known/openid-configuration"
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(url)
        r.raise_for_status()
        d = r.json()
    except (httpx.HTTPError, ValueError) as e:
        raise ValueError(f"Couldn't read {url}: {e.__class__.__name__}: {e}")
    for key in ("issuer", "authorization_endpoint", "token_endpoint", "jwks_uri"):
        if not d.get(key):
            raise ValueError(f"The provider's configuration has no {key}.")
    if d["issuer"].rstrip("/") != issuer:
        raise ValueError(f"The provider calls itself {d['issuer']}. Use exactly that as the issuer.")
    return d


def redirect_uri(request: Request) -> str:
    base = prefs.get("public_url") or str(request.base_url).rstrip("/")
    return base + "/auth/oidc/callback"


# ---------- passwords and local users ----------

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


def validate_password(pw: str) -> None:
    if len(pw or "") < MIN_PASSWORD:
        raise HTTPException(400, f"Use at least {MIN_PASSWORD} characters.")


def create_local_user(username: str, password: str, role: str, name: str = "") -> int:
    username = (username or "").strip()
    if not username or "|" in username or len(username) > 64:
        raise HTTPException(400, "Pick a username (no | character).")
    if role not in ROLES or role == "none":
        raise HTTPException(400, "Pick a role.")
    validate_password(password)
    if db.one("SELECT 1 FROM users WHERE username=?", (username,)):
        raise HTTPException(409, "That username is taken.")
    return db.ex("INSERT INTO users(username, name, role, local, password_hash, created_at) VALUES (?,?,?,1,?,?)",
                 (username, name.strip() or username, role, hash_password(password), time.time()))


# ---------- first-run setup ----------

setup_code: str | None = None


def needs_setup() -> bool:
    return not db.one("SELECT 1 FROM users")


def prepare_setup() -> None:
    """Creates the first admin from AGENTRY_ADMIN_PASSWORD, or prints a one-time setup code."""
    global setup_code
    if not needs_setup():
        setup_code = None
        return
    if settings.admin_password:
        db.ex("INSERT INTO users(username, name, role, local, password_hash, created_at) VALUES "
              "('admin', 'Admin', 'admin', 1, ?, ?)", (hash_password(settings.admin_password), time.time()))
        log.info("Created local user 'admin' from AGENTRY_ADMIN_PASSWORD.")
        return
    setup_code = "-".join(secrets.token_hex(3) for _ in range(3))
    log.warning("=" * 60)
    log.warning("Agentry first-run setup code: %s", setup_code)
    log.warning("Open Agentry in a browser and enter it to create the first admin.")
    log.warning("=" * 60)


# ---------- login rate limit ----------

_fails: dict[str, list[float]] = {}


def rate_limited(ip: str) -> bool:
    now = time.time()
    hits = [t for t in _fails.get(ip, []) if now - t < 300]
    _fails[ip] = hits
    return len(hits) >= 10


def note_failure(ip: str) -> None:
    _fails.setdefault(ip, []).append(time.time())


# ---------- SSO users ----------

def role_from_claims(claims: dict) -> str | None:
    """None means 'no group mapping configured; keep whatever role the user has'."""
    p = prefs.all()
    if not prefs.groups_mapped():
        return None
    groups = claims.get(p["oidc_groups_claim"]) or []
    if isinstance(groups, str):
        groups = [groups]
    groups = {str(g) for g in groups}
    for role in ("admin", "reviewer", "viewer"):
        if groups & set(p[f"oidc_{role}_groups"]):
            return role
    return p["oidc_default_role"]


def upsert_sso_user(claims: dict) -> dict:
    p = prefs.all()
    iss, sub = claims.get("iss", ""), claims.get("sub", "")
    if not sub:
        raise HTTPException(400, "The identity provider didn't send a subject.")
    email = (claims.get("email") or claims.get("preferred_username") or "").lower()
    if p["oidc_allowed_domains"]:
        domain = email.rsplit("@", 1)[-1] if "@" in email else ""
        if domain not in p["oidc_allowed_domains"]:
            raise HTTPException(403, f"Accounts from {domain or 'this provider'} can't sign in here.")
    username = f"{iss}|{sub}"
    name = claims.get("name") or claims.get("preferred_username") or email
    mapped = role_from_claims(claims)
    row = db.one("SELECT * FROM users WHERE username=?", (username,))
    now = time.time()
    if not row:
        role = mapped if mapped is not None else p["oidc_default_role"]
        db.ex("INSERT INTO users(username, email, name, role, local, created_at, last_login) VALUES (?,?,?,?,0,?,?)",
              (username, email, name, role, now, now))
    else:
        role = mapped if mapped is not None else row["role"]
        db.ex("UPDATE users SET email=?, name=?, role=?, last_login=? WHERE id=?", (email, name, role, now, row["id"]))
    user = db.one("SELECT * FROM users WHERE username=?", (username,))
    if ROLES.get(user["role"], 0) < 1:
        raise HTTPException(403, "Your account doesn't have access to Agentry yet. Ask an admin to give you a role.")
    return user


# ---------- request guards ----------

def current_user(request: Request) -> dict | None:
    uid = request.session.get("uid")
    if not uid:
        return None
    u = db.one("SELECT id, username, email, name, role, local, password_hash FROM users WHERE id=?", (uid,))
    if not u or ROLES.get(u["role"], 0) < 1:
        return None
    if u["local"] and not prefs.get("local_login"):
        return None
    # A password change signs out other sessions of that user.
    if u["local"] and request.session.get("pw") != (u["password_hash"] or "")[-16:]:
        return None
    u.pop("password_hash")
    return u


def start_session(request: Request, user: dict) -> None:
    request.session.clear()
    request.session["uid"] = user["id"]
    if user.get("local"):
        row = db.one("SELECT password_hash FROM users WHERE id=?", (user["id"],))
        request.session["pw"] = (row["password_hash"] or "")[-16:]


def need(role: str):
    def dep(request: Request) -> dict:
        u = current_user(request)
        if not u:
            raise HTTPException(401, "Sign in first.")
        if ROLES[u["role"]] < ROLES[role]:
            raise HTTPException(403, f"This needs the {role} role.")
        return u
    return Depends(dep)


__all__ = ["OAuthError", "need", "current_user", "oidc_client"]
