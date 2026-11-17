from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import __version__, auth, crypto, db, prefs, runner
from .agents import TYPES
from .config import settings
from .llm import LLM
from .middleware import DynamicProxyHeaders, SecureCookies
from . import sdp as sdp_mod
from .sdp import SDP

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("agentry")
STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.conn()
    prefs.import_env()
    auth.prepare_setup()
    task = asyncio.create_task(runner.scheduler())
    yield
    task.cancel()


app = FastAPI(title="Agentry", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None)


@app.middleware("http")
async def csrf(request: Request, call_next):
    # Writes must carry a custom header, which a cross-site form or image can't send.
    if request.url.path.startswith("/api/") and request.method not in ("GET", "HEAD", "OPTIONS") \
            and request.headers.get("x-agentry") != "1":
        return JSONResponse({"detail": "Missing X-Agentry header."}, status_code=403)
    resp = await call_next(request)
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    return resp


# Last added runs first: proxy headers fix the scheme, then cookies get Secure on HTTPS.
app.add_middleware(SessionMiddleware, secret_key=settings.resolve_secret(), session_cookie="agentry_session",
                   max_age=12 * 3600, same_site="lax")
app.add_middleware(SecureCookies)
app.add_middleware(DynamicProxyHeaders)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/healthz")
def healthz():
    return {"ok": True, "version": __version__}


# ---------- auth ----------

def _login_options() -> dict:
    p = prefs.all()
    return {"local": p["local_login"] and bool(db.one("SELECT 1 FROM users WHERE local=1")),
            "oidc": prefs.oidc_enabled(), "oidc_label": p["oidc_label"] or "Sign in with SSO"}


@app.get("/api/me")
def me(request: Request):
    return {"user": auth.current_user(request), "version": __version__, "setup": auth.needs_setup(),
            "login": _login_options()}


@app.post("/api/setup")
def setup(request: Request, body: dict = Body(...)):
    ip = request.client.host if request.client else "?"
    if not auth.needs_setup():
        raise HTTPException(409, "Setup is already done.")
    if auth.rate_limited(ip):
        raise HTTPException(429, "Too many attempts. Wait a few minutes.")
    if not auth.setup_code or not secrets.compare_digest(str(body.get("code", "")).strip().lower(), auth.setup_code):
        auth.note_failure(ip)
        raise HTTPException(401, "Wrong setup code. It's in the Agentry log (docker logs agentry).")
    uid = auth.create_local_user(str(body.get("username") or "admin"), str(body.get("password") or ""), "admin",
                                 str(body.get("name") or ""))
    auth.setup_code = None
    db.ex("UPDATE users SET last_login=? WHERE id=?", (time.time(), uid))
    user = db.one("SELECT * FROM users WHERE id=?", (uid,))
    auth.start_session(request, user)
    db.audit(user["username"], "setup.admin_created")
    return {"ok": True}


@app.post("/api/login")
def login(request: Request, body: dict = Body(...)):
    ip = request.client.host if request.client else "?"
    if not prefs.get("local_login"):
        raise HTTPException(403, "Password sign-in is turned off. Use SSO.")
    if auth.rate_limited(ip):
        raise HTTPException(429, "Too many attempts. Wait a few minutes.")
    u = db.one("SELECT * FROM users WHERE username=? AND local=1", (str(body.get("username", "")),))
    if not u or not auth.check_password(str(body.get("password", "")), u["password_hash"]):
        auth.note_failure(ip)
        raise HTTPException(401, "Wrong username or password.")
    if auth.ROLES.get(u["role"], 0) < 1:
        raise HTTPException(403, "This account has no access. Ask an admin.")
    auth.start_session(request, u)
    db.ex("UPDATE users SET last_login=? WHERE id=?", (time.time(), u["id"]))
    db.audit(u["username"], "login.local")
    return {"ok": True}


@app.post("/api/logout")
def logout(request: Request):
    request.session.clear()
    return {"ok": True}


