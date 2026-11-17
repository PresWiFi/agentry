"""Keeps ServiceDesk Plus Solutions (knowledge base articles) in shape.

Two jobs, each optional:
- Review: reads existing solutions (oldest unreviewed first), asks the model whether each needs work, and
  proposes a rewrite when it does.
- Gaps: embeds recently resolved requests and existing solutions, finds groups of similar requests that
  no solution covers, and drafts a new solution for each group from how the tickets were resolved.

In review mode every change waits for a person to approve it in Agentry. In auto mode changes go
straight to ServiceDesk Plus.
"""
from __future__ import annotations

import json
import time

from .. import db
from ..sdp import SDP, updated_ms
from ..textutil import clean_html, clip, cosine, html_to_text, mean, parse_json, sha
from . import AgentType, RunContext, connection

DEFAULT_STYLE = """\
- Audience: IT technicians and end users. Plain language, short sentences.
- Structure: a one-line summary of the problem, then "Applies to", "Symptoms", "Cause" (if known),
  "Resolution" as numbered steps, and "Escalate if" when the steps don't work.
- Keep every technical detail that's in the original: commands, paths, settings, links, screenshots.
- Don't invent steps, product versions, or facts. If something is missing or unclear, add a line
  starting with "[VERIFY]" instead of guessing.
- No names, email addresses, phone numbers, or ticket numbers of specific people."""

REVIEW_SYSTEM = """You maintain an IT knowledge base in ManageEngine ServiceDesk Plus.
You review one solution article at a time and decide whether it needs work.

Style guide:
{style}

A solution needs an update when it is unclear, badly structured, missing obvious steps that the article
itself implies, has broken formatting, has a vague title, or has poor keywords. It does NOT need an
update for small wording preferences. When in doubt, answer "ok".

Reply with one JSON object and nothing else:
{{"verdict": "ok" or "update",
  "issues": ["short description of each problem"],
  "title": "improved title (or the same)",
  "keywords": "comma-separated search keywords",
  "description_html": "the full rewritten article as simple HTML (p, h3, ol, ul, li, b, code, pre, a, img, table)"}}
If the verdict is "ok", you may leave title, keywords and description_html empty."""

DRAFT_SYSTEM = """You write IT knowledge base articles for ManageEngine ServiceDesk Plus.
You get several resolved service desk tickets about the same problem. Write ONE reusable solution
article from how technicians actually resolved them.

Style guide:
{style}

Use only steps that appear in the resolutions. If the resolutions disagree or are thin, say so with
"[VERIFY]" lines. Never copy names, emails, phone numbers, hostnames of individual PCs, or ticket numbers.

Reply with one JSON object and nothing else:
{{"title": "clear, searchable title",
  "keywords": "comma-separated search keywords",
  "description_html": "the article as simple HTML (p, h3, ol, ul, li, b, code, pre, a, table)",
  "confidence": "low", "medium" or "high",
  "notes": "anything the reviewer should check"}}"""


