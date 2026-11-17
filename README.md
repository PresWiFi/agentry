# Agentry

Web app for running AI agents against a local GPU server. Agents propose changes; people approve them before anything is published (or an agent can be set to publish directly).

First agent: **ServiceDesk Plus Solutions**
- **Review**: reads existing Solution articles, proposes rewrites for unclear or badly structured ones.
- **Gaps**: groups recently resolved tickets that no Solution covers (embeddings) and drafts a new Solution from how they were resolved.

![Proposal review](docs/proposal.png)

## Features

- Model servers: Ollama or any OpenAI-compatible server. Health, installed models, what's loaded in VRAM.
- Agents: schedule (every N minutes) or run by hand, live run log, stop button.
- Proposals: side-by-side current vs proposed, edit before approving, reject. Updates are refused if the Solution changed in ServiceDesk Plus after the proposal was made.
- Sign-in: OpenID Connect (Entra ID, Authentik, Keycloak, Google, …) with group → role mapping, and local accounts.
- Roles: admin (everything), reviewer (approve/reject), viewer (read-only).
- All configuration in the UI. Secrets encrypted at rest. Audit log.
- Model output is stripped to formatting-only HTML before it's stored or sent to ServiceDesk Plus.

## Run

On any Docker host that can reach your GPU machine(s), with only [docker-compose.yml](docker-compose.yml):

```bash
gh release download -R PresWiFi/agentry -p docker-compose.yml   # or copy the file over
docker login ghcr.io -u PresWiFi        # once; GitHub token with read:packages (image is private)
docker compose up -d
docker compose logs agentry | grep "setup code"
```

Host port: set `AGENTRY_HOST_PORT` (default 8090). Build from source instead: `docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build`.

Open http://<host>:8090, enter the setup code, create the admin account. Then:
1. **Settings** → public URL and SSO (optional).
2. **Model servers** → add the GPU server (`http://<gpu-server>:11434`, kind Ollama).
3. **Connections** → add ServiceDesk Plus, **Test**.
4. **Agents** → New agent → ServiceDesk Plus Solutions → Save → **Test setup** → **Run now**.
5. **Proposals** → review.

Start in Review mode with a small `Solutions per run`. Turn on the schedule once proposals look right.

No `.env` is needed. Optional variables are in [.env.example](.env.example).

## Model backends

Agentry only talks to model servers over HTTP, so any machine on the network with a GPU can serve it. Add servers under **Model servers**; each agent picks a server for chat and (optionally) a separate one for embeddings.

| Backend | Kind | URL | Notes |
| --- | --- | --- | --- |
| Ollama | Ollama | `http://host:11434` | Chat + embeddings in one server. Shows loaded models and VRAM. |
| llama.cpp | OpenAI-compatible | `http://host:8080` | One model per process: run `llama-server -m chat.gguf -c 16384 --port 8080` and `llama-server -m embed.gguf --embeddings --port 8081`, add both, set the agent's **Embedding server** to the second. Model names are ignored. |
| vLLM | OpenAI-compatible | `http://host:8000` | Use the served model name. `--api-key` → API key field. |
| LM Studio | OpenAI-compatible | `http://host:1234` | Enable the local server and load the models first. |

Embeddings are cached per server and model, so switching the embedding server re-embeds once.

What to run on a Tesla T4: [docs/models.md](docs/models.md).

## SSO

Settings → Single sign-on. The page shows the redirect URI to register (set the public URL first).

**Entra ID**
1. App registrations → New registration → Web → the redirect URI from Settings.
2. Certificates & secrets → New client secret.
3. Token configuration → Add groups claim → Security groups. Entra sends group **object IDs**; enter those under Admin/Reviewer/Viewer groups.
4. Issuer: `https://login.microsoftonline.com/<tenant-id>/v2.0`. Enter client ID and secret, **Test issuer**, Save, **Try signing in**.

**Authentik**: OAuth2/OpenID provider + application. Issuer `https://<authentik>/application/o/<slug>/`. Groups come in the `groups` claim.

Roles from groups are set at every sign-in. With no groups listed, new SSO users get the default role and an admin sets roles on the Users page.

Once an SSO admin has signed in, password sign-in can be turned off in Settings. If SSO breaks after that:

```bash
docker exec -it agentry python -m agentry local-login on && docker restart agentry
```

Other recovery commands: `python -m agentry reset-password <user>`, `python -m agentry add-admin <user>`.

## ServiceDesk Plus Cloud

Connections → Add → Edition **Cloud (Zoho)**:
1. URL: any page URL from your portal (e.g. `https://sdpondemand.manageengine.com/app/itdesk/ui/requests`).
2. Data center: matches your Zoho region (sets the accounts URL).
3. In the Zoho API Console for that region, signed in as the technician the agent should act as (needs: view requests, add/edit Solutions): Add Client → **Self Client**. Copy client ID and secret into Agentry.
4. Self Client → Generate Code → scope `SDPOnDemand.solutions.ALL,SDPOnDemand.requests.READ`, 10 minutes. Paste it as the **Grant code** and Save. Agentry exchanges it for a refresh token and stores it encrypted.
5. **Test**.

A dedicated technician account (e.g. "KB Agent") keeps the Solution history clear. Agentry shares access tokens across runs and backs off on HTTP 429 to stay inside Zoho's limits.

On-premises: technician API key (profile → Generate API Key).

## HTTPS

Put Agentry behind a reverse proxy (Caddy, Nginx Proxy Manager, Traefik). In Settings, set the public URL to the `https://` address and add the proxy's IP to trusted proxies. Cookies are marked Secure automatically on HTTPS requests.

## Update and back up

```bash
docker compose pull && docker compose up -d
```

Everything (database, generated encryption key) is in the `agentry-data` volume:
`docker run --rm -v agentry_agentry-data:/data -v "$PWD":/backup alpine tar czf /backup/agentry-data.tgz -C /data .`
(Compose prefixes the volume with the project name, the folder name by default; check with `docker volume ls`.)

Images: `:latest` follows main, `:X.Y.Z` for releases (tag `vX.Y.Z` matching `agentry/__init__.py`).

## Develop

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
AGENTRY_ADMIN_PASSWORD=dev-password AGENTRY_DATA=./data .venv/bin/python -m agentry   # http://localhost:8090
.venv/bin/python -m pytest -q
```

Tests run the agent end to end against an in-memory ServiceDesk Plus and Ollama (`tests/fakes.py`).

### Adding an agent type

Subclass `AgentType` in `agentry/agents/`, declare `fields` (the settings form is built from them), implement `run(ctx)` and `apply(proposal, config)`, and register it in `TYPES` in `agentry/agents/__init__.py`. Use `ctx.propose(...)` for changes so they go through review.

## Status

v0.2.0. Not yet tested against a real ServiceDesk Plus instance, the T4, or a real identity provider. SDP field names for request resolutions and Solution timestamps may need adjusting.