@app.post("/api/me/password")
def change_my_password(request: Request, body: dict = Body(...), u=auth.need("viewer")):
    row = db.one("SELECT * FROM users WHERE id=?", (u["id"],))
    if not row["local"]:
        raise HTTPException(400, "SSO accounts change their password at the identity provider.")
    if not auth.check_password(str(body.get("current", "")), row["password_hash"]):
        raise HTTPException(401, "The current password is wrong.")
    auth.validate_password(str(body.get("new", "")))
    db.ex("UPDATE users SET password_hash=? WHERE id=?", (auth.hash_password(str(body["new"])), u["id"]))
    auth.start_session(request, row)  # keep this session; others are signed out
    db.audit(u["username"], "user.password", {"id": u["id"]})
    return {"ok": True}


@app.get("/auth/oidc/login")
async def oidc_login(request: Request):
    client = auth.oidc_client()
    if not client:
        return RedirectResponse("/?error=" + quote("SSO isn't set up. An admin can set it up under Settings."))
    try:
        return await client.authorize_redirect(request, auth.redirect_uri(request))
    except Exception as e:
        log.warning("OIDC start failed: %s: %s", e.__class__.__name__, e)
        return RedirectResponse("/?error=" + quote(f"Can't reach the identity provider ({e.__class__.__name__})."))


@app.get("/auth/oidc/callback", name="oidc_callback")
async def oidc_callback(request: Request):
    client = auth.oidc_client()
    if not client:
        return RedirectResponse("/?error=" + quote("SSO isn't set up."))
    try:
        token = await client.authorize_access_token(request)
    except auth.OAuthError as e:
        return RedirectResponse("/?error=" + quote(f"Sign-in failed: {e.error} {e.description or ''}".strip()))
    except Exception as e:  # provider unreachable, token endpoint error, bad ID token
        log.warning("OIDC callback failed: %s: %s", e.__class__.__name__, e)
        return RedirectResponse("/?error=" + quote(f"Sign-in failed: {e.__class__.__name__}. See the Agentry log."))
    claims = dict(token.get("userinfo") or {})
    if prefs.groups_mapped() and prefs.get("oidc_groups_claim") not in claims:
        # Some providers only put groups in the userinfo endpoint.
        try:
            claims.update(await client.userinfo(token=token))
        except Exception:
            pass
    try:
        user = auth.upsert_sso_user(claims)
    except HTTPException as e:
        return RedirectResponse("/?error=" + quote(e.detail))
    auth.start_session(request, user)
    db.audit(user["username"], "login.oidc", {"email": user["email"], "role": user["role"]})
    return RedirectResponse("/")


# ---------- settings ----------

@app.get("/api/settings")
def get_settings(request: Request, u=auth.need("admin")):
    return {**prefs.public(), "redirect_uri": auth.redirect_uri(request),
            "secret_source": settings.secret_source, "data_dir": str(settings.data_dir),
            "sso_admins": db.one("SELECT COUNT(*) n FROM users WHERE local=0 AND role='admin'")["n"]}


@app.put("/api/settings")
async def put_settings(body: dict = Body(...), u=auth.need("admin")):
    changes = {k: v for k, v in body.items() if k in prefs.SPEC}
    if "oidc_client_secret" in changes and not changes["oidc_client_secret"]:
        del changes["oidc_client_secret"]  # blank = keep
    if body.get("clear_oidc_client_secret"):
        changes["oidc_client_secret"] = ""
    url = str(changes.get("public_url", "") or "").strip()
    if url and not url.startswith(("http://", "https://")):
        raise HTTPException(400, "The public URL must start with http:// or https://.")
    if changes.get("oidc_default_role", "none") not in prefs.ROLES:
        raise HTTPException(400, "Unknown default role.")
    new_issuer = str(changes.get("oidc_issuer", prefs.get("oidc_issuer")) or "").strip()
    if new_issuer and new_issuer.rstrip("/") != prefs.get("oidc_issuer"):
        try:
            await auth.check_issuer(new_issuer)
        except ValueError as e:
            raise HTTPException(400, str(e))
    if "local_login" in changes and not prefs._coerce("local_login", changes["local_login"]):
        will_have_sso = bool(new_issuer and changes.get("oidc_client_id", prefs.get("oidc_client_id")))
        sso_admin = db.one("SELECT 1 FROM users WHERE local=0 AND role='admin'")
        if not (will_have_sso and sso_admin):
            raise HTTPException(400, "Before turning password sign-in off, set up SSO and sign in once with an SSO "
                                     "account that has the admin role.")
    local_after = prefs._coerce("local_login", changes.get("local_login", prefs.get("local_login")))
    sso_after = bool(new_issuer and str(changes.get("oidc_client_id", prefs.get("oidc_client_id")) or "").strip())
    if not local_after and not sso_after:
        raise HTTPException(400, "Password sign-in is off, so SSO can't be removed. Turn password sign-in on first.")
    prefs.update(changes)
    db.audit(u["username"], "settings.update", {"keys": sorted(changes)})
    return {"ok": True}


