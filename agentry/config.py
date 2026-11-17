"""Settings from environment variables (see .env.example)."""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


def _list(name: str) -> list[str]:
    return [x.strip() for x in os.environ.get(name, "").split(",") if x.strip()]


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("AGENTRY_DATA", "./data")))
    host: str = os.environ.get("AGENTRY_HOST", "0.0.0.0")
    port: int = int(os.environ.get("AGENTRY_PORT", "8090"))
    public_url: str = os.environ.get("AGENTRY_PUBLIC_URL", "").rstrip("/")
    trusted_proxies: str = os.environ.get("AGENTRY_TRUSTED_PROXIES", "127.0.0.1")
    secret_key: str = os.environ.get("AGENTRY_SECRET_KEY", "")
    secure_cookies: bool = _bool("AGENTRY_SECURE_COOKIES", False)

    # Local sign-in: one built-in admin account, for setup and break-glass.
    admin_password: str = os.environ.get("AGENTRY_ADMIN_PASSWORD", "")
    local_login: bool = _bool("AGENTRY_LOCAL_LOGIN", True)

    # OpenID Connect
    oidc_issuer: str = os.environ.get("OIDC_ISSUER", "").rstrip("/")
    oidc_client_id: str = os.environ.get("OIDC_CLIENT_ID", "")
    oidc_client_secret: str = os.environ.get("OIDC_CLIENT_SECRET", "")
    oidc_scopes: str = os.environ.get("OIDC_SCOPES", "openid email profile")
    oidc_label: str = os.environ.get("OIDC_LABEL", "Sign in with SSO")
    oidc_groups_claim: str = os.environ.get("OIDC_GROUPS_CLAIM", "groups")
    oidc_admin_groups: list[str] = field(default_factory=lambda: _list("OIDC_ADMIN_GROUPS"))
    oidc_reviewer_groups: list[str] = field(default_factory=lambda: _list("OIDC_REVIEWER_GROUPS"))
    oidc_viewer_groups: list[str] = field(default_factory=lambda: _list("OIDC_VIEWER_GROUPS"))
    # Role for people with no matching group: none (denied), viewer, reviewer, admin.
    oidc_default_role: str = os.environ.get("OIDC_DEFAULT_ROLE", "none")
    oidc_allowed_domains: list[str] = field(default_factory=lambda: [d.lower() for d in _list("OIDC_ALLOWED_DOMAINS")])

    scheduler_tick: int = int(os.environ.get("AGENTRY_SCHEDULER_TICK", "30"))

    @property
    def oidc_enabled(self) -> bool:
        return bool(self.oidc_issuer and self.oidc_client_id)

    @property
    def groups_mapped(self) -> bool:
        return bool(self.oidc_admin_groups or self.oidc_reviewer_groups or self.oidc_viewer_groups)

    def resolve_secret(self) -> str:
        """AGENTRY_SECRET_KEY, or a key generated once and kept in the data folder."""
        if self.secret_key:
            return self.secret_key
        path = self.data_dir / "secret.key"
        if path.exists():
            return path.read_text().strip()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        key = secrets.token_urlsafe(48)
        path.write_text(key)
        path.chmod(0o600)
        return key


settings = Settings()
