# GPU server: what to run

## Stack

| Layer | Pick | Notes |
| --- | --- | --- |
| OS | Ubuntu Server 24.04 LTS, or Proxmox with the driver on the host (as on lab-pve) | Bare metal is simplest. |
| Driver | NVIDIA proprietary + `nvidia-container-toolkit` | Toolkit only if inference runs in Docker. |
| Inference | **Ollama** to start | Swaps models on demand, chat + embeddings in one server, already used on lab-pve. |
| Later | **vLLM** | When several agents run at once. Continuous batching gives far more throughput, but one model per process and it must fit in VRAM. Add it in Agentry as "OpenAI-compatible". |
| Optional | Open WebUI | Chat UI for staff on the same models. |
| Optional | `nvidia-smi` / dcgm-exporter + Prometheus | GPU temps and utilization. Agentry shows Ollama's loaded models and VRAM use. |

Agentry itself can run on the GPU server (Docker) or anywhere else on the network.

## Ollama settings

`systemctl edit ollama`:

```ini
[Service]
Environment="OLLAMA_HOST=0.0.0.0:11434"
Environment="OLLAMA_CONTEXT_LENGTH=16384"
Environment="OLLAMA_KEEP_ALIVE=30m"
Environment="OLLAMA_MAX_LOADED_MODELS=2"
Environment="OLLAMA_NUM_PARALLEL=1"
Environment="OLLAMA_FLASH_ATTENTION=1"
Environment="OLLAMA_KV_CACHE_TYPE=q8_0"
```

- `MAX_LOADED_MODELS=2` keeps the chat model and the embedding model loaded together.
- Agentry sends `num_ctx` per agent (default 16384). Ollama's default context is small and silently truncates long articles.
- Ollama has no authentication. Firewall 11434 to the Agentry host only, or put it behind a reverse proxy that checks a bearer token (Agentry can send one: API key field on the server).

## Models

The job: read a KB article or a handful of tickets, rewrite faithfully, return valid JSON. Instruction following and not inventing steps matter more than raw knowledge. 14B–32B instruct models are the sweet spot.

| VRAM | Chat model (`ollama pull …`) | Approx. size at Q4 |
| --- | --- | --- |
| 4 GB (lab-pve T1200) | `qwen3:4b` | ~3 GB. Too weak for unattended rewrites; testing only. |
| 8 GB (RTX 2080) | `qwen3:8b`, `llama3.1:8b` | ~5–6 GB. Usable in review mode. |
| 16 GB | `qwen3:14b`, `gpt-oss:20b` | ~9 GB / ~14 GB |
| **24 GB (recommended)** | `qwen3:30b-a3b` (fast MoE), `mistral-small3.2:24b`, `gemma3:27b` | ~15–19 GB, leaves room for 16k context + embeddings |
| 48 GB+ | `qwen3:32b` at Q8, `llama3.3:70b` at Q4 | ~35–43 GB |
| 80 GB+ | `gpt-oss:120b` | ~65 GB |

Embedding model (gap finding): `bge-m3` (multilingual, long input, ~1.2 GB) or `nomic-embed-text` (~0.3 GB). Pick one and keep it: changing it re-embeds everything.

Hardware suggestion: one 24 GB card (RTX 3090/4090, RTX A5000, L4) covers this agent and a few more. Add VRAM before adding cards; splitting a model across cards is slower.

Newer releases (e.g. Qwen 3.5) may be on <https://ollama.com/library> by the time you read this; try them on a copy of the agent in review mode before switching.

## Picking between candidates

1. Create one agent per model, same connection, mode **Review**, `Solutions per run` 10.
2. Run each once and compare the proposals side by side.
3. Watch for: invented steps, dropped commands or links, broken JSON (shows as warnings in the run log), and speed (run duration).