@app.post("/api/settings/test-oidc")
async def test_oidc(body: dict = Body(default={}), u=auth.need("admin")):
    issuer = str(body.get("issuer") or prefs.get("oidc_issuer") or "")
    if not issuer:
        raise HTTPException(400, "Enter the issuer first.")
    try:
        d = await auth.check_issuer(issuer)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "issuer": d["issuer"], "authorization_endpoint": d["authorization_endpoint"],
            "scopes_supported": d.get("scopes_supported"), "claims_supported": d.get("claims_supported")}


# ---------- overview ----------

@app.get("/api/overview")
def overview(u=auth.need("viewer")):
    running = runner.running_agents()
    agents = db.q("SELECT id, name, type, enabled, schedule_minutes, next_run_at FROM agents ORDER BY name")
    for a in agents:
        a["running"] = a["id"] in running
        a["last_run"] = db.one("SELECT id, status, started_at, finished_at, summary FROM runs WHERE agent_id=? "
                               "ORDER BY id DESC LIMIT 1", (a["id"],))
    return {"agents": agents,
            "pending": db.one("SELECT COUNT(*) n FROM proposals WHERE status='pending'")["n"],
            "servers": db.q("SELECT id, name, kind, base_url FROM model_servers ORDER BY name"),
            "runs": db.q("SELECT r.*, a.name agent_name FROM runs r JOIN agents a ON a.id=r.agent_id "
                         "ORDER BY r.id DESC LIMIT 10")}


# ---------- model servers ----------

def _server_out(s: dict) -> dict:
    return {k: s[k] for k in ("id", "name", "kind", "base_url", "created_at")} | {"has_key": bool(s["api_key"])}


@app.get("/api/servers")
def servers(u=auth.need("viewer")):
    return [_server_out(s) for s in db.q("SELECT * FROM model_servers ORDER BY name")]


def _server_body(body: dict) -> tuple[str, str, str]:
    name = str(body.get("name") or "").strip()
    kind = body.get("kind")
    url = str(body.get("base_url") or "").strip().rstrip("/")
    if not name or kind not in ("ollama", "openai") or not url.startswith(("http://", "https://")):
        raise HTTPException(400, "Give a name, a kind (ollama or openai), and an http(s) URL.")
    return name, kind, url


@app.post("/api/servers")
def add_server(body: dict = Body(...), u=auth.need("admin")):
    name, kind, url = _server_body(body)
    sid = db.ex("INSERT INTO model_servers(name, kind, base_url, api_key, created_at) VALUES (?,?,?,?,?)",
                (name, kind, url, crypto.encrypt(str(body.get("api_key") or "")), time.time()))
    db.audit(u["username"], "server.add", {"id": sid, "name": name, "url": url})
    return {"id": sid}


@app.put("/api/servers/{sid}")
def edit_server(sid: int, body: dict = Body(...), u=auth.need("admin")):
    name, kind, url = _server_body(body)
    if body.get("api_key") is not None and body.get("api_key") != "":
        db.ex("UPDATE model_servers SET api_key=? WHERE id=?", (crypto.encrypt(str(body["api_key"])), sid))
    if body.get("clear_key"):
        db.ex("UPDATE model_servers SET api_key='' WHERE id=?", (sid,))
    db.ex("UPDATE model_servers SET name=?, kind=?, base_url=? WHERE id=?", (name, kind, url, sid))
    db.audit(u["username"], "server.edit", {"id": sid})
    return {"ok": True}


@app.delete("/api/servers/{sid}")
def delete_server(sid: int, u=auth.need("admin")):
    db.ex("DELETE FROM model_servers WHERE id=?", (sid,))
    db.audit(u["username"], "server.delete", {"id": sid})
    return {"ok": True}


