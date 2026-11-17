"""End to end through the API: set up a server, a connection and an agent, run it against fake
ServiceDesk Plus and Ollama, then review and apply its proposals."""
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from agentry import db, llm as llm_mod
from agentry.agents import sdp_solutions
from agentry import app as app_mod
from agentry.app import app
from agentry.sdp import SDP

from fakes import FakeOllama, FakeSDP

H = {"X-Agentry": "1"}


@pytest.fixture()
def env(monkeypatch):
    sdp, oll = FakeSDP(), FakeOllama()
    now = int(time.time() * 1000)
    for i in range(3):
        sdp.add_request(f"9{i}", f"Printer paper jam on floor {i}", "Opened tray 2, removed the jammed paper", now - i * 1000)
    for i in range(2):
        sdp.add_request(f"8{i}", "Need password reset", "Reset the password from the portal", now - 5000 - i)
    sdp.add_request("70", "Outlook keeps asking for email password", "Cleared cached creds", now - 9000)
    sdp.add_request("60", "Old printer paper jam", "Removed paper", now - 90 * 86400 * 1000)  # outside lookback

    fake_sdp = lambda cfg, secret: SDP(cfg, secret, transport=httpx.MockTransport(sdp.handler))
    monkeypatch.setattr(sdp_solutions, "SDP", fake_sdp)
    monkeypatch.setattr(app_mod, "SDP", fake_sdp)
    monkeypatch.setattr(llm_mod.LLM, "from_server", classmethod(
        lambda cls, sid: cls("ollama", "http://gpu:11434", transport=httpx.MockTransport(oll.handler))))
    with TestClient(app) as c:
        yield c, sdp, oll


def login(c, user="admin", pw="test-admin-pw"):
    return c.post("/api/login", json={"username": user, "password": pw}, headers=H)


def wait_run(c, rid):
    for _ in range(200):
        r = c.get(f"/api/runs/{rid}").json()
        if r["status"] != "running":
            return r
        time.sleep(0.05)
    raise AssertionError("run didn't finish")


def test_auth_basics(env):
    c, _, _ = env
    assert c.get("/api/overview").status_code == 401
    assert c.post("/api/login", json={"username": "admin", "password": "test-admin-pw"}).status_code == 403  # no CSRF header
    assert login(c, pw="wrong").status_code == 401
    assert login(c).status_code == 200
    assert c.get("/api/me").json()["user"]["role"] == "admin"
    assert c.get("/api/overview").status_code == 200


