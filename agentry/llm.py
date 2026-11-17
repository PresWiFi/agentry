"""Client for the model server on the GPU box: Ollama's native API, or any OpenAI-compatible server
(vLLM, llama.cpp server, LM Studio, LocalAI, TGI)."""
from __future__ import annotations

import time

import httpx

from . import crypto, db

TIMEOUT = httpx.Timeout(connect=10, read=600, write=60, pool=10)


class LLMError(Exception):
    pass


class LLM:
    def __init__(self, kind: str, base_url: str, api_key: str = "", transport: httpx.AsyncBaseTransport | None = None):
        self.kind = kind
        self.base = base_url.rstrip("/")
        if self.kind == "openai" and self.base.endswith("/v1"):
            self.base = self.base[:-3]
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self.http = httpx.AsyncClient(timeout=TIMEOUT, headers=headers, transport=transport)

    @classmethod
    def from_server(cls, server_id: int) -> "LLM":
        s = db.one("SELECT * FROM model_servers WHERE id=?", (server_id,))
        if not s:
            raise LLMError(f"Model server {server_id} doesn't exist.")
        return cls(s["kind"], s["base_url"], crypto.decrypt(s["api_key"]))

    async def close(self):
        await self.http.aclose()

    async def _post(self, path: str, body: dict) -> dict:
        try:
            r = await self.http.post(self.base + path, json=body)
        except httpx.HTTPError as e:
            raise LLMError(f"Can't reach the model server at {self.base}: {e.__class__.__name__}: {e}")
        if r.status_code >= 400:
            raise LLMError(f"Model server answered HTTP {r.status_code}: {r.text[:300]}")
        return r.json()

    async def chat(self, model: str, messages: list[dict], *, json_mode: bool = False, temperature: float = 0.2,
                   num_ctx: int | None = None) -> str:
        if self.kind == "ollama":
            body: dict = {"model": model, "messages": messages, "stream": False,
                          "options": {"temperature": temperature}}
            if num_ctx:
                body["options"]["num_ctx"] = num_ctx
            if json_mode:
                body["format"] = "json"
            data = await self._post("/api/chat", body)
            return (data.get("message") or {}).get("content", "")
        body = {"model": model, "messages": messages, "temperature": temperature}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        data = await self._post("/v1/chat/completions", body)
        try:
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError):
            raise LLMError(f"Unexpected reply from the model server: {str(data)[:300]}")

    async def embed(self, model: str, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if self.kind == "ollama":
            data = await self._post("/api/embed", {"model": model, "input": texts})
            vecs = data.get("embeddings")
        else:
            data = await self._post("/v1/embeddings", {"model": model, "input": texts})
            vecs = [d["embedding"] for d in sorted(data.get("data", []), key=lambda d: d.get("index", 0))]
        if not vecs or len(vecs) != len(texts):
            raise LLMError("The model server returned the wrong number of embeddings.")
        return vecs

    async def status(self) -> dict:
        """Health, available models, and (Ollama) what's loaded in VRAM."""
        out: dict = {"ok": False, "models": [], "loaded": [], "version": None, "latency_ms": None, "error": None}
        t = time.perf_counter()
        try:
            if self.kind == "ollama":
                v = await self.http.get(self.base + "/api/version", timeout=5)
                v.raise_for_status()
                out["version"] = v.json().get("version")
                tags = (await self.http.get(self.base + "/api/tags", timeout=10)).json()
                out["models"] = [{"name": m["name"], "size": m.get("size"),
                                  "params": (m.get("details") or {}).get("parameter_size"),
                                  "quant": (m.get("details") or {}).get("quantization_level")}
                                 for m in tags.get("models", [])]
                ps = (await self.http.get(self.base + "/api/ps", timeout=10)).json()
                out["loaded"] = [{"name": m["name"], "size": m.get("size"), "size_vram": m.get("size_vram"),
                                  "context": m.get("context_length"), "expires_at": m.get("expires_at")}
                                 for m in ps.get("models", [])]
            else:
                r = await self.http.get(self.base + "/v1/models", timeout=10)
                r.raise_for_status()
                out["models"] = [{"name": m["id"]} for m in r.json().get("data", [])]
            out["ok"] = True
        except (httpx.HTTPError, ValueError, KeyError) as e:
            out["error"] = f"{e.__class__.__name__}: {e}"
        out["latency_ms"] = round((time.perf_counter() - t) * 1000)
        return out