@app.get("/api/servers/{sid}/status")
async def server_status(sid: int, u=auth.need("viewer")):
    try:
        llm = LLM.from_server(sid)
    except Exception as e:
        raise HTTPException(404, str(e))
    try:
        return await llm.status()
    finally:
        await llm.close()


# ---------- connections ----------

CONN_SECRETS = {"sdp": ["api_key", "client_id", "client_secret", "refresh_token"]}


def _conn_out(c: dict) -> dict:
    secret = json.loads(crypto.decrypt(c["secret"]) or "{}")
    return {"id": c["id"], "name": c["name"], "kind": c["kind"], "config": json.loads(c["config"]),
            "secrets_set": sorted(k for k, v in secret.items() if v)}


async def _conn_body(body: dict, old_secret: dict | None = None) -> tuple[str, str, dict, dict]:
    name = str(body.get("name") or "").strip()
    kind = body.get("kind")
    if not name or kind not in CONN_SECRETS:
        raise HTTPException(400, "Give a name and a kind.")
    cfg = body.get("config") or {}
    if kind == "sdp":
        edition = cfg.get("edition", "onprem")
        url = str(cfg.get("base_url") or "").strip().rstrip("/")
        if edition not in ("onprem", "cloud") or not url.startswith(("http://", "https://")):
            raise HTTPException(400, "Pick the edition and give the ServiceDesk Plus URL.")
        if edition == "cloud":
            url = sdp_mod.cloud_base(url)
        cfg = {"edition": edition, "base_url": url, "verify_tls": bool(cfg.get("verify_tls", True)),
               "accounts_url": str(cfg.get("accounts_url") or "").strip().rstrip("/")}
    secret = dict(old_secret or {})
    for k in CONN_SECRETS[kind]:
        v = (body.get("secret") or {}).get(k)
        if v:
            secret[k] = str(v).strip()
    code = str((body.get("secret") or {}).get("grant_code") or "").strip()
    if kind == "sdp" and code:
        if cfg["edition"] != "cloud":
            raise HTTPException(400, "Grant codes are for ServiceDesk Plus Cloud.")
        if not (secret.get("client_id") and secret.get("client_secret")):
            raise HTTPException(400, "Enter the client ID and secret along with the grant code.")
        try:
            secret["refresh_token"] = await sdp_mod.exchange_grant(cfg["accounts_url"], secret["client_id"],
                                                                   secret["client_secret"], code)
        except sdp_mod.SDPError as e:
            raise HTTPException(400, str(e))
    if kind == "sdp" and cfg["edition"] == "cloud" and not secret.get("refresh_token") and \
            secret.get("client_id"):
        raise HTTPException(400, "Paste a grant code (or a refresh token) so Agentry can get access.")
    return name, kind, cfg, secret


@app.get("/api/connections")
def connections(u=auth.need("viewer")):
    return [_conn_out(c) for c in db.q("SELECT * FROM connections ORDER BY name")]


@app.post("/api/connections")
async def add_connection(body: dict = Body(...), u=auth.need("admin")):
    name, kind, cfg, secret = await _conn_body(body)
    cid = db.ex("INSERT INTO connections(name, kind, config, secret, created_at) VALUES (?,?,?,?,?)",
                (name, kind, json.dumps(cfg), crypto.encrypt(json.dumps(secret)), time.time()))
    db.audit(u["username"], "connection.add", {"id": cid, "name": name})
    return {"id": cid}


@app.put("/api/connections/{cid}")
async def edit_connection(cid: int, body: dict = Body(...), u=auth.need("admin")):
    old = db.one("SELECT * FROM connections WHERE id=?", (cid,))
    if not old:
        raise HTTPException(404, "No such connection.")
    name, kind, cfg, secret = await _conn_body(body, json.loads(crypto.decrypt(old["secret"]) or "{}"))
    db.ex("UPDATE connections SET name=?, kind=?, config=?, secret=? WHERE id=?",
          (name, kind, json.dumps(cfg), crypto.encrypt(json.dumps(secret)), cid))
    db.audit(u["username"], "connection.edit", {"id": cid})
    return {"ok": True}


@app.delete("/api/connections/{cid}")
def delete_connection(cid: int, u=auth.need("admin")):
    db.ex("DELETE FROM connections WHERE id=?", (cid,))
    db.audit(u["username"], "connection.delete", {"id": cid})
    return {"ok": True}


