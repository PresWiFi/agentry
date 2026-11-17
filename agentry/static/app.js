"use strict";
const $ = (s, el = document) => el.querySelector(s);
const view = $("#view");
let ME = null;
let poll = null;

// ---------- helpers ----------
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function when(t) {
  if (!t) return "—";
  const d = new Date(t * 1000), s = (Date.now() - d) / 1000;
  if (s < 60 && s > -60) return "just now";
  if (s < 3600 && s > 0) return `${Math.round(s / 60)} min ago`;
  if (s < 86400 && s > 0) return `${Math.round(s / 3600)} h ago`;
  return d.toLocaleString();
}
function dur(a, b) {
  if (!a) return "";
  const s = Math.round(((b || Date.now() / 1000) - a));
  return s < 60 ? `${s}s` : s < 3600 ? `${Math.floor(s / 60)}m ${s % 60}s` : `${Math.floor(s / 3600)}h ${Math.floor(s % 3600 / 60)}m`;
}
function bytes(n) {
  if (!n) return "—";
  const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0;
  while (n >= 1024 && i < 4) { n /= 1024; i++; }
  return `${n.toFixed(i > 2 ? 1 : 0)} ${u[i]}`;
}
const pill = s => `<span class="pill ${esc(s)}">${esc(s)}</span>`;
const can = role => ({ viewer: 1, reviewer: 2, admin: 3 }[ME?.role] || 0) >= ({ viewer: 1, reviewer: 2, admin: 3 }[role]);
function toast(msg, err = false) {
  const t = $("#toast");
  t.textContent = msg; t.className = "toast" + (err ? " err" : "");
  clearTimeout(t._h); t._h = setTimeout(() => t.classList.add("hidden"), err ? 8000 : 3500);
}
async function api(path, opts = {}) {
  const init = { method: opts.method || "GET", headers: { "X-Agentry": "1" } };
  if (opts.body !== undefined) { init.headers["Content-Type"] = "application/json"; init.body = JSON.stringify(opts.body); }
  const r = await fetch(path, init);
  if (r.status === 401 && path !== "/api/me") { showLogin(); throw new Error("Signed out"); }
  let data = null;
  try { data = await r.json(); } catch { }
  if (!r.ok) throw new Error((data && (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail))) || `HTTP ${r.status}`);
  return data;
}
async function act(fn, ok) {
  try { const r = await fn(); if (ok) toast(ok); return r; } catch (e) { toast(e.message, true); throw e; }
}
function formData(form) {
  const o = {};
  for (const el of form.elements) {
    if (!el.name) continue;
    o[el.name] = el.type === "checkbox" ? el.checked : el.value;
  }
  return o;
}

// ---------- auth ----------
async function boot() {
  const m = await api("/api/me");
  const err = new URLSearchParams(location.search).get("error");
  if (err) { history.replaceState(null, "", "/" + location.hash); }
  if (m.setup) return showSetup();
  if (!m.user) return showLogin(m.login, err);
  ME = m.user;
  $("#login").classList.add("hidden");
  $("#setup").classList.add("hidden");
  $("#shell").classList.remove("hidden");
  $("#me-name").textContent = ME.name || ME.email || ME.username;
  $("#me-role").textContent = ME.role + (ME.local ? " · local" : "");
  document.querySelectorAll("[data-role]").forEach(a => a.classList.toggle("hidden", !can(a.dataset.role)));
  route();
}
function showSetup() {
  $("#shell").classList.add("hidden");
  $("#login").classList.add("hidden");
  $("#setup").classList.remove("hidden");
}
$("#setup-form").addEventListener("submit", async ev => {
  ev.preventDefault();
  try {
    await api("/api/setup", { method: "POST", body: formData(ev.target) });
    location.hash = "#/settings";
    boot();
  } catch (e) { const el = $("#setup-error"); el.textContent = e.message; el.classList.remove("hidden"); }
});
async function showLogin(login, err) {
  if (!login) login = (await api("/api/me")).login;
  $("#setup").classList.add("hidden");
  $("#shell").classList.add("hidden");
  $("#login").classList.remove("hidden");
  $("#sso-btn").classList.toggle("hidden", !login.oidc);
  $("#sso-btn").textContent = login.oidc_label;
  $("#local-login").classList.toggle("hidden", !login.local);
  $("#or").classList.toggle("hidden", !(login.oidc && login.local));
  $("#no-login").classList.toggle("hidden", login.oidc || login.local);
  const e = $("#login-error");
  e.textContent = err || ""; e.classList.toggle("hidden", !err);
}
$("#login-form").addEventListener("submit", async ev => {
  ev.preventDefault();
  try {
    await api("/api/login", { method: "POST", body: formData(ev.target) });
    boot();
  } catch (e) { const el = $("#login-error"); el.textContent = e.message; el.classList.remove("hidden"); }
});
$("#logout").onclick = async () => { await api("/api/logout", { method: "POST" }); location.hash = "#/"; showLogin(); };

