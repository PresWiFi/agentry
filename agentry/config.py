"""Process-level settings from environment variables. Everything else is on the Settings page (prefs.py)."""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("AGENTRY_DATA", "./data")))
    host: str = os.environ.get("AGENTRY_HOST", "0.0.0.0")
    port: int = int(os.environ.get("AGENTRY_PORT", "8090"))
    secret_key: str = os.environ.get("AGENTRY_SECRET_KEY", "")
    # Only used once, to create the first admin without the setup page.
    admin_password: str = os.environ.get("AGENTRY_ADMIN_PASSWORD", "")
    scheduler_tick: int = int(os.environ.get("AGENTRY_SCHEDULER_TICK", "30"))

    @property
    def secret_source(self) -> str:
        return "env" if self.secret_key else "file"

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