@app.post("/api/connections/{cid}/test")
async def test_connection(cid: int, u=auth.need("admin")):
    c = db.one("SELECT * FROM connections WHERE id=?", (cid,))
    if not c:
        raise HTTPException(404, "No such connection.")
    sdp = SDP(json.loads(c["config"]), json.loads(crypto.decrypt(c["secret"]) or "{}"))
    try:
        return {"ok": True, **(await sdp.test())}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        await sdp.close()


# ---------- agents ----------

@app.get("/api/agent-types")
def agent_types(u=auth.need("viewer")):
    return [{"key": t.key, "name": t.name, "description": t.description, "fields": t.fields,
             "defaults": t.defaults()} for t in TYPES.values()]


def _agent_out(a: dict) -> dict:
    out = dict(a)
    out["config"] = json.loads(a["config"] or "{}")
    out["running"] = a["id"] in runner.running_agents()
    return out


@app.get("/api/agents")
def agents(u=auth.need("viewer")):
    return [_agent_out(a) for a in db.q("SELECT * FROM agents ORDER BY name")]


@app.get("/api/agents/{aid}")
def agent(aid: int, u=auth.need("viewer")):
    a = db.one("SELECT * FROM agents WHERE id=?", (aid,))
    if not a:
        raise HTTPException(404, "No such agent.")
    return _agent_out(a)


def _agent_body(body: dict) -> tuple[str, str, bool, int, dict]:
    name = str(body.get("name") or "").strip()
    atype = body.get("type")
    if not name or atype not in TYPES:
        raise HTTPException(400, "Give a name and an agent type.")
    try:
        minutes = max(0, int(body.get("schedule_minutes") or 0))
        cfg = TYPES[atype].validate(body.get("config") or {})
    except ValueError as e:
        raise HTTPException(400, str(e))
    return name, atype, bool(body.get("enabled")), minutes, cfg


@app.post("/api/agents")
def add_agent(body: dict = Body(...), u=auth.need("admin")):
    name, atype, enabled, minutes, cfg = _agent_body(body)
    now = time.time()
    aid = db.ex("INSERT INTO agents(name, type, enabled, schedule_minutes, config, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?)", (name, atype, int(enabled), minutes, json.dumps(cfg), now, now))
    runner.schedule_next(aid, minutes, enabled)
    db.audit(u["username"], "agent.add", {"id": aid, "name": name})
    return {"id": aid}


@app.put("/api/agents/{aid}")
def edit_agent(aid: int, body: dict = Body(...), u=auth.need("admin")):
    old = db.one("SELECT * FROM agents WHERE id=?", (aid,))
    if not old:
        raise HTTPException(404, "No such agent.")
    name, atype, enabled, minutes, cfg = _agent_body(body)
    db.ex("UPDATE agents SET name=?, type=?, enabled=?, schedule_minutes=?, config=?, updated_at=? WHERE id=?",
          (name, atype, int(enabled), minutes, json.dumps(cfg), time.time(), aid))
    if enabled != bool(old["enabled"]) or minutes != old["schedule_minutes"] or not old["next_run_at"]:
        runner.schedule_next(aid, minutes, enabled)
    db.audit(u["username"], "agent.edit", {"id": aid, "enabled": enabled, "mode": cfg.get("mode")})
    return {"ok": True}


@app.delete("/api/agents/{aid}")
def delete_agent(aid: int, u=auth.need("admin")):
    if aid in runner.running_agents():
        raise HTTPException(409, "Stop the agent's run first.")
    for t in ("agent_state", "proposals", "runs"):
        db.ex(f"DELETE FROM {t} WHERE agent_id=?", (aid,))
    db.ex("DELETE FROM run_logs WHERE run_id NOT IN (SELECT id FROM runs)")
    db.ex("DELETE FROM agents WHERE id=?", (aid,))
    db.audit(u["username"], "agent.delete", {"id": aid})
    return {"ok": True}


@app.post("/api/agents/{aid}/run")
async def run_agent(aid: int, u=auth.need("admin")):
    try:
        rid = runner.start_run(aid, "manual", u["username"])
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    db.audit(u["username"], "agent.run", {"id": aid, "run": rid})
    return {"run_id": rid}