class SDPSolutions(AgentType):
    key = "sdp_solutions"
    name = "ServiceDesk Plus Solutions"
    description = "Reviews and rewrites Solution articles, and drafts new ones for recurring tickets with no article."
    fields = [
        {"key": "connection", "label": "ServiceDesk Plus connection", "type": "connection", "connection_kind": "sdp",
         "required": True},
        {"key": "server", "label": "Model server", "type": "server", "required": True},
        {"key": "model", "label": "Chat model", "type": "model", "default": "", "required": True,
         "help": "Writes and reviews articles. 14B+ recommended; see docs/models.md."},
        {"key": "num_ctx", "label": "Context length (tokens)", "type": "number", "default": 16384, "min": 2048,
         "max": 262144, "help": "Ollama only. Long articles need 16k+. More context uses more VRAM."},
        {"key": "mode", "label": "Mode", "type": "select", "default": "review",
         "options": [["review", "Review: changes wait for approval"], ["auto", "Auto: publish changes directly"]]},
        {"key": "style_guide", "label": "Style guide", "type": "textarea", "default": DEFAULT_STYLE,
         "help": "Given to the model for every review and draft."},

        {"key": "review_existing", "label": "Review existing solutions", "type": "bool", "default": True,
         "section": "Review"},
        {"key": "review_max", "label": "Solutions per run", "type": "number", "default": 5, "min": 1, "max": 200},
        {"key": "revisit_days", "label": "Re-review after (days)", "type": "number", "default": 90, "min": 1,
         "max": 3650, "help": "A solution changed in ServiceDesk Plus is re-reviewed sooner."},
        {"key": "review_topics", "label": "Only these topics", "type": "text", "default": "",
         "help": "Comma-separated topic names. Empty = all topics."},

        {"key": "find_gaps", "label": "Draft solutions for recurring tickets", "type": "bool", "default": False,
         "section": "Gaps"},
        {"key": "embed_model", "label": "Embedding model", "type": "embed_model", "default": "",
         "help": "Needed for gap finding, e.g. nomic-embed-text or bge-m3."},
        {"key": "gap_lookback_days", "label": "Look at tickets from the last (days)", "type": "number", "default": 30,
         "min": 1, "max": 365},
        {"key": "gap_statuses", "label": "Ticket statuses", "type": "text", "default": "Resolved, Closed"},
        {"key": "gap_max_requests", "label": "Max tickets per run", "type": "number", "default": 300, "min": 10,
         "max": 5000},
        {"key": "gap_coverage", "label": "Covered if similarity ≥", "type": "number", "default": 0.78, "min": 0.3,
         "max": 0.99, "help": "A ticket this similar to an existing solution counts as covered."},
        {"key": "gap_cluster", "label": "Group tickets if similarity ≥", "type": "number", "default": 0.82,
         "min": 0.3, "max": 0.99},
        {"key": "gap_min_cluster", "label": "Min tickets per gap", "type": "number", "default": 3, "min": 2,
         "max": 50},
        {"key": "gap_max_drafts", "label": "Max drafts per run", "type": "number", "default": 3, "min": 1, "max": 20},
        {"key": "new_topic", "label": "Topic for new solutions", "type": "text", "default": "",
         "help": "Topic name or ID in ServiceDesk Plus."},
    ]

    def validate(self, config: dict) -> dict:
        c = super().validate(config)
        if c["find_gaps"] and not c["embed_model"]:
            raise ValueError("Gap finding needs an embedding model.")
        if c["find_gaps"] and not c["new_topic"]:
            raise ValueError("Gap finding needs a topic for new solutions.")
        return c

    def _sdp(self, config: dict) -> SDP:
        cfg, secret = connection(config["connection"])
        return SDP(cfg, secret)

    async def test(self, config: dict) -> dict:
        sdp = self._sdp(config)
        try:
            return await sdp.test()
        finally:
            await sdp.close()

    # ---------- run ----------

    async def run(self, ctx: RunContext) -> str:
        c = ctx.config
        sdp = self._sdp(c)
        parts = []
        try:
            ctx.log("Loading solutions from ServiceDesk Plus")
            solutions = [s async for s in sdp.list_solutions()]
            ctx.log(f"{len(solutions)} solutions")
            if c["review_existing"]:
                parts.append(await self._review(ctx, sdp, solutions))
            if c["find_gaps"]:
                parts.append(await self._gaps(ctx, sdp, solutions))
        finally:
            await sdp.close()
        return "; ".join(p for p in parts if p) or "Nothing to do (both jobs are off)"

    def _call_model(self, ctx: RunContext):
        c = ctx.config
        llm = ctx.llm(c["server"])

        async def ask(system: str, user: str) -> dict:
            msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
            last = None
            for attempt in range(2):
                raw = await llm.chat(c["model"], msgs, json_mode=True, num_ctx=c["num_ctx"])
                try:
                    return parse_json(raw)
                except ValueError as e:
                    last = e
                    ctx.log(f"Model reply wasn't valid JSON (attempt {attempt + 1}); retrying", "warn")
                    msgs = msgs + [{"role": "assistant", "content": raw[:2000]},
                                   {"role": "user", "content": "That wasn't a valid JSON object. Reply with only the JSON object."}]
            raise ValueError(str(last))
        return ask

    async def _review(self, ctx: RunContext, sdp: SDP, solutions: list[dict]) -> str:
        c = ctx.config
        topics = {t.strip().lower() for t in c["review_topics"].split(",") if t.strip()}
        reviewed: dict = ctx.state("reviewed", {})
        pending = ctx.pending_targets()
        now = time.time()
        due = []
        for s in solutions:
            sid = str(s.get("id"))
            if sid in pending:
                continue
            topic = ((s.get("topic") or {}).get("name") or "").lower()
            if topics and topic not in topics:
                continue
            seen = reviewed.get(sid)
            upd = updated_ms(s)
            if seen and now - seen["at"] < c["revisit_days"] * 86400 and seen.get("updated") == upd:
                continue
            due.append((seen["at"] if seen else 0, upd or 0, s))
        due.sort(key=lambda x: (x[0], x[1]))  # never reviewed first, then least recently reviewed
        batch = [d[2] for d in due[: c["review_max"]]]
        ctx.log(f"Review: {len(due)} solutions due, reviewing {len(batch)}")
        ask = self._call_model(ctx)
        system = REVIEW_SYSTEM.format(style=c["style_guide"])
        max_chars = int(c["num_ctx"] * 2.2)  # leave room for the reply
        proposed = 0
        for s in batch:
            ctx.check()
            sid = str(s["id"])
            try:
                full = await sdp.get_solution(sid)
            except Exception as e:
                ctx.log(f"Solution {sid}: couldn't load ({e})", "error")
                continue
            title = full.get("title") or s.get("title") or ""
            desc = full.get("description") or ""
            if len(desc) > max_chars:
                ctx.log(f"Solution {sid} \"{title}\" is too long for the context length ({len(desc)} chars); skipped",
                        "warn")
                continue
            user = (f"Title: {title}\nTopic: {(full.get('topic') or {}).get('name', '')}\n"
                    f"Keywords: {full.get('keywords') or ''}\n\nArticle HTML:\n{desc}")
            ctx.log(f"Reviewing {sid} \"{title}\"")
            try:
                out = await ask(system, user)
            except Exception as e:
                ctx.log(f"Solution {sid}: model failed: {e}", "error")
                continue
            reviewed[sid] = {"at": time.time(), "updated": updated_ms(full) or updated_ms(s)}
            ctx.set_state("reviewed", reviewed)
            if str(out.get("verdict", "ok")).lower() != "update" or not out.get("description_html"):
                ctx.log(f"Solution {sid}: no changes needed")
                continue
            new = {}
            new_title = str(out.get("title") or "").strip()
            if new_title and new_title != title:
                new["title"] = new_title[:250]
            new_desc = clean_html(str(out["description_html"]))
            if new_desc and html_to_text(new_desc) != html_to_text(desc):
                new["description"] = new_desc
            new_kw = str(out.get("keywords") or "").strip()
            if new_kw and new_kw != (full.get("keywords") or ""):
                new["keywords"] = new_kw[:500]
            if not new:
                ctx.log(f"Solution {sid}: model suggested an update but nothing changed")
                continue
            issues = out.get("issues") or []
            rationale = "\n".join(f"- {i}" for i in issues) if isinstance(issues, list) else str(issues)
            await ctx.propose("update", f"Update: {title}", new, target_id=sid, rationale=rationale,
                              original={"title": title, "description": desc, "keywords": full.get("keywords") or "",
                                        "topic": (full.get("topic") or {}).get("name"),
                                        "_updated": updated_ms(full)})
            proposed += 1
        return f"reviewed {len(batch)}, {proposed} updates proposed"

    async def _embed_cached(self, ctx: RunContext, items: list[tuple[str, str, str]]) -> dict[str, list[float]]:
        """items: (cache key, content hash, text). Returns key -> vector."""
        c = ctx.config
        model = c["embed_model"]
        out, todo = {}, []
        for key, h, text in items:
            row = db.one("SELECT hash, vector FROM embeddings WHERE key=? AND model=?", (key, model))
            if row and row["hash"] == h:
                out[key] = json.loads(row["vector"])
            else:
                todo.append((key, h, text))
        llm = ctx.llm(c["server"])
        for i in range(0, len(todo), 16):
            ctx.check()
            chunk = todo[i:i + 16]
            vecs = await llm.embed(model, [t[2] for t in chunk])
            for (key, h, _), v in zip(chunk, vecs):
                db.ex("INSERT INTO embeddings(key, model, hash, vector) VALUES (?,?,?,?) ON CONFLICT(key, model) "
                      "DO UPDATE SET hash=excluded.hash, vector=excluded.vector", (key, model, h, json.dumps(v)))
                out[key] = v
        return out

    async def _gaps(self, ctx: RunContext, sdp: SDP, solutions: list[dict]) -> str:
        c = ctx.config
        conn_id = c["connection"]
        ctx.log("Gaps: embedding solutions")
        sol_items = []
        for s in solutions:
            sid = str(s["id"])
            upd = str(updated_ms(s) or "")
            key = f"c{conn_id}:sol:{sid}"
            cached = db.one("SELECT hash FROM embeddings WHERE key=? AND model=?", (key, c["embed_model"]))
            if cached and cached["hash"] == sha(upd):
                sol_items.append((key, sha(upd), ""))
                continue
            full = await sdp.get_solution(sid)
            text = clip(f"{full.get('title', '')}\n{full.get('keywords') or ''}\n"
                        f"{html_to_text(full.get('description'))}", 3000)
            sol_items.append((key, sha(upd), text))
        # Pending drafts count as coverage too, so the same gap isn't drafted twice.
        for p in db.q("SELECT id, proposed FROM proposals WHERE agent_id=? AND kind='create' AND status='pending'",
                      (ctx.agent_id,)):
            d = json.loads(p["proposed"])
            text = clip(f"{d.get('title', '')}\n{d.get('keywords', '')}\n{html_to_text(d.get('description'))}", 3000)
            sol_items.append((f"proposal:{p['id']}", sha(text), text))
        sol_vecs = list((await self._embed_cached(ctx, sol_items)).values())

        since = int((time.time() - c["gap_lookback_days"] * 86400) * 1000)
        statuses = [s.strip() for s in c["gap_statuses"].split(",") if s.strip()]
        seen = set(ctx.state("gap_seen", []))
        ctx.log(f"Gaps: loading tickets from the last {c['gap_lookback_days']} days ({', '.join(statuses) or 'any status'})")
        reqs = [r async for r in sdp.recent_requests(statuses, since, c["gap_max_requests"])]
        reqs = [r for r in reqs if str(r.get("id")) not in seen]
        ctx.log(f"Gaps: {len(reqs)} new tickets to check")

        req_items, texts = [], {}
        for r in reqs:
            ctx.check()
            rid = str(r["id"])
            key = f"c{conn_id}:req:{rid}"
            if db.one("SELECT 1 FROM embeddings WHERE key=? AND model=?", (key, c["embed_model"])):
                req_items.append((key, "closed", ""))
                continue
            text = await self._request_text(sdp, rid, r)
            texts[rid] = text
            req_items.append((key, "closed", text))
        req_vecs = await self._embed_cached(ctx, req_items)

        uncovered = []
        for r in reqs:
            v = req_vecs.get(f"c{conn_id}:req:{r['id']}")
            if v is None:
                continue
            best = max((cosine(v, s) for s in sol_vecs), default=0.0)
            if best < c["gap_coverage"]:
                uncovered.append((str(r["id"]), r.get("subject") or "", v))
        ctx.log(f"Gaps: {len(uncovered)} tickets not covered by an existing solution")

        clusters: list[dict] = []
        for rid, subject, v in uncovered:
            for cl in clusters:
                if cosine(v, cl["centroid"]) >= c["gap_cluster"]:
                    cl["members"].append((rid, subject, v))
                    cl["centroid"] = mean([m[2] for m in cl["members"]])
                    break
            else:
                clusters.append({"members": [(rid, subject, v)], "centroid": v})
        big = sorted([cl for cl in clusters if len(cl["members"]) >= c["gap_min_cluster"]],
                     key=lambda cl: -len(cl["members"]))
        ctx.log(f"Gaps: {len(big)} groups of {c['gap_min_cluster']}+ similar tickets")

        ask = self._call_model(ctx)
        system = DRAFT_SYSTEM.format(style=c["style_guide"])
        drafted = 0
        for cl in big[: c["gap_max_drafts"]]:
            ctx.check()
            members = cl["members"]
            sample = members[:8]
            blocks = []
            for i, (rid, subject, _) in enumerate(sample, 1):
                text = texts.get(rid) or await self._request_text(sdp, rid, {"subject": subject})
                blocks.append(f"--- Ticket {i} ---\n{text}")
            ctx.log(f"Drafting a solution from {len(members)} tickets (e.g. \"{sample[0][1]}\")")
            try:
                out = await ask(system, "\n\n".join(blocks))
            except Exception as e:
                ctx.log(f"Draft failed: {e}", "error")
                continue
            desc = clean_html(str(out.get("description_html") or ""))
            title = str(out.get("title") or "").strip()[:250]
            if not desc or not title:
                ctx.log("Draft came back empty; skipped", "warn")
                continue
            rationale = (f"{len(members)} similar tickets in the last {c['gap_lookback_days']} days have no matching "
                         f"solution. Model confidence: {out.get('confidence', '?')}.")
            if out.get("notes"):
                rationale += f"\nNotes: {out['notes']}"
            rationale += "\nTickets: " + ", ".join(f"#{m[0]}" for m in members[:30])
            await ctx.propose("create", f"New: {title}",
                              {"title": title, "description": desc, "keywords": str(out.get("keywords") or "")[:500],
                               "topic": c["new_topic"]},
                              rationale=rationale)
            seen.update(m[0] for m in members)
            drafted += 1
        ctx.set_state("gap_seen", sorted(seen)[-20000:])
        return f"{len(uncovered)} uncovered tickets, {len(big)} gaps, {drafted} drafts proposed"

    async def _request_text(self, sdp: SDP, rid: str, listed: dict) -> str:
        try:
            req = await sdp.get_request(rid)
        except Exception:
            req = listed
        res = (req.get("resolution") or {}).get("content") if isinstance(req.get("resolution"), dict) else None
        if res is None:
            res = await sdp.get_resolution(rid)
        cat = " / ".join(x for x in [(req.get("category") or {}).get("name"),
                                     (req.get("subcategory") or {}).get("name")] if x)
        return clip(f"Subject: {req.get('subject') or listed.get('subject', '')}\n"
                    f"Category: {cat}\n"
                    f"Description: {clip(html_to_text(req.get('description')), 800)}\n"
                    f"Resolution: {clip(html_to_text(res), 1500)}", 2600)

    # ---------- apply ----------

    async def apply(self, proposal: dict, config: dict) -> str:
        sdp = self._sdp(config)
        try:
            new = json.loads(proposal["proposed"])
            if proposal["kind"] == "update":
                sid = proposal["target_id"]
                orig = json.loads(proposal["original"] or "{}")
                current = await sdp.get_solution(sid)
                if orig.get("_updated") and updated_ms(current) and updated_ms(current) != orig["_updated"]:
                    raise RuntimeError("The solution was changed in ServiceDesk Plus after this proposal was made. "
                                       "Reject it and run the agent again.")
                fields = {k: new[k] for k in ("title", "description", "keywords") if k in new}
                await sdp.update_solution(sid, fields)
                return f"Updated solution {sid}"
            topic = str(new.get("topic") or config.get("new_topic") or "").strip()
            fields = {k: new[k] for k in ("title", "description", "keywords") if new.get(k)}
            if topic:
                fields["topic"] = {"id": topic} if topic.isdigit() else {"name": topic}
            created = await sdp.create_solution(fields)
            return f"Created solution {created.get('id', '?')}"
        finally:
            await sdp.close()

