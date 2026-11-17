"""Agent on two llama.cpp servers: one for chat, one started with --embeddings."""
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from agentry import app as app_mod, db, llm as llm_mod
from agentry.agents import sdp_solutions
from agentry.app import app
from agentry.sdp import SDP

from fakes import FakeLlamaCpp, FakeSDP

H = {"X-Agentry": "1"}


def test_agent_on_llamacpp(monkeypatch):
    sdp = FakeSDP()
    now = int(time.time() * 1000)
    for i in range(3):
        sdp.add_request(f"9{i}", f"Printer paper jam {i}", "Removed the jammed paper from tray 2", now - i)
    chat, emb = FakeLlamaCpp("qwen2.5-14b", False), FakeLlamaCpp("nomic-embed", True)
    fake_sdp = lambda cfg, secret: SDP(cfg, secret, transport=httpx.MockTransport(sdp.handler))
    monkeypatch.setattr(sdp_solutions, "SDP", fake_sdp)
    monkeypatch.setattr(app_mod, "SDP", fake_sdp)

    def from_server(cls, sid):
        s = db.one("SELECT * FROM model_servers WHERE id=?", (sid,))
        fake = chat if ":9201" in s["base_url"] else emb
        return cls(s["kind"], s["base_url"], transport=httpx.MockTransport(fake.handler))
    monkeypatch.setattr(llm_mod.LLM, "from_server", classmethod(from_server))

    with TestClient(app) as c:
        assert c.post("/api/login", json={"username": "admin", "password": "test-admin-pw"}, headers=H).status_code == 200
        s_chat = c.post("/api/servers", json={"name": "llama-chat", "kind": "openai", "base_url": "http://gpu:9201/v1"}, headers=H).json()["id"]
        s_emb = c.post("/api/servers", json={"name": "llama-embed", "kind": "openai", "base_url": "http://gpu:9202"}, headers=H).json()["id"]
        cid = c.post("/api/connections", json={"name": "SDP-l", "kind": "sdp", "config": {"base_url": "https://sdp.test"},
                                               "secret": {"api_key": "KEY"}}, headers=H).json()["id"]
        aid = c.post("/api/agents", json={"name": "KB-llama", "type": "sdp_solutions", "config": {
            "connection": cid, "server": s_chat, "model": "anything", "embed_server": s_emb,
            "embed_model": "whatever", "find_gaps": True, "review_existing": True, "new_topic": "General"}}, headers=H).json()["id"]

        t = c.post(f"/api/agents/{aid}/test", headers=H).json()
        assert t["model"]["ok"] and "qwen2.5-14b" in t["model"]["detail"]
        assert t["embed_model"]["ok"] and "nomic-embed" in t["embed_model"]["detail"]

        rid = c.post(f"/api/agents/{aid}/run", headers=H).json()["run_id"]
        for _ in range(200):
            run = c.get(f"/api/runs/{rid}").json()
            if run["status"] != "running":
                break
            time.sleep(0.05)
        assert run["status"] == "ok", run
        assert "1 updates proposed" in run["summary"] and "1 drafts proposed" in run["summary"]
        assert "/v1/chat/completions" in chat.paths and "/v1/embeddings" not in chat.paths
        assert "/v1/embeddings" in emb.paths and "/v1/chat/completions" not in emb.paths
        # Vectors are cached per server, so another server with the same model name can't reuse them.
        keys = {r["model"] for r in db.q("SELECT DISTINCT model FROM embeddings")}
        assert f"{s_emb}:whatever" in keys