def test_full_flow(env):
    c, sdp, oll = env
    assert login(c).status_code == 200
    sid = c.post("/api/servers", json={"name": "gpu", "kind": "ollama", "base_url": "http://gpu:11434"}, headers=H).json()["id"]
    st = c.get(f"/api/servers/{sid}/status").json()
    assert st["ok"] and st["version"] == "0.13.0"

    cid = c.post("/api/connections", json={"name": "SDP", "kind": "sdp",
                                           "config": {"edition": "onprem", "base_url": "https://sdp.test"},
                                           "secret": {"api_key": "KEY"}}, headers=H).json()["id"]
    conns = c.get("/api/connections").json()
    assert conns[0]["secrets_set"] == ["api_key"] and "KEY" not in str(conns)
    assert c.post(f"/api/connections/{cid}/test", headers=H).json() == {"ok": True, "solutions": 2, "requests_readable": True}

    bad = c.post("/api/agents", json={"name": "KB", "type": "sdp_solutions", "config": {
        "connection": cid, "server": sid, "model": "qwen3:14b", "find_gaps": True}}, headers=H)
    assert bad.status_code == 400 and "embedding" in bad.json()["detail"]

    aid = c.post("/api/agents", json={"name": "KB", "type": "sdp_solutions", "schedule_minutes": 0, "config": {
        "connection": cid, "server": sid, "model": "qwen3:14b", "embed_model": "nomic-embed-text",
        "find_gaps": True, "new_topic": "General"}}, headers=H).json()["id"]
    t = c.post(f"/api/agents/{aid}/test", headers=H).json()
    assert t["model"]["ok"] and t["embed_model"]["ok"] and t["connection"]["solutions"] == 2

    run = wait_run(c, c.post(f"/api/agents/{aid}/run", headers=H).json()["run_id"])
    assert run["status"] == "ok", run
    assert "reviewed 2, 1 updates proposed" in run["summary"]
    assert "1 gaps, 1 drafts proposed" in run["summary"]
    assert oll.chats[0]["options"]["num_ctx"] == 16384 and oll.chats[0]["format"] == "json"
    assert "think" not in oll.chats[0]  # model default unless set

    props = c.get("/api/proposals").json()
    assert len(props) == 2
    upd = next(p for p in props if p["kind"] == "update")
    new = next(p for p in props if p["kind"] == "create")
    detail = c.get(f"/api/proposals/{upd['id']}").json()
    assert detail["target_id"] == "101"
    assert "script" not in detail["proposed"]["description"] and "onerror" not in detail["proposed"]["description"]
    nd = c.get(f"/api/proposals/{new['id']}").json()
    assert "#90" in nd["rationale"] and "#60" not in nd["rationale"] and "#70" not in nd["rationale"]

    # Reviewer edits, then approves.
    assert c.put(f"/api/proposals/{upd['id']}", json={"title": "VPN: restart the client"}, headers=H).status_code == 200
    r = c.post(f"/api/proposals/{upd['id']}/approve", headers=H)
    assert r.status_code == 200, r.text
    assert sdp.solutions["101"]["title"] == "VPN: restart the client"
    assert ("PUT", "/api/v3/solutions/101") in sdp.calls
    r = c.post(f"/api/proposals/{new['id']}/approve", headers=H)
    assert r.status_code == 200 and "Created solution 500" in r.json()["result"]
    assert sdp.solutions["500"]["topic"] == {"name": "General"}
    assert c.post(f"/api/proposals/{new['id']}/approve", headers=H).status_code == 409  # not twice

    # Second run: nothing new to propose (101 now reads fine, printer tickets already drafted).
    run = wait_run(c, c.post(f"/api/agents/{aid}/run", headers=H).json()["run_id"])
    assert run["status"] == "ok"
    assert len(c.get("/api/proposals?status=pending").json()) == 0, run


def test_conflict_when_solution_changed(env):
    c, sdp, _ = env
    login(c)
    sid = c.post("/api/servers", json={"name": "gpu", "kind": "ollama", "base_url": "http://gpu:11434"}, headers=H).json()["id"]
    cid = c.post("/api/connections", json={"name": "SDP", "kind": "sdp", "config": {"base_url": "https://sdp.test"},
                                           "secret": {"api_key": "KEY"}}, headers=H).json()["id"]
    aid = c.post("/api/agents", json={"name": "KB2", "type": "sdp_solutions", "config": {
        "connection": cid, "server": sid, "model": "m"}}, headers=H).json()["id"]
    wait_run(c, c.post(f"/api/agents/{aid}/run", headers=H).json()["run_id"])
    p = [p for p in c.get("/api/proposals").json() if p["agent_id"] == aid][0]
    sdp.solutions["101"]["last_updated_time"] = {"value": "1750000000000"}  # someone edited it in SDP
    r = c.post(f"/api/proposals/{p['id']}/approve", headers=H)
    assert r.status_code == 409 and "changed in ServiceDesk Plus" in r.json()["detail"]
    assert c.get(f"/api/proposals/{p['id']}").json()["status"] == "failed"


def test_roles(env):
    c, _, _ = env
    now = time.time()
    from agentry.auth import hash_password
    db.ex("INSERT OR IGNORE INTO users(username, name, role, local, password_hash, created_at) VALUES "
          "('viewer1', 'V', 'viewer', 1, ?, ?)", (hash_password("pw"), now))
    login(c, "viewer1", "pw")
    assert c.get("/api/agents").status_code == 200
    assert c.post("/api/servers", json={"name": "x", "kind": "ollama", "base_url": "http://x"}, headers=H).status_code == 403
    assert c.post("/api/proposals/1/approve", headers=H).status_code == 403
    assert c.get("/api/users").status_code == 403
