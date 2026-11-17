"""ManageEngine ServiceDesk Plus REST API v3 client (on-premises and Cloud).

On-premises: base URL like https://sdp.example.com, technician API key sent as the `authtoken` header.
Cloud: base URL like https://sdpondemand.manageengine.com/app/<portal>, Zoho OAuth access token sent as
`Authorization: Zoho-oauthtoken <token>` (refreshed here from a refresh token).

v3 takes parameters as a JSON `input_data` value: in the query string for GET, form-encoded for POST/PUT.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from urllib.parse import urlsplit

import httpx

ACCEPT = "application/vnd.manageengine.sdp.v3+json"
CLOUD_SCOPES = "SDPOnDemand.solutions.ALL,SDPOnDemand.requests.READ"

# Zoho caps how many access tokens one refresh token may mint per 10 minutes, so share them
# across clients: key = hash of (accounts URL, client ID, refresh token) -> (token, expiry).
_tokens: dict[str, tuple[str, float]] = {}


class SDPError(Exception):
    pass


def _ms(v) -> int | None:
    """SDP time fields look like {"value": "1700000000000", "display_value": "..."}."""
    if isinstance(v, dict):
        v = v.get("value")
    try:
        return int(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


class SDP:
    def __init__(self, config: dict, secret: dict, transport: httpx.AsyncBaseTransport | None = None):
        self.edition = config.get("edition", "onprem")  # onprem | cloud
        self.base = config["base_url"].rstrip("/")
        self.verify_tls = config.get("verify_tls", True)
        self.secret = secret
        self.accounts_url = config.get("accounts_url") or "https://accounts.zoho.com"
        self._token_key = hashlib.sha256("|".join([self.accounts_url, secret.get("client_id", ""),
                                                   secret.get("refresh_token", "")]).encode()).hexdigest()
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(60, connect=10), verify=self.verify_tls,
                                      transport=transport)

    async def close(self):
        await self.http.aclose()

    async def _auth_headers(self) -> dict:
        if self.edition == "onprem":
            return {"authtoken": self.secret.get("api_key", "")}
        token, exp = _tokens.get(self._token_key, ("", 0.0))
        if not token or time.time() > exp - 120:
            try:
                r = await self.http.post(self.accounts_url.rstrip("/") + "/oauth/v2/token", data={
                    "grant_type": "refresh_token", "refresh_token": self.secret.get("refresh_token", ""),
                    "client_id": self.secret.get("client_id", ""), "client_secret": self.secret.get("client_secret", "")})
                data = r.json()
            except (httpx.HTTPError, ValueError) as e:
                raise SDPError(f"Couldn't refresh the Zoho token: {e}")
            if "access_token" not in data:
                raise SDPError(f"Zoho token refresh failed: {data.get('error', data)}. Check the client ID/secret, "
                               "the accounts URL (data center), and that the refresh token wasn't revoked.")
            token, exp = data["access_token"], time.time() + int(data.get("expires_in", 3600))
            _tokens[self._token_key] = (token, exp)
        return {"Authorization": f"Zoho-oauthtoken {token}"}

    async def _call(self, method: str, path: str, input_data: dict | None = None) -> dict:
        headers = {"Accept": ACCEPT, **(await self._auth_headers())}
        url = f"{self.base}/api/v3/{path.lstrip('/')}"
        kw: dict = {}
        if input_data is not None:
            if method == "GET":
                kw["params"] = {"input_data": json.dumps(input_data)}
            else:
                kw["data"] = {"input_data": json.dumps(input_data)}
        for attempt in range(4):
            try:
                r = await self.http.request(method, url, headers=headers, **kw)
            except httpx.HTTPError as e:
                raise SDPError(f"Can't reach ServiceDesk Plus at {self.base}: {e.__class__.__name__}: {e}")
            if r.status_code != 429 or attempt == 3:
                break
            # Rate limited (Cloud has per-minute API limits): back off and retry.
            await asyncio.sleep(min(60, float(r.headers.get("retry-after") or 5 * 2 ** attempt)))
        try:
            data = r.json()
        except ValueError:
            raise SDPError(f"ServiceDesk Plus answered HTTP {r.status_code} with non-JSON: {r.text[:200]}")
        statuses = data.get("response_status")
        statuses = statuses if isinstance(statuses, list) else [statuses] if statuses else []
        failed = [s for s in statuses if isinstance(s, dict) and s.get("status") == "failed"]
        if r.status_code >= 400 or failed:
            msgs = []
            for s in failed:
                for m in s.get("messages") or []:
                    msgs.append(m.get("message") or m.get("type") or str(m))
                if not s.get("messages"):
                    msgs.append(f"status_code {s.get('status_code')}")
            raise SDPError(f"ServiceDesk Plus {method} {path} failed (HTTP {r.status_code}): "
                           f"{'; '.join(msgs) or r.text[:200]}")
        return data

    async def _list(self, path: str, key: str, list_info: dict, limit: int):
        start, got = 1, 0
        while got < limit:
            info = {**list_info, "row_count": min(100, limit - got), "start_index": start}
            data = await self._call("GET", path, {"list_info": info})
            items = data.get(key) or []
            for it in items:
                yield it
            got += len(items)
            start += len(items)
            if not items or not (data.get("list_info") or {}).get("has_more_rows"):
                return

    # ---- solutions ----

    async def list_solutions(self, limit: int = 2000):
        async for s in self._list("solutions", "solutions",
                                  {"sort_field": "created_time", "sort_order": "asc"}, limit):
            yield s

    async def get_solution(self, sid: str) -> dict:
        return (await self._call("GET", f"solutions/{sid}")).get("solution") or {}

    async def update_solution(self, sid: str, fields: dict) -> dict:
        return (await self._call("PUT", f"solutions/{sid}", {"solution": fields})).get("solution") or {}

    async def create_solution(self, fields: dict) -> dict:
        return (await self._call("POST", "solutions", {"solution": fields})).get("solution") or {}

    # ---- requests ----

    async def recent_requests(self, statuses: list[str], since_ms: int, limit: int = 500):
        """Newest first; stops at the first request created before since_ms."""
        info: dict = {"sort_field": "created_time", "sort_order": "desc"}
        if statuses:
            info["search_criteria"] = [{"field": "status.name", "condition": "is", "values": statuses}]
        async for r in self._list("requests", "requests", info, limit):
            created = _ms(r.get("created_time"))
            if created is not None and created < since_ms:
                return
            yield r

    async def get_request(self, rid: str) -> dict:
        return (await self._call("GET", f"requests/{rid}")).get("request") or {}

    async def get_resolution(self, rid: str) -> str:
        req = await self.get_request(rid)
        res = req.get("resolution") or {}
        if isinstance(res, dict) and res.get("content"):
            return res["content"]
        try:
            data = await self._call("GET", f"requests/{rid}/resolutions")
            res = data.get("resolution") or {}
            return res.get("content") or "" if isinstance(res, dict) else ""
        except SDPError:
            return ""

    async def test(self) -> dict:
        out = {}
        data = await self._call("GET", "solutions", {"list_info": {"row_count": 1, "start_index": 1,
                                                                   "get_total_count": True}})
        out["solutions"] = (data.get("list_info") or {}).get("total_count", len(data.get("solutions") or []))
        data = await self._call("GET", "requests", {"list_info": {"row_count": 1, "start_index": 1}})
        out["requests_readable"] = True
        return out


async def exchange_grant(accounts_url: str, client_id: str, client_secret: str, code: str,
                         transport: httpx.AsyncBaseTransport | None = None) -> str:
    """Trades a one-time grant code from a Zoho Self Client for a long-lived refresh token."""
    url = (accounts_url or "https://accounts.zoho.com").rstrip("/") + "/oauth/v2/token"
    try:
        async with httpx.AsyncClient(timeout=20, transport=transport) as c:
            r = await c.post(url, data={"grant_type": "authorization_code", "code": code.strip(),
                                        "client_id": client_id, "client_secret": client_secret})
        data = r.json()
    except (httpx.HTTPError, ValueError) as e:
        raise SDPError(f"Couldn't reach {url}: {e}")
    if not data.get("refresh_token"):
        err = data.get("error", data)
        hint = " Grant codes expire after a few minutes and work once; generate a new one." \
            if err == "invalid_code" else ""
        raise SDPError(f"Zoho didn't return a refresh token: {err}.{hint}")
    return data["refresh_token"]


def cloud_base(url: str) -> str:
    """https://sdpondemand.manageengine.com/app/itdesk/ui/requests -> https://sdpondemand.manageengine.com/app/itdesk"""
    u = urlsplit(url.strip())
    parts = [p for p in u.path.split("/") if p]
    if "app" in parts and parts.index("app") + 1 < len(parts):
        i = parts.index("app")
        return f"{u.scheme}://{u.netloc}/app/{parts[i + 1]}"
    return url.strip().rstrip("/")


def updated_ms(sol: dict) -> int | None:
    for k in ("last_updated_time", "updated_time", "last_modified_time", "created_time"):
        v = _ms(sol.get(k))
        if v:
            return v
    return None
