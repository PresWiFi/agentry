"""Agent types. Each type declares its settings (drives the form in the UI), runs, and applies the
proposals it makes. Register new types in TYPES."""
from __future__ import annotations

import json
import time
from typing import Any, Callable

from .. import crypto, db
from ..llm import LLM


class Cancelled(Exception):
    pass


class AgentType:
    key = ""
    name = ""
    description = ""
    # Field: {key, label, type: text|textarea|number|bool|select|server|model|embed_model|connection,
    #         default, help, options (select), connection_kind (connection), required}
    fields: list[dict] = []

    def defaults(self) -> dict:
        return {f["key"]: f.get("default") for f in self.fields}

    def validate(self, config: dict) -> dict:
        out = self.defaults()
        for f in self.fields:
            k, t = f["key"], f["type"]
            if k not in config:
                continue
            v = config[k]
            if t == "number":
                try:
                    v = float(v) if "." in str(v) else int(v)
                except (TypeError, ValueError):
                    raise ValueError(f"{f['label']} must be a number.")
                if "min" in f and v < f["min"] or "max" in f and v > f["max"]:
                    raise ValueError(f"{f['label']} must be between {f.get('min')} and {f.get('max')}.")
            elif t == "bool":
                v = bool(v)
            elif t in ("server", "connection"):
                v = int(v) if v not in (None, "") else None
            elif t == "select":
                if v not in [o[0] for o in f["options"]]:
                    raise ValueError(f"{f['label']}: pick one of the options.")
            else:
                v = "" if v is None else str(v)
            out[k] = v
        for f in self.fields:
            if f.get("required") and out.get(f["key"]) in (None, ""):
                raise ValueError(f"{f['label']} is required.")
        return out

    async def run(self, ctx: "RunContext") -> str:
        raise NotImplementedError

    async def apply(self, proposal: dict, config: dict) -> str:
        raise NotImplementedError

    async def test(self, config: dict) -> dict:
        return {}


def connection(conn_id: int | None) -> tuple[dict, dict]:
    c = db.one("SELECT * FROM connections WHERE id=?", (conn_id,)) if conn_id else None
    if not c:
        raise ValueError("The agent's connection is missing. Pick one in the agent settings.")
    secret = json.loads(crypto.decrypt(c["secret"]) or "{}")
    return json.loads(c["config"]), secret


class RunContext:
    def __init__(self, agent: dict, run_id: int, agent_type: AgentType, cancel_check: Callable[[], bool]):
        self.agent = agent
        self.agent_id = agent["id"]
        self.run_id = run_id
        self.type = agent_type
        self.config = agent_type.validate(json.loads(agent["config"] or "{}"))
        self._cancel_check = cancel_check
        self._llms: dict[int, LLM] = {}
        self.counts = {"proposals": 0, "applied": 0}

    def log(self, message: str, level: str = "info") -> None:
        db.ex("INSERT INTO run_logs(run_id, at, level, message) VALUES (?,?,?,?)",
              (self.run_id, time.time(), level, message[:4000]))

    def check(self) -> None:
        if self._cancel_check():
            raise Cancelled()

    def llm(self, server_id: int | None) -> LLM:
        if not server_id:
            raise ValueError("No model server is set for this agent.")
        if server_id not in self._llms:
            self._llms[server_id] = LLM.from_server(server_id)
        return self._llms[server_id]

    async def close(self):
        for c in self._llms.values():
            await c.close()

    def state(self, key: str, default: Any = None) -> Any:
        return db.state_get(self.agent_id, key, default)

    def set_state(self, key: str, value: Any) -> None:
        db.state_set(self.agent_id, key, value)

    def pending_targets(self) -> set[str]:
        return {r["target_id"] for r in db.q(
            "SELECT target_id FROM proposals WHERE agent_id=? AND status='pending' AND target_id IS NOT NULL",
            (self.agent_id,))}

    async def propose(self, kind: str, title: str, proposed: dict, *, target_id: str | None = None,
                      rationale: str = "", original: dict | None = None) -> int:
        pid = db.ex("INSERT INTO proposals(agent_id, run_id, kind, target_id, title, rationale, original, proposed, "
                    "status, created_at) VALUES (?,?,?,?,?,?,?,?, 'pending', ?)",
                    (self.agent_id, self.run_id, kind, target_id, title, rationale,
                     json.dumps(original) if original is not None else None, json.dumps(proposed), time.time()))
        self.counts["proposals"] += 1
        if self.config.get("mode") == "auto":
            from ..runner import apply_proposal
            ok, msg = await apply_proposal(pid, "agent (auto mode)")
            self.log(f"Auto-applied proposal #{pid}: {msg}" if ok else f"Proposal #{pid} failed to apply: {msg}",
                     "info" if ok else "error")
            if ok:
                self.counts["applied"] += 1
        else:
            self.log(f"Proposal #{pid} waiting for review: {title}")
        return pid


def _types() -> dict[str, AgentType]:
    from .sdp_solutions import SDPSolutions
    return {t.key: t for t in (SDPSolutions(),)}


TYPES = _types()
