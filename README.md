# Agentry

Runs AI agents against model servers on your network (Ollama, llama.cpp, vLLM, LM Studio). Agents propose changes; a person approves them, or an agent can publish directly.

**ServiceDesk Plus Solutions agent**
- **Notes**: write what changed ("We moved to GlobalProtect 6.2; reconnect from the tray icon"). It edits the affected Solutions or drafts a new one.
- **Review** (optional, scheduled): rewrites unclear Solutions.
- **Gaps** (optional, scheduled): drafts Solutions for recurring tickets that none cover. Needs an embedding model.

![Proposal review](docs/proposal.png)

## Install

```bash
curl -O https://raw.githubusercontent.com/PresWiFi/agentry/main/docker-compose.yml
docker login ghcr.io -u PresWiFi     # while the image is private: classic token with read:packages
docker compose up -d
docker compose logs agentry | grep "setup code"
```

Open `http://<host>:8090`, enter the setup code, create the admin. Port: `AGENTRY_HOST_PORT`. Optional env vars: [.env.example](.env.example).

## Set up

1. **Model servers** → add the GPU server, e.g. `http://<gpu-host>:11434`, kind Ollama.
2. **Connections** → add ServiceDesk Plus → **Test**.
3. **Agents** → New agent → ServiceDesk Plus Solutions. Set the chat model and an embedding model (`nomic-embed-text`) → **Test setup**.
4. Write a note on the Dashboard. Review the result under **Proposals**.

Models per GPU: [docs/models.md](docs/models.md).

## Model servers

| Backend | Kind | Default URL | Notes |
| --- | --- | --- | --- |
| Ollama | Ollama | `http://host:11434` | Chat and embeddings on one server. |
| llama.cpp | OpenAI-compatible | `http://host:8080` | One model per process: run a second `llama-server --embeddings` and set it as the agent's **Embedding server**. |
| vLLM | OpenAI-compatible | `http://host:8000` | API key field = `--api-key`. |
| LM Studio | OpenAI-compatible | `http://host:1234` | Start the local server and load the models. |

The server must listen on the network (Ollama: `OLLAMA_HOST=0.0.0.0`). From a container, use the host's LAN IP, not `localhost`.

## ServiceDesk Plus

**Cloud**: Connections → Add → Edition **Cloud (Zoho)**.
1. URL: any page from your portal. Pick your data center.
2. Zoho API Console (same region), signed in as the technician the agent acts as → Add Client → **Self Client**. Copy client ID and secret.
3. Generate Code, scope `SDPOnDemand.solutions.ALL,SDPOnDemand.requests.READ` → paste as **Grant code** → Save → **Test**.

**On-premises**: technician API key (profile → Generate API Key).

The technician needs: view requests, add/edit Solutions. A dedicated account (e.g. "KB Agent") keeps Solution history clear.

## SSO (OpenID Connect)

Settings → Single sign-on. Set the public URL first; the page shows the redirect URI.

- **Entra ID**: App registration (Web, redirect URI) → client secret → Token configuration → groups claim (Security groups). Issuer `https://login.microsoftonline.com/<tenant-id>/v2.0`. Group fields take group **object IDs**.
- **Authentik**: OAuth2/OpenID provider. Issuer `https://<authentik>/application/o/<slug>/`.

Roles: admin (everything), reviewer (notes, approve/reject), viewer (read-only). Set from groups at each sign-in, or on the Users page.

Locked out after turning off password sign-in:
```bash
docker exec -it agentry python -m agentry local-login on && docker restart agentry
```
Also: `reset-password <user>`, `add-admin <user>`.

## HTTPS

Use a reverse proxy. In Settings set the `https://` public URL and add the proxy IP to trusted proxies.

## Update and back up

```bash
docker compose pull && docker compose up -d
```

All data (database, encryption key) is in the `agentry-data` volume (prefixed with the compose project name; see `docker volume ls`):
```bash
docker run --rm -v agentry_agentry-data:/data -v "$PWD":/backup alpine tar czf /backup/agentry-data.tgz -C /data .
```

Tags: `:latest` = main, `:X.Y.Z` = releases.

## Develop

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
AGENTRY_ADMIN_PASSWORD=dev-password AGENTRY_DATA=./data .venv/bin/python -m agentry   # http://localhost:8090
.venv/bin/python -m pytest -q
```

Build the image locally: `docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build`.

New agent type: subclass `AgentType` in `agentry/agents/`, declare `fields`, implement `run(ctx)` and `apply()`, register in `TYPES`. Set `note_label` to accept notes.

## Status

v0.3.0. Not yet tested against a real ServiceDesk Plus instance or identity provider.