@app.post("/api/agents/{aid}/test")
async def test_agent(aid: int, u=auth.need("admin")):
    a = db.one("SELECT * FROM agents WHERE id=?", (aid,))
    if not a:
        raise HTTPException(404, "No such agent.")
    atype = TYPES[a["type"]]
    out: dict = {}
    try:
        cfg = atype.validate(json.loads(a["config"]))
        out["connection"] = await atype.test(cfg)
    except Exception as e:
        out["connection_error"] = str(e)
        cfg = json.loads(a["config"])
    checks = [("model", cfg.get("server"), cfg.get("model"))]
    if cfg.get("embed_model"):
        checks.append(("embed_model", cfg.get("embed_server") or cfg.get("server"), cfg.get("embed_model")))
    status: dict = {}
    for key, sid, model in checks:
        try:
            if sid not in status:
                llm = LLM.from_server(sid)
                try:
                    status[sid] = (llm.kind, await llm.status())
                finally:
                    await llm.close()
            kind, st = status[sid]
        except Exception as e:
            out[key] = {"ok": False, "detail": str(e)}
            continue
        if not st["ok"]:
            out[key] = {"ok": False, "detail": f"server unreachable: {st['error']}"}
            continue
        names = [m["name"] for m in st["models"]]
        if model in names or f"{model}:latest" in names:
            out[key] = {"ok": True, "detail": "available"}
        elif kind == "openai" and len(names) == 1:
            # llama.cpp and similar serve one model and answer to any name.
            out[key] = {"ok": True, "detail": f"server has one model ({names[0]}); requests go to it"}
        else:
            out[key] = {"ok": False, "detail": "not on the server" + (" (ollama pull it)" if kind == "ollama" else "")
                        + (f"; it has: {', '.join(names[:8])}" if names else "")}
    return out


# ---------- runs ----------

@app.get("/api/runs")
def runs(agent_id: int | None = None, limit: int = 50, u=auth.need("viewer")):
    if agent_id:
        return db.q("SELECT * FROM runs WHERE agent_id=? ORDER BY id DESC LIMIT ?", (agent_id, min(limit, 500)))
    return db.q("SELECT r.*, a.name agent_name FROM runs r JOIN agents a ON a.id=r.agent_id ORDER BY r.id DESC "
                "LIMIT ?", (min(limit, 500),))


@app.get("/api/runs/{rid}")
def run(rid: int, after: int = 0, u=auth.need("viewer")):
    r = db.one("SELECT r.*, a.name agent_name FROM runs r JOIN agents a ON a.id=r.agent_id WHERE r.id=?", (rid,))
    if not r:
        raise HTTPException(404, "No such run.")
    r["logs"] = db.q("SELECT id, at, level, message FROM run_logs WHERE run_id=? AND id>? ORDER BY id LIMIT 2000",
                     (rid, after))
    return r


@app.post("/api/runs/{rid}/cancel")
def cancel(rid: int, u=auth.need("admin")):
    runner.cancel_run(rid)
    db.audit(u["username"], "run.cancel", {"id": rid})
    return {"ok": True}


# ---------- proposals ----------

def _prop_out(p: dict) -> dict:
    out = dict(p)
    out["proposed"] = json.loads(p["proposed"])
    out["original"] = json.loads(p["original"]) if p.get("original") else None
    return out


@app.get("/api/proposals")
def proposals(status: str = "pending", limit: int = 200, u=auth.need("viewer")):
    where, args = ("", ()) if status == "all" else ("WHERE p.status=? ", (status,))
    return db.q("SELECT p.id, p.agent_id, p.kind, p.target_id, p.title, p.status, p.created_at, p.decided_by, "
                "p.decided_at, p.result, a.name agent_name FROM proposals p LEFT JOIN agents a ON a.id=p.agent_id "
                + where + "ORDER BY p.id DESC LIMIT ?", args + (min(limit, 1000),))


@app.get("/api/proposals/{pid}")
def proposal(pid: int, u=auth.need("viewer")):
    p = db.one("SELECT p.*, a.name agent_name FROM proposals p LEFT JOIN agents a ON a.id=p.agent_id WHERE p.id=?",
               (pid,))
    if not p:
        raise HTTPException(404, "No such proposal.")
    return _prop_out(p)


