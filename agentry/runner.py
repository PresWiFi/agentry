"""Runs agents: on their schedule, or on demand. One run per agent at a time."""
from __future__ import annotations

import asyncio
import json
import logging
import time
import traceback

from . import db
from .agents import TYPES, Cancelled, RunContext
from .config import settings

log = logging.getLogger("agentry.runner")

_tasks: dict[int, asyncio.Task] = {}       # agent_id -> running task
_cancel: set[int] = set()                   # run ids asked to stop


def running_agents() -> set[int]:
    return {a for a, t in _tasks.items() if not t.done()}


def schedule_next(agent_id: int, minutes: int, enabled: bool, from_time: float | None = None) -> None:
    nxt = (from_time or time.time()) + minutes * 60 if enabled and minutes > 0 else None
    db.ex("UPDATE agents SET next_run_at=? WHERE id=?", (nxt, agent_id))


def start_run(agent_id: int, trigger: str, user: str | None = None) -> int:
    if agent_id in running_agents():
        raise RuntimeError("This agent is already running.")
    agent = db.one("SELECT * FROM agents WHERE id=?", (agent_id,))
    if not agent:
        raise RuntimeError("No such agent.")
    if agent["type"] not in TYPES:
        raise RuntimeError(f"Unknown agent type {agent['type']}.")
    run_id = db.ex("INSERT INTO runs(agent_id, trigger, triggered_by, status, started_at) VALUES (?,?,?,?,?)",
                   (agent_id, trigger, user, "running", time.time()))
    _tasks[agent_id] = asyncio.create_task(_run(agent, run_id))
    return run_id


def cancel_run(run_id: int) -> None:
    _cancel.add(run_id)


async def _run(agent: dict, run_id: int) -> None:
    atype = TYPES[agent["type"]]
    ctx = None
    status, summary = "ok", ""
    try:
        ctx = RunContext(agent, run_id, atype, lambda: run_id in _cancel)
        ctx.log(f"Run started ({atype.name})")
        summary = await atype.run(ctx) or ""
    except Cancelled:
        status, summary = "cancelled", "Stopped by a user"
    except Exception as e:
        status, summary = "error", f"{e.__class__.__name__}: {e}"
        log.error("agent %s run %s failed\n%s", agent["id"], run_id, traceback.format_exc())
    finally:
        if ctx:
            if status != "ok":
                ctx.log(summary, "error" if status == "error" else "warn")
            await ctx.close()
        _cancel.discard(run_id)
        db.ex("UPDATE runs SET status=?, finished_at=?, summary=? WHERE id=?",
              (status, time.time(), summary[:1000], run_id))


async def apply_proposal(pid: int, user: str) -> tuple[bool, str]:
    p = db.one("SELECT * FROM proposals WHERE id=?", (pid,))
    if not p:
        return False, "No such proposal."
    if p["status"] not in ("pending", "failed"):
        return False, f"Proposal is already {p['status']}."
    agent = db.one("SELECT * FROM agents WHERE id=?", (p["agent_id"],))
    if not agent or agent["type"] not in TYPES:
        return False, "The agent that made this proposal no longer exists."
    atype = TYPES[agent["type"]]
    try:
        config = atype.validate(json.loads(agent["config"] or "{}"))
        msg = await atype.apply(p, config)
        db.ex("UPDATE proposals SET status='applied', decided_by=?, decided_at=?, result=? WHERE id=?",
              (user, time.time(), msg, pid))
        db.audit(user, "proposal.apply", {"id": pid, "result": msg})
        return True, msg
    except Exception as e:
        msg = f"{e.__class__.__name__}: {e}" if not isinstance(e, RuntimeError) else str(e)
        db.ex("UPDATE proposals SET status='failed', decided_by=?, decided_at=?, result=? WHERE id=?",
              (user, time.time(), msg[:1000], pid))
        db.audit(user, "proposal.apply_failed", {"id": pid, "error": msg[:500]})
        return False, msg


async def scheduler() -> None:
    while True:
        try:
            now = time.time()
            for a in db.q("SELECT * FROM agents WHERE enabled=1 AND schedule_minutes>0 AND next_run_at IS NOT NULL "
                          "AND next_run_at<=?", (now,)):
                schedule_next(a["id"], a["schedule_minutes"], True, now)
                if a["id"] in running_agents():
                    continue
                try:
                    start_run(a["id"], "schedule")
                except RuntimeError as e:
                    log.warning("scheduled run of agent %s skipped: %s", a["id"], e)
        except Exception:
            log.exception("scheduler tick failed")
        await asyncio.sleep(settings.scheduler_tick)