// ---------- router ----------
const routes = [
  [/^#?\/?$/, dashboard],
  [/^#\/proposals$/, proposals],
  [/^#\/proposals\/(\d+)$/, proposalDetail],
  [/^#\/agents$/, agents],
  [/^#\/agents\/new$/, () => agentForm(null)],
  [/^#\/agents\/(\d+)$/, agentDetail],
  [/^#\/agents\/(\d+)\/edit$/, id => agentForm(id)],
  [/^#\/runs$/, runs],
  [/^#\/runs\/(\d+)$/, runDetail],
  [/^#\/servers$/, servers],
  [/^#\/connections$/, connections],
  [/^#\/users$/, users],
  [/^#\/audit$/, audit],
  [/^#\/settings$/, settingsPage],
  [/^#\/account$/, account],
];
async function route() {
  clearInterval(poll); poll = null;
  const h = location.hash || "#/";
  document.querySelectorAll("nav a").forEach(a => {
    const href = a.getAttribute("href");
    a.classList.toggle("active", href === "#/" ? h === "#/" || h === "" : h.startsWith(href));
  });
  for (const [re, fn] of routes) {
    const m = h.match(re);
    if (m) {
      try { await fn(...m.slice(1)); } catch (e) { if (e.message !== "Signed out") view.innerHTML = `<p class="error">${esc(e.message)}</p>`; }
      refreshBadge();
      return;
    }
  }
  view.innerHTML = "<p>Not found.</p>";
}
window.addEventListener("hashchange", route);
async function refreshBadge() {
  try {
    const p = await api("/api/proposals?status=pending&limit=1000");
    const b = $("#pending-badge");
    b.textContent = p.length; b.classList.toggle("hidden", !p.length);
  } catch { }
}

// ---------- notes ("we changed X") ----------
function noteBox(agents, types) {
  const label = Object.fromEntries(types.map(t => [t.key, t]));
  const ok = agents.filter(a => label[a.type]?.note_label);
  if (!ok.length || !can("reviewer")) return "";
  const t = label[ok[0].type];
  return `<form class="card" id="note-form"><h2>${esc(t.note_label)}</h2>
    ${ok.length > 1 ? `<label>Agent <select name="agent">${ok.map(a => `<option value="${a.id}">${esc(a.name)}</option>`).join("")}</select></label>`
      : `<input type="hidden" name="agent" value="${ok[0].id}">`}
    <textarea name="text" class="note" required maxlength="8000" placeholder="We updated …"></textarea>
    <small class="muted">${esc(t.note_help)}</small>
    <div class="row"><button class="btn primary">Send</button></div></form>`;
}
function wireNoteBox() {
  $("#note-form")?.addEventListener("submit", async ev => {
    ev.preventDefault();
    const f = ev.target;
    f.querySelector("button").disabled = true;
    try {
      const r = await api(`/api/agents/${f.agent.value}/note`, { method: "POST", body: { text: f.text.value } });
      location.hash = `#/runs/${r.run_id}`;
    } catch (e) { toast(e.message, true); f.querySelector("button").disabled = false; }
  });
}

// ---------- dashboard ----------
async function dashboard() {
  const [o, types] = await Promise.all([api("/api/overview"), api("/api/agent-types")]);
  view.innerHTML = `
    <div class="head"><h1>Dashboard</h1></div>
    ${noteBox(o.agents, types)}
    <div class="grid">
      <a class="card" href="#/proposals"><div class="muted">Waiting for review</div><div class="stat">${o.pending}</div></a>
      <a class="card" href="#/agents"><div class="muted">Agents</div><div class="stat">${o.agents.length}</div>
        <div class="muted">${o.agents.filter(a => a.enabled).length} scheduled · ${o.agents.filter(a => a.running).length} running</div></a>
      <a class="card" href="#/servers"><div class="muted">Model servers</div><div class="stat">${o.servers.length}</div></a>
    </div>
    <div class="cols">
      <div class="card"><h2>Model servers</h2><div id="srv">${o.servers.length ? "" : `<p class="muted">None yet. <a href="#/servers">Add the GPU server</a>.</p>`}</div></div>
      <div class="card"><h2>Agents</h2>${o.agents.length ? `<table>${o.agents.map(a => `
        <tr class="click" onclick="location.hash='#/agents/${a.id}'"><td>${esc(a.name)}</td>
        <td>${a.running ? pill("running") : a.last_run ? pill(a.last_run.status) : '<span class="muted">never run</span>'}</td>
        <td class="muted">${a.enabled && a.next_run_at ? "next " + when(a.next_run_at).replace(" ago", "") : a.enabled ? "" : "manual"}</td></tr>`).join("")}</table>`
      : `<p class="muted">None yet. <a href="#/agents/new">Create one</a>.</p>`}</div>
    </div>
    <div class="card"><h2>Recent runs</h2>${runTable(o.runs, true)}</div>`;
  wireNoteBox();
  for (const s of o.servers) {
    const el = document.createElement("div");
    el.innerHTML = `<b>${esc(s.name)}</b> <span class="muted">${esc(s.base_url)}</span> <span class="pill">checking…</span>`;
    $("#srv").append(el);
    api(`/api/servers/${s.id}/status`).then(st => {
      el.querySelector(".pill").outerHTML = st.ok ? `${pill("ok")} <span class="muted">${st.latency_ms} ms · ${st.models.length} models${st.loaded.length ? " · loaded: " + st.loaded.map(m => esc(m.name)).join(", ") : ""}</span>` : `${pill("error")} <span class="muted">${esc(st.error)}</span>`;
    }).catch(() => { });
  }
}
function runTable(rs, showAgent) {
  if (!rs.length) return `<p class="muted">No runs yet.</p>`;
  return `<table><tr><th>Run</th>${showAgent ? "<th>Agent</th>" : ""}<th>Status</th><th>Started</th><th>Took</th><th>Summary</th></tr>
    ${rs.map(r => `<tr class="click" onclick="location.hash='#/runs/${r.id}'"><td>#${r.id}</td>${showAgent ? `<td>${esc(r.agent_name)}</td>` : ""}
    <td>${pill(r.status)}</td><td>${when(r.started_at)}<br><small class="muted">${esc(r.trigger)}${r.triggered_by ? " · " + esc(r.triggered_by) : ""}</small></td>
    <td>${dur(r.started_at, r.finished_at)}</td><td>${esc(r.summary || "")}</td></tr>`).join("")}</table>`;
}

// ---------- proposals ----------
async function proposals() {
  const status = sessionStorage.getItem("pstatus") || "pending";
  const list = await api(`/api/proposals?status=${status}`);
  view.innerHTML = `
    <div class="head"><h1>Proposals</h1>
      <select id="pstatus" style="width:auto">${["pending", "applied", "rejected", "failed", "all"].map(s => `<option ${s === status ? "selected" : ""}>${s}</option>`).join("")}</select></div>
    <div class="card">${list.length ? `<table><tr><th>#</th><th>Change</th><th>Agent</th><th>Status</th><th>Created</th></tr>
      ${list.map(p => `<tr class="click" onclick="location.hash='#/proposals/${p.id}'"><td>${p.id}</td><td>${esc(p.title)}</td>
      <td>${esc(p.agent_name || "")}</td><td>${pill(p.status)}${p.decided_by ? `<br><small class="muted">${esc(p.decided_by)}</small>` : ""}</td><td>${when(p.created_at)}</td></tr>`).join("")}</table>`
      : `<p class="muted">Nothing ${status === "all" ? "here" : status}.</p>`}</div>`;
  $("#pstatus").onchange = e => { sessionStorage.setItem("pstatus", e.target.value); proposals(); };
}
function docFrame(html) {
  // Sandboxed with no scripts: article HTML from ServiceDesk Plus or the model can't run code here.
  const f = document.createElement("iframe");
  f.className = "doc"; f.setAttribute("sandbox", ""); f.setAttribute("referrerpolicy", "no-referrer");
  f.srcdoc = `<!doctype html><meta charset="utf-8"><style>body{font:14px/1.5 system-ui,sans-serif;color:#1b1f27;padding:4px 12px}img{max-width:100%}pre{white-space:pre-wrap;background:#f3f4f6;padding:8px}table{border-collapse:collapse}td,th{border:1px solid #ccc;padding:4px}</style>${html || "<p><i>(empty)</i></p>"}`;
  return f;
}
async function proposalDetail(id) {
  const p = await api(`/api/proposals/${id}`);
  const open = p.status === "pending" || p.status === "failed";
  const o = p.original || {}, n = p.proposed;
  view.innerHTML = `
    <div class="head"><h1>${esc(p.title)}</h1>${pill(p.status)}</div>
    <div class="card"><dl class="kv">
      <dt>Agent</dt><dd><a href="#/agents/${p.agent_id}">${esc(p.agent_name || "?")}</a> · <a href="#/runs/${p.run_id}">run #${p.run_id}</a></dd>
      <dt>Kind</dt><dd>${p.kind === "update" ? `Update solution ${esc(p.target_id)}` : `New solution in topic “${esc(n.topic || "")}”`}</dd>
      <dt>Created</dt><dd>${when(p.created_at)}</dd>
      ${p.decided_by ? `<dt>Decided</dt><dd>${esc(p.decided_by)} · ${when(p.decided_at)}</dd>` : ""}
      ${p.result ? `<dt>Result</dt><dd class="${p.status === "failed" ? "error" : ""}">${esc(p.result)}</dd>` : ""}
    </dl>${p.rationale ? `<h3>Why</h3><pre class="rationale">${esc(p.rationale)}</pre>` : ""}</div>
    ${open && can("reviewer") ? `<div class="row" style="margin-bottom:16px">
      <button class="btn primary" id="approve">${p.status === "failed" ? "Retry" : "Approve and publish"}</button>
      <button class="btn" id="edit">Edit before approving</button>
      <button class="btn danger" id="reject">Reject</button></div>` : ""}
    <div id="editor" class="card hidden"><form id="pform">
      <label>Title <input name="title" value="${esc(n.title || o.title || "")}"></label>
      <label>Keywords <input name="keywords" value="${esc(n.keywords ?? o.keywords ?? "")}"></label>
      ${p.kind === "create" ? `<label>Topic <input name="topic" value="${esc(n.topic || "")}"><small>Topic name or ID.</small></label>` : ""}
      <label>Article HTML <textarea name="description" style="min-height:300px">${esc(n.description ?? o.description ?? "")}</textarea></label>
      <div class="row"><button class="btn primary">Save</button><button type="button" class="btn" id="cancel-edit">Cancel</button></div></form></div>
    <div class="${p.kind === "update" ? "cols" : ""}">
      ${p.kind === "update" ? `<div class="card"><h2>Current</h2><p><b>${esc(o.title)}</b></p><p class="muted">Keywords: ${esc(o.keywords || "—")}</p><div id="f-old"></div></div>` : ""}
      <div class="card"><h2>${p.kind === "update" ? "Proposed" : "Draft"}</h2>
        <p><b>${esc(n.title ?? o.title)}</b>${n.title && o.title && n.title !== o.title ? ' <span class="pill pending">changed</span>' : ""}</p>
        <p class="muted">Keywords: ${esc(n.keywords ?? o.keywords ?? "—")}${n.keywords !== undefined && o.keywords !== undefined && n.keywords !== o.keywords ? ' <span class="pill pending">changed</span>' : ""}</p>
        <div id="f-new"></div></div>
    </div>`;
  if (p.kind === "update") $("#f-old").append(docFrame(o.description));
  $("#f-new").append(docFrame(n.description ?? o.description));
  if (!open || !can("reviewer")) return;
  $("#approve").onclick = async e => {
    e.target.disabled = true;
    try { const r = await act(() => api(`/api/proposals/${id}/approve`, { method: "POST" })); toast(r.result); } catch { }
    proposalDetail(id);
  };
  $("#reject").onclick = async () => {
    const reason = prompt("Reason (optional):");
    if (reason === null) return;
    await act(() => api(`/api/proposals/${id}/reject`, { method: "POST", body: { reason } }), "Rejected");
    location.hash = "#/proposals";
  };
  $("#edit").onclick = () => $("#editor").classList.remove("hidden");
  $("#cancel-edit").onclick = () => $("#editor").classList.add("hidden");
  $("#pform").onsubmit = async ev => {
    ev.preventDefault();
    await act(() => api(`/api/proposals/${id}`, { method: "PUT", body: formData(ev.target) }), "Saved");
    proposalDetail(id);
  };
}

// ---------- agents ----------
async function agents() {
  const [list, types] = await Promise.all([api("/api/agents"), api("/api/agent-types")]);
  const tname = Object.fromEntries(types.map(t => [t.key, t.name]));
  view.innerHTML = `
    <div class="head"><h1>Agents</h1>${can("admin") ? `<a class="btn primary" href="#/agents/new">New agent</a>` : ""}</div>
    <div class="card">${list.length ? `<table><tr><th>Name</th><th>Type</th><th>Schedule</th><th>Mode</th><th></th></tr>
      ${list.map(a => `<tr class="click" onclick="location.hash='#/agents/${a.id}'"><td>${esc(a.name)}</td><td>${esc(tname[a.type] || a.type)}</td>
      <td>${a.enabled && a.schedule_minutes ? `every ${a.schedule_minutes} min` : "manual"}</td><td>${esc(a.config.mode || "")}</td>
      <td>${a.running ? pill("running") : ""}</td></tr>`).join("")}</table>`
      : `<p class="muted">No agents yet.</p>`}</div>`;
}
async function agentDetail(id) {
  const [a, rs, types] = await Promise.all([api(`/api/agents/${id}`), api(`/api/runs?agent_id=${id}&limit=25`), api("/api/agent-types")]);
  const t = types.find(t => t.key === a.type);
  view.innerHTML = `
    <div class="head"><h1>${esc(a.name)}</h1>${a.running ? pill("running") : ""}
      ${can("admin") ? `<button class="btn primary" id="run" ${a.running ? "disabled" : ""}>Run now</button>
      <button class="btn" id="test">Test setup</button><a class="btn" href="#/agents/${id}/edit">Settings</a>` : ""}</div>
    <div class="card"><dl class="kv">
      <dt>Type</dt><dd>${esc(t ? t.name : a.type)}</dd>
      <dt>Mode</dt><dd>${a.config.mode === "auto" ? "<b>Auto</b>: publishes changes directly" : "Review: changes wait for approval"}</dd>
      <dt>Schedule</dt><dd>${a.enabled && a.schedule_minutes ? `every ${a.schedule_minutes} min · next ${a.next_run_at ? new Date(a.next_run_at * 1000).toLocaleString() : "—"}` : "manual only"}</dd>
      <dt>Model</dt><dd>${esc(a.config.model || "—")}${a.config.embed_model ? ` · embeddings ${esc(a.config.embed_model)}` : ""}</dd>
    </dl><div id="test-out"></div></div>
    ${noteBox([a], types)}
    <div class="card"><h2>Runs</h2>${runTable(rs, false)}</div>`;
  wireNoteBox();
  if (!can("admin")) return;
  $("#run").onclick = async () => {
    const r = await act(() => api(`/api/agents/${id}/run`, { method: "POST" }));
    location.hash = `#/runs/${r.run_id}`;
  };
  $("#test").onclick = async e => {
    e.target.disabled = true;
    const out = $("#test-out");
    out.innerHTML = `<p class="muted">Testing…</p>`;
    try {
      const r = await api(`/api/agents/${id}/test`, { method: "POST" });
      const line = (ok, label, detail) => `<div>${ok ? pill("ok") : pill("error")} ${label} <span class="muted">${esc(detail || "")}</span></div>`;
      out.innerHTML = `<h3>Test</h3>` +
        (r.connection ? line(true, "ServiceDesk Plus", `${r.connection.solutions} solutions`) : line(false, "ServiceDesk Plus", r.connection_error)) +
        (r.model ? line(r.model.ok, "Chat model", r.model.detail) : "") +
        (r.embed_model ? line(r.embed_model.ok, "Embedding model", r.embed_model.detail) : "");
    } catch (err) { out.innerHTML = `<p class="error">${esc(err.message)}</p>`; }
    e.target.disabled = false;
  };
}
async function agentForm(id) {
  const [types, srv, conns, a] = await Promise.all([api("/api/agent-types"), api("/api/servers"), api("/api/connections"),
    id ? api(`/api/agents/${id}`) : null]);
  let type = a ? a.type : types[0].key;
  const render = () => {
    const t = types.find(x => x.key === type);
    const cfg = { ...t.defaults, ...(a && a.type === type ? a.config : {}) };
    const fieldHtml = f => {
      const v = cfg[f.key];
      const help = f.help ? `<small>${esc(f.help)}</small>` : "";
      const name = `cfg.${f.key}`;
      switch (f.type) {
        case "bool": return `<label class="check"><input type="checkbox" name="${name}" ${v ? "checked" : ""}> ${esc(f.label)}</label>`;
        case "textarea": return `<label style="grid-column:1/-1">${esc(f.label)}<textarea name="${name}">${esc(v)}</textarea>${help}</label>`;
        case "select": return `<label>${esc(f.label)}<select name="${name}">${f.options.map(([k, l]) => `<option value="${esc(k)}" ${k === v ? "selected" : ""}>${esc(l)}</option>`).join("")}</select>${help}</label>`;
        case "server": return `<label>${esc(f.label)}<select name="${name}" data-server><option value="">${f.required ? "—" : "Same as model server"}</option>${srv.map(s => `<option value="${s.id}" ${s.id === v ? "selected" : ""}>${esc(s.name)}</option>`).join("")}</select>${srv.length ? "" : `<small>Add one under <a href="#/servers">Model servers</a> first.</small>`}${help}</label>`;
        case "connection": {
          const cs = conns.filter(c => c.kind === f.connection_kind);
          return `<label>${esc(f.label)}<select name="${name}"><option value="">—</option>${cs.map(c => `<option value="${c.id}" ${c.id === v ? "selected" : ""}>${esc(c.name)}</option>`).join("")}</select>${cs.length ? "" : `<small>Add one under <a href="#/connections">Connections</a> first.</small>`}${help}</label>`;
        }
        case "model": case "embed_model": return `<label>${esc(f.label)}<input name="${name}" value="${esc(v)}" list="${f.type === "embed_model" ? "embed-models" : "models"}">${help}</label>`;
        case "number": return `<label>${esc(f.label)}<input name="${name}" type="number" step="any" value="${esc(v)}">${help}</label>`;
        default: return `<label>${esc(f.label)}<input name="${name}" value="${esc(v)}">${help}</label>`;
      }
    };
    // Group fields into sections: fields before the first `section` go in "General".
    const sections = [["General", []]];
    for (const f of t.fields) { if (f.section) sections.push([f.section, []]); sections[sections.length - 1][1].push(f); }
    view.innerHTML = `
      <div class="head"><h1>${a ? "Agent settings" : "New agent"}</h1></div>
      <form id="aform" class="card">
        <div class="formgrid">
          <label>Name <input name="name" required value="${esc(a ? a.name : "")}"></label>
          <label>Type <select name="type" id="atype">${types.map(x => `<option value="${x.key}" ${x.key === type ? "selected" : ""}>${esc(x.name)}</option>`).join("")}</select><small>${esc(t.description)}</small></label>
          <label>Run every (minutes) <input name="schedule_minutes" type="number" min="0" value="${a ? a.schedule_minutes : 1440}"><small>0 = only when started by hand.</small></label>
          <label class="check" style="align-self:center"><input type="checkbox" name="enabled" ${a?.enabled ? "checked" : ""}> Schedule on</label>
        </div>
        ${sections.map(([title, fs]) => `<fieldset><legend>${esc(title)}</legend><div class="formgrid">${fs.map(fieldHtml).join("")}</div></fieldset>`).join("")}
        <datalist id="models"></datalist><datalist id="embed-models"></datalist>
        <div class="row"><button class="btn primary">Save</button><a class="btn" href="${a ? `#/agents/${a.id}` : "#/agents"}">Cancel</a>
        ${a ? `<span style="flex:1"></span><button type="button" class="btn danger" id="del">Delete agent</button>` : ""}</div>
      </form>`;
    $("#atype").onchange = e => { type = e.target.value; render(); };
    // Model name suggestions come from the server each model field uses.
    const loadModels = async (sel, listId) => {
      const sid = sel?.value || $("[name='cfg.server']")?.value;
      if (!sid) return;
      try {
        const st = await api(`/api/servers/${sid}/status`);
        $("#" + listId).innerHTML = st.models.map(m => `<option value="${esc(m.name)}">`).join("");
      } catch { }
    };
    const chatSel = $("[name='cfg.server']"), embSel = $("[name='cfg.embed_server']");
    const refresh = () => { loadModels(chatSel, "models"); loadModels(embSel, "embed-models"); };
    chatSel?.addEventListener("change", refresh); embSel?.addEventListener("change", refresh);
    refresh();
    if (a) $("#del").onclick = async () => {
      if (!confirm(`Delete “${a.name}” with its runs and proposals?`)) return;
      await act(() => api(`/api/agents/${a.id}`, { method: "DELETE" }), "Deleted");
      location.hash = "#/agents";
    };
    $("#aform").onsubmit = async ev => {
      ev.preventDefault();
      const d = formData(ev.target), body = { name: d.name, type: d.type, enabled: d.enabled, schedule_minutes: +d.schedule_minutes, config: {} };
      for (const [k, v] of Object.entries(d)) if (k.startsWith("cfg.")) body.config[k.slice(4)] = v;
      if (body.config.mode === "auto" && (!a || a.config.mode !== "auto") &&
        !confirm("Auto mode publishes the model's changes to ServiceDesk Plus without review. Continue?")) return;
      const r = await act(() => a ? api(`/api/agents/${a.id}`, { method: "PUT", body }) : api("/api/agents", { method: "POST", body }), "Saved");
      location.hash = `#/agents/${a ? a.id : r.id}`;
    };
  };
  render();
}

// ---------- runs ----------
async function runs() {
  const rs = await api("/api/runs?limit=100");
  view.innerHTML = `<div class="head"><h1>Runs</h1></div><div class="card">${runTable(rs, true)}</div>`;
}
function runProposals(ps) {
  if (!ps || !ps.length) return "";
  return `<h3>Proposals</h3><table>${ps.map(p => `<tr class="click" onclick="location.hash='#/proposals/${p.id}'">
    <td>${p.kind === "create" ? "New" : "Edit"}</td><td>${esc(p.title)}</td><td>${pill(p.status)}</td></tr>`).join("")}</table>`;
}
async function runDetail(id) {
  let last = 0;
  const r = await api(`/api/runs/${id}`);
  view.innerHTML = `
    <div class="head"><h1>Run #${r.id} · <a href="#/agents/${r.agent_id}">${esc(r.agent_name)}</a></h1><span id="rstatus">${pill(r.status)}</span>
      ${can("admin") ? `<button class="btn danger ${r.status === "running" ? "" : "hidden"}" id="stop">Stop</button>` : ""}</div>
    <div class="card"><dl class="kv"><dt>Started</dt><dd>${new Date(r.started_at * 1000).toLocaleString()} (${esc(r.trigger)}${r.triggered_by ? " by " + esc(r.triggered_by) : ""})</dd>
      <dt>Took</dt><dd id="rdur">${dur(r.started_at, r.finished_at)}</dd><dt>Summary</dt><dd id="rsum">${esc(r.summary || "")}</dd>
      ${r.input ? `<dt>Note</dt><dd class="note-text">${esc(r.input)}</dd>` : ""}</dl>
      <div id="rprops">${runProposals(r.proposals)}</div></div>
    <div class="logs" id="logs"></div>`;
  const logs = $("#logs");
  const add = rows => {
    for (const l of rows) {
      const d = document.createElement("div"); d.className = l.level;
      d.innerHTML = `<time>${new Date(l.at * 1000).toLocaleTimeString()}</time>${esc(l.message)}`;
      logs.append(d); last = l.id;
    }
    if (rows.length) logs.scrollTop = logs.scrollHeight;
  };
  add(r.logs);
  $("#stop")?.addEventListener("click", async e => {
    e.target.disabled = true;
    await act(() => api(`/api/runs/${id}/cancel`, { method: "POST" }), "Stopping after the current step");
  });
  if (r.status !== "running") return;
  poll = setInterval(async () => {
    const n = await api(`/api/runs/${id}?after=${last}`);
    add(n.logs);
    $("#rdur").textContent = dur(n.started_at, n.finished_at);
    if (n.status !== "running") {
      clearInterval(poll); poll = null;
      $("#rstatus").innerHTML = pill(n.status); $("#rsum").textContent = n.summary || "";
      $("#rprops").innerHTML = runProposals(n.proposals);
      $("#stop")?.classList.add("hidden");
      refreshBadge();
    }
  }, 2000);
}

// ---------- model servers ----------
async function servers() {
  const list = await api("/api/servers");
  view.innerHTML = `
    <div class="head"><h1>Model servers</h1></div>
    <p class="muted">The GPU machine(s) that run models. Ollama, or anything with an OpenAI-compatible API (vLLM, llama.cpp server, LM Studio).</p>
    <div id="slist"></div>
    ${can("admin") ? `<form id="sform" class="card"><h2>Add a server</h2><div class="formgrid">
      <label>Name <input name="name" required placeholder="gpu-01"></label>
      <label>Kind <select name="kind"><option value="ollama">Ollama</option><option value="openai">OpenAI-compatible (vLLM, llama.cpp…)</option></select></label>
      <label>URL <input name="base_url" required placeholder="http://10.0.20.50:11434"></label>
      <label>API key <input name="api_key" type="password" placeholder="optional"><small>For servers behind an auth proxy, or vLLM --api-key.</small></label>
    </div><button class="btn primary">Add</button></form>` : ""}`;
  for (const s of list) {
    const card = document.createElement("div");
    card.className = "card";
    card.innerHTML = `<div class="head" style="margin-bottom:8px"><h2 style="flex:1;margin:0">${esc(s.name)}</h2><span class="pill st">checking…</span>
      ${can("admin") ? `<button class="btn danger del">Remove</button>` : ""}</div>
      <div class="muted">${s.kind === "ollama" ? "Ollama" : "OpenAI-compatible"} · ${esc(s.base_url)}${s.has_key ? " · API key set" : ""}</div><div class="body"></div>`;
    $("#slist").append(card);
    card.querySelector(".del")?.addEventListener("click", async () => {
      if (!confirm(`Remove ${s.name}? Agents using it will fail until you pick another.`)) return;
      await act(() => api(`/api/servers/${s.id}`, { method: "DELETE" }), "Removed"); servers();
    });
    api(`/api/servers/${s.id}/status`).then(st => {
      card.querySelector(".st").outerHTML = st.ok ? pill("ok") : pill("error");
      card.querySelector(".body").innerHTML = st.ok ? `
        <p class="muted">${st.version ? "Ollama " + esc(st.version) + " · " : ""}${st.latency_ms} ms</p>
        ${st.loaded.length ? `<h3>Loaded now</h3><table><tr><th>Model</th><th>In VRAM</th><th>Total</th><th>Context</th><th>Unloads</th></tr>${st.loaded.map(m => `<tr><td>${esc(m.name)}</td><td>${bytes(m.size_vram)}</td><td>${bytes(m.size)}${m.size_vram < m.size ? ' <span class="pill pending">partly on CPU</span>' : ""}</td><td>${m.context || "—"}</td><td>${m.expires_at ? new Date(m.expires_at).toLocaleTimeString() : "—"}</td></tr>`).join("")}</table>` : ""}
        <h3>Installed models</h3>${st.models.length ? `<table><tr><th>Model</th><th>Params</th><th>Quant</th><th>Size</th></tr>${st.models.map(m => `<tr><td>${esc(m.name)}</td><td>${esc(m.params || "")}</td><td>${esc(m.quant || "")}</td><td>${bytes(m.size)}</td></tr>`).join("")}</table>` : `<p class="muted">None. On the server: <code>ollama pull &lt;model&gt;</code></p>`}`
        : `<p class="error">${esc(st.error)}</p>`;
    }).catch(() => { });
  }
  if (!list.length) $("#slist").innerHTML = `<div class="card muted">No servers yet.</div>`;
  $("#sform")?.addEventListener("submit", async ev => {
    ev.preventDefault();
    await act(() => api("/api/servers", { method: "POST", body: formData(ev.target) }), "Added");
    servers();
  });
}

// ---------- connections ----------
const ZOHO_DC = [["us", "United States (.com)", "https://accounts.zoho.com"], ["eu", "Europe (.eu)", "https://accounts.zoho.eu"],
  ["in", "India (.in)", "https://accounts.zoho.in"], ["au", "Australia (.com.au)", "https://accounts.zoho.com.au"],
  ["jp", "Japan (.jp)", "https://accounts.zoho.jp"], ["ca", "Canada (zohocloud.ca)", "https://accounts.zohocloud.ca"],
  ["cn", "China (.com.cn)", "https://accounts.zoho.com.cn"]];
async function connections() {
  const list = await api("/api/connections");
  view.innerHTML = `
    <div class="head"><h1>Connections</h1></div>
    <p class="muted">Systems agents work in. Secrets are encrypted at rest and never sent back to the browser.</p>
    <div class="card">${list.length ? `<table><tr><th>Name</th><th>System</th><th>URL</th><th>Secrets set</th><th></th></tr>
      ${list.map(c => `<tr><td>${esc(c.name)}</td><td>ServiceDesk Plus ${c.config.edition === "cloud" ? "Cloud" : "on-prem"}</td><td>${esc(c.config.base_url)}</td>
      <td>${esc(c.secrets_set.join(", ") || "none")}</td><td class="row"><button class="btn" data-test="${c.id}">Test</button><button class="btn" data-edit="${c.id}">Edit</button><button class="btn danger" data-del="${c.id}">Delete</button></td></tr>`).join("")}</table>`
      : `<p class="muted">None yet.</p>`}</div>
    <form id="cform" class="card"></form>`;
  const form = $("#cform");
  const fill = c => {
    const cfg = c?.config || { edition: "onprem", verify_tls: true };
    form.innerHTML = `<h2>${c ? "Edit " + esc(c.name) : "Add ServiceDesk Plus"}</h2><div class="formgrid">
      <label>Name <input name="name" required value="${esc(c?.name || "ServiceDesk Plus")}"></label>
      <label>Edition <select name="edition"><option value="onprem" ${cfg.edition === "onprem" ? "selected" : ""}>On-premises</option><option value="cloud" ${cfg.edition === "cloud" ? "selected" : ""}>Cloud (Zoho)</option></select></label>
      <label>URL <input name="base_url" required value="${esc(cfg.base_url || "")}" placeholder="https://servicedesk.example.com"><small class="ed-cloud-help">Cloud: paste any page URL from your portal, e.g. https://sdpondemand.manageengine.com/app/itdesk/ui/requests. Agentry keeps the part up to the portal name.</small></label>
      <label class="check" style="align-self:center"><input type="checkbox" name="verify_tls" ${cfg.verify_tls !== false ? "checked" : ""}> Verify TLS certificate</label>
    </div>
    <fieldset class="ed-onprem"><legend>On-premises</legend><label>Technician API key <input name="api_key" type="password" placeholder="${c?.secrets_set.includes("api_key") ? "set (leave blank to keep)" : ""}"><small>Generate under the technician's profile → Generate API Key. The agent acts with this technician's permissions: give it SDAdmin or a role that can add/edit solutions and view requests.</small></label></fieldset>
    <fieldset class="ed-cloud"><legend>Cloud (Zoho OAuth)</legend>
      <ol class="muted" style="margin-top:0;padding-left:18px">
        <li>Sign in to the Zoho API Console for your data center (e.g. api-console.zoho.com) as a technician who can edit Solutions and view requests.</li>
        <li>Add Client → <b>Self Client</b> → Create. Copy the client ID and secret below.</li>
        <li>Generate Code tab → scope <code>SDPOnDemand.solutions.ALL,SDPOnDemand.requests.READ</code>, duration 10 minutes → Create. Paste the code below and Save within the time limit.</li>
      </ol>
      <div class="formgrid">
      <label>Data center <select name="dc">${ZOHO_DC.map(([k, l, u]) => `<option value="${k}" ${(cfg.accounts_url || "https://accounts.zoho.com") === u ? "selected" : ""}>${l}</option>`).join("")}<option value="custom" ${cfg.accounts_url && !ZOHO_DC.some(d => d[2] === cfg.accounts_url) ? "selected" : ""}>Other</option></select></label>
      <label>Accounts URL <input name="accounts_url" value="${esc(cfg.accounts_url || "https://accounts.zoho.com")}"></label>
      <label>Client ID <input name="client_id" autocomplete="off" placeholder="${c?.secrets_set.includes("client_id") ? "set (leave blank to keep)" : ""}"></label>
      <label>Client secret <input name="client_secret" type="password" autocomplete="off" placeholder="${c?.secrets_set.includes("client_secret") ? "set (leave blank to keep)" : ""}"></label>
      <label>Grant code <input name="grant_code" autocomplete="off" placeholder="1000.xxxx…"><small>${c?.secrets_set.includes("refresh_token") ? "Access is set up. Paste a new code only to replace it." : "One-time code from step 3. Agentry trades it for a refresh token."}</small></label>
      <label>…or refresh token <input name="refresh_token" type="password" autocomplete="off"><small>If you already have one.</small></label>
    </div></fieldset>
    <div class="row"><button class="btn primary">Save</button>${c ? `<button type="button" class="btn" id="cnew">Cancel</button>` : ""}</div>`;
    const ed = () => { const v = form.edition.value; form.querySelector(".ed-onprem").classList.toggle("hidden", v !== "onprem"); form.querySelector(".ed-cloud").classList.toggle("hidden", v !== "cloud"); form.querySelector(".ed-cloud-help").classList.toggle("hidden", v !== "cloud"); };
    form.edition.onchange = ed; ed();
    form.dc.onchange = () => { const d = ZOHO_DC.find(x => x[0] === form.dc.value); if (d) form.accounts_url.value = d[2]; };
    $("#cnew")?.addEventListener("click", () => fill(null));
    form.onsubmit = async ev => {
      ev.preventDefault();
      const d = formData(form);
      const body = { name: d.name, kind: "sdp", config: { edition: d.edition, base_url: d.base_url, verify_tls: d.verify_tls, accounts_url: d.accounts_url },
        secret: { api_key: d.api_key, client_id: d.client_id, client_secret: d.client_secret, refresh_token: d.refresh_token, grant_code: d.grant_code } };
      await act(() => c ? api(`/api/connections/${c.id}`, { method: "PUT", body }) : api("/api/connections", { method: "POST", body }), "Saved");
      connections();
    };
  };
  fill(null);
  view.querySelectorAll("[data-edit]").forEach(b => b.onclick = () => { fill(list.find(c => c.id === +b.dataset.edit)); form.scrollIntoView(); });
  view.querySelectorAll("[data-del]").forEach(b => b.onclick = async () => {
    if (!confirm("Delete this connection? Agents using it will stop working.")) return;
    await act(() => api(`/api/connections/${b.dataset.del}`, { method: "DELETE" }), "Deleted"); connections();
  });
  view.querySelectorAll("[data-test]").forEach(b => b.onclick = async () => {
    b.disabled = true;
    const r = await api(`/api/connections/${b.dataset.test}/test`, { method: "POST" }).catch(e => ({ ok: false, error: e.message }));
    toast(r.ok ? `Connected: ${r.solutions} solutions, requests readable` : r.error, !r.ok);
    b.disabled = false;
  });
}

// ---------- users & audit ----------
async function users() {
  const { users, groups_mapped } = await api("/api/users");
  view.innerHTML = `
    <div class="head"><h1>Users</h1></div>
    <p class="muted">Admins manage everything. Reviewers approve or reject proposals. Viewers can look.
      ${groups_mapped ? "Roles for SSO users come from identity-provider groups and are set again at every sign-in." : "SSO users are created at first sign-in."}</p>
    <div class="card"><table><tr><th>Name</th><th>Email</th><th>Sign-in</th><th>Role</th><th>Last sign-in</th><th></th></tr>
    ${users.map(u => `<tr><td>${esc(u.name || u.username)}</td><td>${esc(u.email || "")}</td><td>${u.local ? "local" : "SSO"}</td>
      <td><select data-role="${u.id}" style="width:auto">${["admin", "reviewer", "viewer", "none"].map(r => `<option ${r === u.role ? "selected" : ""}>${r}</option>`).join("")}</select></td>
      <td>${when(u.last_login)}</td><td class="row">${u.local ? `<button class="btn" data-pw="${u.id}" data-name="${esc(u.username)}">Set password</button>` : ""}${u.id !== ME.id ? `<button class="btn danger" data-del="${u.id}">Delete</button>` : ""}</td></tr>`).join("")}</table></div>
    <form id="uform" class="card"><h2>Add a local account</h2>
      <p class="muted">For service desk staff without SSO, or a break-glass admin. SSO users appear here after their first sign-in.</p>
      <div class="formgrid">
        <label>Username <input name="username" required autocomplete="off"></label>
        <label>Display name <input name="name"></label>
        <label>Password <input name="password" type="password" required minlength="10" autocomplete="new-password"><small>At least 10 characters.</small></label>
        <label>Role <select name="role"><option>reviewer</option><option>viewer</option><option>admin</option></select></label>
      </div><button class="btn primary">Add</button></form>`;
  $("#uform").onsubmit = async ev => {
    ev.preventDefault();
    await act(() => api("/api/users", { method: "POST", body: formData(ev.target) }), "Added"); users();
  };
  view.querySelectorAll("[data-pw]").forEach(b => b.onclick = async () => {
    const pw = prompt(`New password for ${b.dataset.name} (at least 10 characters):`);
    if (!pw) return;
    await act(() => api(`/api/users/${b.dataset.pw}/password`, { method: "POST", body: { password: pw } }), "Password set");
  });
  view.querySelectorAll("select[data-role]").forEach(s => s.onchange = async () => {
    const r = await act(() => api(`/api/users/${s.dataset.role}`, { method: "PUT", body: { role: s.value } }), "Role changed").catch(() => users());
    if (r?.note) toast(r.note);
  });
  view.querySelectorAll("[data-del]").forEach(b => b.onclick = async () => {
    if (!confirm("Delete this user? SSO users are recreated if they sign in again.")) return;
    await act(() => api(`/api/users/${b.dataset.del}`, { method: "DELETE" }), "Deleted"); users();
  });
}
async function audit() {
  const rows = await api("/api/audit?limit=500");
  view.innerHTML = `<div class="head"><h1>Audit log</h1></div><div class="card"><table><tr><th>When</th><th>Who</th><th>Action</th><th>Detail</th></tr>
    ${rows.map(r => `<tr><td>${new Date(r.at * 1000).toLocaleString()}</td><td>${esc(r.user || "")}</td><td>${esc(r.action)}</td><td class="muted">${esc(r.detail || "")}</td></tr>`).join("")}</table></div>`;
}

// ---------- settings & account ----------
async function settingsPage() {
  const s = await api("/api/settings");
  const list = a => esc((a || []).join(", "));
  view.innerHTML = `
    <div class="head"><h1>Settings</h1></div>
    <form id="gform" class="card"><h2>Address</h2><div class="formgrid">
      <label>Public URL <input name="public_url" value="${esc(s.public_url)}" placeholder="${esc(location.origin)}"><small>The address people use, e.g. https://agentry.example.com. Used for the SSO redirect URI. Empty = whatever address the browser used.</small></label>
      <label>Trusted reverse proxies <input name="trusted_proxies" value="${esc(s.trusted_proxies)}"><small>Comma-separated IPs or CIDRs whose X-Forwarded-For/Proto headers are trusted. Cookies are marked Secure automatically on HTTPS.</small></label>
    </div><button class="btn primary">Save</button></form>

    <form id="oform" class="card"><h2>Single sign-on (OpenID Connect)</h2>
      <label>Redirect URI to register at your identity provider <div class="copy"><code id="ruri">${esc(s.redirect_uri)}</code><button type="button" class="btn" id="copy-ruri">Copy</button></div>
        <small>Changes with the public URL above (save it first).</small></label>
      <div class="formgrid">
        <label style="grid-column:span 2">Issuer <input name="oidc_issuer" value="${esc(s.oidc_issuer)}" placeholder="https://login.microsoftonline.com/<tenant-id>/v2.0">
          <small>Entra ID: https://login.microsoftonline.com/&lt;tenant-id&gt;/v2.0 · Authentik: https://auth.example.com/application/o/agentry/ · Keycloak: https://kc.example.com/realms/&lt;realm&gt; · Google: https://accounts.google.com</small></label>
        <label>Client ID <input name="oidc_client_id" value="${esc(s.oidc_client_id)}" autocomplete="off"></label>
        <label>Client secret <input name="oidc_client_secret" type="password" autocomplete="off" placeholder="${s.oidc_client_secret_set ? "set (leave blank to keep)" : ""}">
          ${s.oidc_client_secret_set ? `<label class="check" style="margin-top:6px"><input type="checkbox" name="clear_oidc_client_secret"> Remove the stored secret</label>` : ""}</label>
        <label>Button label <input name="oidc_label" value="${esc(s.oidc_label)}"></label>
        <label>Scopes <input name="oidc_scopes" value="${esc(s.oidc_scopes)}"></label>
      </div>
      <h3>Roles</h3>
      <p class="muted">Group names or IDs from the groups claim (Entra sends group object IDs). First match wins: admin, reviewer, viewer. With no groups listed, new SSO users get the default role and you set roles on the Users page.</p>
      <div class="formgrid">
        <label>Groups claim <input name="oidc_groups_claim" value="${esc(s.oidc_groups_claim)}"></label>
        <label>Admin groups <input name="oidc_admin_groups" value="${list(s.oidc_admin_groups)}"><small>Comma-separated.</small></label>
        <label>Reviewer groups <input name="oidc_reviewer_groups" value="${list(s.oidc_reviewer_groups)}"></label>
        <label>Viewer groups <input name="oidc_viewer_groups" value="${list(s.oidc_viewer_groups)}"></label>
        <label>Default role <select name="oidc_default_role">${["none", "viewer", "reviewer", "admin"].map(r => `<option ${r === s.oidc_default_role ? "selected" : ""}>${r}</option>`).join("")}</select><small>For users matching no group. "none" = no access.</small></label>
        <label>Allowed email domains <input name="oidc_allowed_domains" value="${list(s.oidc_allowed_domains)}" placeholder="optional"><small>Comma-separated. Empty = any.</small></label>
      </div>
      <div class="row"><button class="btn primary">Save</button><button type="button" class="btn" id="test-oidc">Test issuer</button>
        ${s.oidc_issuer && s.oidc_client_id ? `<a class="btn" href="/auth/oidc/login">Try signing in</a>` : ""}<span id="oidc-out" class="muted"></span></div>
    </form>

    <form id="lform" class="card"><h2>Password sign-in</h2>
      <label class="check"><input type="checkbox" name="local_login" ${s.local_login ? "checked" : ""}> Allow local accounts to sign in with a password</label>
      ${s.sso_admins ? "" : `<p class="note">To turn this off, first sign in once with SSO using an account that gets the admin role.</p>`}
      <p class="muted">If SSO breaks while this is off: <code>docker exec -it agentry python -m agentry local-login on</code>, then restart the container.</p>
      <button class="btn primary">Save</button></form>

    <div class="card"><h2>Server</h2><dl class="kv">
      <dt>Encryption key</dt><dd>${s.secret_source === "env" ? "From AGENTRY_SECRET_KEY" : `Generated, stored in ${esc(s.data_dir)}/secret.key`} <small class="muted">· back up the data volume; without the key, stored secrets can't be read.</small></dd>
      <dt>Version</dt><dd id="ver"></dd></dl></div>`;
  api("/api/me").then(m => $("#ver").textContent = m.version);
  const save = (form, pick) => async ev => {
    ev.preventDefault();
    await act(() => api("/api/settings", { method: "PUT", body: pick(formData(form)) }), "Saved");
    settingsPage();
  };
  $("#gform").onsubmit = save($("#gform"), d => d);
  $("#oform").onsubmit = save($("#oform"), d => d);
  $("#lform").onsubmit = save($("#lform"), d => d);
  $("#copy-ruri").onclick = () => {
    const t = $("#ruri").textContent;
    (navigator.clipboard ? navigator.clipboard.writeText(t) : Promise.reject()).then(() => toast("Copied"), () => {
      const r = document.createRange(); r.selectNodeContents($("#ruri")); getSelection().removeAllRanges(); getSelection().addRange(r);
      toast("Selected; press Ctrl+C");
    });
  };
  $("#test-oidc").onclick = async () => {
    const out = $("#oidc-out");
    out.textContent = "Checking…";
    const r = await api("/api/settings/test-oidc", { method: "POST", body: { issuer: $("#oform").oidc_issuer.value } }).catch(e => ({ ok: false, error: e.message }));
    out.innerHTML = r.ok ? `${pill("ok")} Found ${esc(r.issuer)}${r.claims_supported && !r.claims_supported.includes($("#oform").oidc_groups_claim.value) ? ` · note: “${esc($("#oform").oidc_groups_claim.value)}” isn't in the provider's advertised claims (may still be sent)` : ""}`
      : `${pill("error")} ${esc(r.error)}`;
  };
}
async function account() {
  view.innerHTML = `<div class="head"><h1>Account</h1></div>
    <div class="card"><dl class="kv"><dt>Name</dt><dd>${esc(ME.name || "")}</dd><dt>Email</dt><dd>${esc(ME.email || "—")}</dd>
      <dt>Role</dt><dd>${esc(ME.role)}</dd><dt>Sign-in</dt><dd>${ME.local ? "Local password" : "SSO"}</dd></dl></div>
    ${ME.local ? `<form id="pwform" class="card"><h2>Change password</h2><div class="formgrid">
      <label>Current password <input name="current" type="password" required autocomplete="current-password"></label>
      <label>New password <input name="new" type="password" required minlength="10" autocomplete="new-password"><small>At least 10 characters. Signs out your other sessions.</small></label>
    </div><button class="btn primary">Change</button></form>` : ""}`;
  $("#pwform")?.addEventListener("submit", async ev => {
    ev.preventDefault();
    await act(() => api("/api/me/password", { method: "POST", body: formData(ev.target) }), "Password changed");
    ev.target.reset();
  });
}

boot();