@app.put("/api/proposals/{pid}")
def edit_proposal(pid: int, body: dict = Body(...), u=auth.need("reviewer")):
    from .textutil import clean_html
    p = db.one("SELECT * FROM proposals WHERE id=?", (pid,))
    if not p or p["status"] not in ("pending", "failed"):
        raise HTTPException(409, "Only pending proposals can be edited.")
    new = json.loads(p["proposed"])
    for k in ("title", "keywords", "topic"):
        if k in body:
            new[k] = str(body[k])
    if "description" in body:
        new["description"] = clean_html(str(body["description"]))
    db.ex("UPDATE proposals SET proposed=? WHERE id=?", (json.dumps(new), pid))
    db.audit(u["username"], "proposal.edit", {"id": pid})
    return {"ok": True}


@app.post("/api/proposals/{pid}/approve")
async def approve(pid: int, u=auth.need("reviewer")):
    ok, msg = await runner.apply_proposal(pid, u["username"])
    if not ok:
        raise HTTPException(409, msg)
    return {"ok": True, "result": msg}


@app.post("/api/proposals/{pid}/reject")
def reject(pid: int, body: dict = Body(default={}), u=auth.need("reviewer")):
    p = db.one("SELECT status FROM proposals WHERE id=?", (pid,))
    if not p or p["status"] not in ("pending", "failed"):
        raise HTTPException(409, "Only pending proposals can be rejected.")
    db.ex("UPDATE proposals SET status='rejected', decided_by=?, decided_at=?, result=? WHERE id=?",
          (u["username"], time.time(), str(body.get("reason") or "")[:500], pid))
    db.audit(u["username"], "proposal.reject", {"id": pid})
    return {"ok": True}


# ---------- users & audit ----------

@app.get("/api/users")
def users(u=auth.need("admin")):
    return {"users": db.q("SELECT id, username, email, name, role, local, created_at, last_login FROM users "
                          "ORDER BY local DESC, name"),
            "groups_mapped": prefs.groups_mapped()}


@app.post("/api/users")
def add_local_user(body: dict = Body(...), u=auth.need("admin")):
    uid = auth.create_local_user(str(body.get("username") or ""), str(body.get("password") or ""),
                                 str(body.get("role") or ""), str(body.get("name") or ""))
    db.audit(u["username"], "user.add", {"id": uid, "username": body.get("username"), "role": body.get("role")})
    return {"id": uid}


@app.post("/api/users/{uid}/password")
def reset_password(uid: int, body: dict = Body(...), u=auth.need("admin")):
    target = db.one("SELECT * FROM users WHERE id=?", (uid,))
    if not target or not target["local"]:
        raise HTTPException(400, "Only local accounts have passwords here.")
    auth.validate_password(str(body.get("password") or ""))
    db.ex("UPDATE users SET password_hash=? WHERE id=?", (auth.hash_password(str(body["password"])), uid))
    db.audit(u["username"], "user.password", {"id": uid})
    return {"ok": True}


@app.put("/api/users/{uid}")
def edit_user(uid: int, body: dict = Body(...), u=auth.need("admin")):
    role = body.get("role")
    if role not in auth.ROLES:
        raise HTTPException(400, "Unknown role.")
    target = db.one("SELECT * FROM users WHERE id=?", (uid,))
    if not target:
        raise HTTPException(404, "No such user.")
    if target["id"] == u["id"] and role != "admin":
        raise HTTPException(400, "You can't remove your own admin role.")
    db.ex("UPDATE users SET role=? WHERE id=?", (role, uid))
    db.audit(u["username"], "user.role", {"id": uid, "role": role})
    return {"ok": True, "note": "Group mapping is on; the role resets at the user's next sign-in."
            if prefs.groups_mapped() and not target["local"] else None}


@app.delete("/api/users/{uid}")
def delete_user(uid: int, u=auth.need("admin")):
    if uid == u["id"]:
        raise HTTPException(400, "You can't delete yourself.")
    db.ex("DELETE FROM users WHERE id=?", (uid,))
    db.audit(u["username"], "user.delete", {"id": uid})
    return {"ok": True}


@app.get("/api/audit")
def audit_log(limit: int = 200, u=auth.need("admin")):
    return db.q("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (min(limit, 2000),))
