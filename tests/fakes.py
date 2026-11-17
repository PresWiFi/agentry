"""In-memory ServiceDesk Plus and Ollama, served through httpx.MockTransport."""
from __future__ import annotations

import json
import re
from urllib.parse import parse_qs

import httpx


class FakeSDP:
    def __init__(self):
        self.solutions = {
            "101": {"id": "101", "title": "vpn", "description": "<p>restart vpn client it works</p>",
                    "keywords": "", "topic": {"id": "5", "name": "Network"},
                    "last_updated_time": {"value": "1700000000000"}},
            "102": {"id": "102", "title": "How to reset your password",
                    "description": "<h3>Steps</h3><ol><li>Go to the portal</li><li>Click Forgot password</li></ol>",
                    "keywords": "password, reset", "topic": {"id": "6", "name": "Accounts"},
                    "last_updated_time": {"value": "1700000000000"}},
        }
        self.requests = {}
        self.calls = []
        self.next_id = 500

    def add_request(self, rid, subject, resolution, created_ms):
        self.requests[rid] = {"id": rid, "subject": subject, "description": f"<p>{subject}</p>",
                              "status": {"name": "Resolved"}, "created_time": {"value": str(created_ms)},
                              "resolution": {"content": resolution}}

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authtoken") == "KEY"
        path = request.url.path
        self.calls.append((request.method, path))
        if request.method == "GET":
            inp = json.loads(request.url.params.get("input_data") or "{}")
        else:
            inp = json.loads(parse_qs(request.content.decode())["input_data"][0])
        ok = {"status": "success", "status_code": 2000}
        if path == "/api/v3/solutions" and request.method == "GET":
            li = inp.get("list_info", {})
            items = list(self.solutions.values())
            start, n = li.get("start_index", 1), li.get("row_count", 100)
            page = items[start - 1:start - 1 + n]
            return httpx.Response(200, json={"solutions": [{k: v for k, v in s.items() if k != "description"} for s in page],
                                             "list_info": {"has_more_rows": start - 1 + n < len(items),
                                                           "total_count": len(items)}, "response_status": [ok]})
        if path == "/api/v3/solutions" and request.method == "POST":
            sid = str(self.next_id); self.next_id += 1
            s = {"id": sid, **inp["solution"], "last_updated_time": {"value": "1800000000000"}}
            self.solutions[sid] = s
            return httpx.Response(201, json={"solution": s, "response_status": ok})
        m = re.fullmatch(r"/api/v3/solutions/(\w+)", path)
        if m:
            sid = m.group(1)
            if sid not in self.solutions:
                return httpx.Response(404, json={"response_status": {"status": "failed", "status_code": 4000,
                                                                     "messages": [{"message": "Not found"}]}})
            if request.method == "PUT":
                self.solutions[sid].update(inp["solution"])
                self.solutions[sid]["last_updated_time"] = {"value": "1800000000000"}
            return httpx.Response(200, json={"solution": self.solutions[sid], "response_status": ok})
        if path == "/api/v3/requests":
            items = sorted(self.requests.values(), key=lambda r: -int(r["created_time"]["value"]))
            li = inp.get("list_info", {})
            start, n = li.get("start_index", 1), li.get("row_count", 100)
            page = items[start - 1:start - 1 + n]
            return httpx.Response(200, json={"requests": [{k: v for k, v in r.items() if k != "resolution"} for r in page],
                                             "list_info": {"has_more_rows": start - 1 + n < len(items)},
                                             "response_status": [ok]})
        m = re.fullmatch(r"/api/v3/requests/(\w+)", path)
        if m:
            return httpx.Response(200, json={"request": self.requests[m.group(1)], "response_status": ok})
        return httpx.Response(404, json={})


VOCAB = ["vpn", "password", "printer", "toner", "outlook", "email", "paper", "jam"]


def embed(text: str) -> list[float]:
    t = text.lower()
    return [float(t.count(w)) for w in VOCAB] + [0.01]


class FakeOllama:
    def __init__(self):
        self.chats = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        if request.url.path == "/api/chat":
            self.chats.append(body)
            system = body["messages"][0]["content"]
            user = body["messages"][-1]["content"]
            if "review one solution" in system:
                if "Title: vpn" in user:
                    reply = {"verdict": "update", "issues": ["Vague title", "No steps"],
                             "title": "VPN won't connect: restart the VPN client", "keywords": "vpn, remote",
                             "description_html": "<h3>Resolution</h3><ol><li>Restart the VPN client.</li></ol>"
                                                 "<script>alert(1)</script><img src=x onerror=alert(1)>"}
                else:
                    reply = {"verdict": "ok", "issues": []}
            else:
                reply = {"title": "Printer paper jam", "keywords": "printer, jam",
                         "description_html": "<p>Open tray 2 and remove the paper.</p>", "confidence": "medium"}
            return httpx.Response(200, json={"message": {"role": "assistant", "content": "```json\n" + json.dumps(reply) + "\n```"}})
        if request.url.path == "/api/embed":
            return httpx.Response(200, json={"embeddings": [embed(t) for t in body["input"]]})
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "0.13.0"})
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "qwen3:14b", "details": {"parameter_size": "14B"}},
                                                        {"name": "nomic-embed-text:latest"}]})
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": []})
        return httpx.Response(404)
