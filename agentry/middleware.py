"""ASGI middleware whose behaviour follows the Settings page without a restart."""
from __future__ import annotations

from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from . import prefs


class DynamicProxyHeaders:
    """Trusts X-Forwarded-For/Proto only from the reverse proxies listed in Settings."""

    def __init__(self, app):
        self.app = app
        self._inner = None
        self._key = None

    async def __call__(self, scope, receive, send):
        trusted = prefs.get("trusted_proxies") or "127.0.0.1"
        if trusted != self._key:
            hosts = [h.strip() for h in trusted.split(",") if h.strip()]
            self._inner = ProxyHeadersMiddleware(self.app, trusted_hosts=hosts or ["127.0.0.1"])
            self._key = trusted
        await self._inner(scope, receive, send)


class SecureCookies:
    """Adds the Secure flag to cookies on requests that arrived over HTTPS (directly or via a trusted proxy)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("scheme") != "https":
            return await self.app(scope, receive, send)

        async def wrapped(message):
            if message["type"] == "http.response.start":
                headers = []
                for k, v in message.get("headers", []):
                    if k.lower() == b"set-cookie" and b"; secure" not in v.lower():
                        v = v + b"; secure"
                    headers.append((k, v))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, wrapped)
