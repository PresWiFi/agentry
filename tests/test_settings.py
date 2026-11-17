"""Settings page, first-run setup, local accounts, and the Zoho grant exchange."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from agentry import auth, crypto, db, prefs, sdp
from agentry.app import app
from agentry.config import settings

H = {"X-Agentry": "1"}


@pytest.fixture()
def c():
    with TestClient(app) as client:
        yield client


def login(c, user="admin", pw="test-admin-pw"):
    r = c.post("/api/login", json={"username": user, "password": pw}, headers=H)
    assert r.status_code == 200, r.text


def test_settings_roundtrip_hides_secret(c, monkeypatch):
    db.ex("DELETE FROM users WHERE local=0")  # no SSO admins yet
    login(c)
    async def ok(issuer):
        return {"issuer": issuer}
    monkeypatch.setattr(auth, "check_issuer", ok)
    r = c.put("/api/settings", json={"public_url": "https://agentry.corp.com/", "oidc_issuer": "https://idp.corp.com",
                                     "oidc_client_id": "cid", "oidc_client_secret": "topsecret",
                                     "oidc_admin_groups": "a, b", "oidc_default_role": "viewer"}, headers=H)
    assert r.status_code == 200, r.text
    s = c.get("/api/settings").json()
    assert s["public_url"] == "https://agentry.corp.com"
    assert s["redirect_uri"] == "https://agentry.corp.com/auth/oidc/callback"
    assert s["oidc_admin_groups"] == ["a", "b"] and s["oidc_client_secret_set"] is True
    assert "topsecret" not in json.dumps(s)
    stored = db.one("SELECT value FROM prefs WHERE key='oidc_client_secret'")["value"]
    assert "topsecret" not in stored and crypto.decrypt(json.loads(stored)) == "topsecret"
    # Blank secret keeps the old one.
    c.put("/api/settings", json={"oidc_client_secret": "", "oidc_label": "Sign in with Microsoft"}, headers=H)
    assert prefs.get("oidc_client_secret") == "topsecret"
    me = c.get("/api/me").json()
    assert me["login"]["oidc"] is True and me["login"]["oidc_label"] == "Sign in with Microsoft"
    # The SSO client follows the settings.
    assert auth.oidc_client().client_id == "cid"
    c.put("/api/settings", json={"oidc_client_id": "cid2"}, headers=H)
    assert auth.oidc_client().client_id == "cid2"
    # Can't turn password sign-in off without an SSO admin.
    r = c.put("/api/settings", json={"local_login": False}, headers=H)
    assert r.status_code == 400 and "SSO" in r.json()["detail"]
    # With an SSO admin it's allowed, and then SSO can't be removed.
    auth.upsert_sso_user({"iss": "https://idp.corp.com", "sub": "boss", "email": "boss@corp.com", "groups": ["a"]})
    assert c.put("/api/settings", json={"local_login": False}, headers=H).status_code == 200
    assert c.get("/api/me").json()["user"] is None  # the local admin is now signed out
    prefs.update({"local_login": True})
    login(c)
    prefs.update({"local_login": False})
    r = c.put("/api/settings", json={"oidc_issuer": ""}, headers=H)
    assert r.status_code == 401  # local sessions are off entirely
    prefs.update({"local_login": True})
    login(c)
    db.ex("DELETE FROM users WHERE local=0")
    assert c.put("/api/settings", json={"public_url": "ftp://x"}, headers=H).status_code == 400
    c.put("/api/settings", json={"oidc_issuer": "", "oidc_client_id": "", "public_url": ""}, headers=H)
    assert c.get("/api/me").json()["login"]["oidc"] is False


def test_bad_issuer_rejected(c):
    login(c)
    r = c.put("/api/settings", json={"oidc_issuer": "http://not-https.example"}, headers=H)
    assert r.status_code == 400 and "https" in r.json()["detail"]


def test_env_import_once(monkeypatch):
    db.conn()
    db.ex("DELETE FROM prefs WHERE key='oidc_label'")
    prefs.update({})  # clear cache
    monkeypatch.setenv("OIDC_LABEL", "From env")
    assert "oidc_label" in prefs.import_env()
    assert prefs.get("oidc_label") == "From env"
    monkeypatch.setenv("OIDC_LABEL", "Changed env")
    assert prefs.import_env() == []
    assert prefs.get("oidc_label") == "From env"


def test_local_users_and_password_change(c):
    login(c)
    r = c.post("/api/users", json={"username": "tech1", "password": "short", "role": "reviewer"}, headers=H)
    assert r.status_code == 400
    r = c.post("/api/users", json={"username": "tech1", "password": "long-enough-pw", "role": "reviewer"}, headers=H)
    assert r.status_code == 200
    assert c.post("/api/users", json={"username": "tech1", "password": "long-enough-pw", "role": "viewer"}, headers=H).status_code == 409
    uid = r.json()["id"]
    other = TestClient(app)
    login(other, "tech1", "long-enough-pw")
    me2 = TestClient(app)
    login(me2, "tech1", "long-enough-pw")
    assert me2.get("/api/me").json()["user"]["role"] == "reviewer"
    assert me2.post("/api/me/password", json={"current": "wrong", "new": "another-long-pw"}, headers=H).status_code == 401
    assert me2.post("/api/me/password", json={"current": "long-enough-pw", "new": "another-long-pw"}, headers=H).status_code == 200
    assert me2.get("/api/me").json()["user"] is not None          # this session stays
    assert other.get("/api/me").json()["user"] is None            # other sessions signed out
    assert c.post(f"/api/users/{uid}/password", json={"password": "admin-set-pw-1"}, headers=H).status_code == 200
    login(other, "tech1", "admin-set-pw-1")


def test_first_run_setup(c, monkeypatch):
    monkeypatch.setattr(settings, "admin_password", "")
    saved = db.q("SELECT * FROM users")
    db.ex("DELETE FROM users")
    try:
        auth.prepare_setup()
        assert auth.setup_code
        assert c.get("/api/me").json()["setup"] is True
        assert c.post("/api/setup", json={"code": "nope", "username": "boss", "password": "long-enough-pw"}, headers=H).status_code == 401
        r = c.post("/api/setup", json={"code": auth.setup_code.upper(), "username": "boss", "password": "long-enough-pw"}, headers=H)
        assert r.status_code == 200, r.text
        me = c.get("/api/me").json()
        assert me["setup"] is False and me["user"]["role"] == "admin"
        assert c.post("/api/setup", json={"code": "x", "username": "b2", "password": "long-enough-pw"}, headers=H).status_code == 409
    finally:
        db.ex("DELETE FROM users")
        for u in saved:
            cols = ",".join(u)
            db.ex(f"INSERT INTO users({cols}) VALUES ({','.join('?' * len(u))})", tuple(u.values()))


def test_cloud_base():
    assert sdp.cloud_base("https://sdpondemand.manageengine.com/app/itdesk/ui/requests?x=1") == \
        "https://sdpondemand.manageengine.com/app/itdesk"
    assert sdp.cloud_base("https://sdpondemand.manageengine.eu/app/corp/") == "https://sdpondemand.manageengine.eu/app/corp"


def test_grant_exchange_and_token_cache(c, monkeypatch):
    seen = []

    def zoho(request: httpx.Request):
        form = dict(x.split("=") for x in request.content.decode().split("&"))
        seen.append(form["grant_type"])
        if form["grant_type"] == "authorization_code":
            if form["code"] != "1000.good":
                return httpx.Response(200, json={"error": "invalid_code"})
            return httpx.Response(200, json={"refresh_token": "1000.refresh", "access_token": "a"})
        return httpx.Response(200, json={"access_token": "1000.access", "expires_in": 3600})

    real_exchange = sdp.exchange_grant
    monkeypatch.setattr(sdp, "exchange_grant", lambda *a: real_exchange(*a, transport=httpx.MockTransport(zoho)))
    login(c)
    body = {"name": "SDP Cloud", "kind": "sdp",
            "config": {"edition": "cloud", "base_url": "https://sdpondemand.manageengine.com/app/itdesk/ui/requests",
                       "accounts_url": "https://accounts.zoho.com"},
            "secret": {"client_id": "1000.CID", "client_secret": "sec", "grant_code": "1000.bad"}}
    r = c.post("/api/connections", json=body, headers=H)
    assert r.status_code == 400 and "invalid_code" in r.json()["detail"]
    body["secret"]["grant_code"] = "1000.good"
    r = c.post("/api/connections", json=body, headers=H)
    assert r.status_code == 200, r.text
    conn = [x for x in c.get("/api/connections").json() if x["id"] == r.json()["id"]][0]
    assert conn["config"]["base_url"] == "https://sdpondemand.manageengine.com/app/itdesk"
    assert set(conn["secrets_set"]) == {"client_id", "client_secret", "refresh_token"}

    # Calls through two separate clients reuse one access token.
    def api(request: httpx.Request):
        if request.url.host == "accounts.zoho.com":
            return zoho(request)
        assert request.headers["authorization"] == "Zoho-oauthtoken 1000.access"
        return httpx.Response(200, json={"solutions": [], "list_info": {"total_count": 0}, "response_status": [{"status": "success"}]})

    import asyncio
    secret = {"client_id": "1000.CID", "client_secret": "sec", "refresh_token": "1000.refresh"}
    cfg = {"edition": "cloud", "base_url": "https://sdpondemand.manageengine.com/app/itdesk"}

    async def two():
        for _ in range(2):
            s = sdp.SDP(cfg, secret, transport=httpx.MockTransport(api))
            await s._call("GET", "solutions", {"list_info": {"row_count": 1}})
            await s.close()
    seen.clear()
    asyncio.run(two())
    assert seen == ["refresh_token"]
