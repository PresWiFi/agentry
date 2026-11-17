# GPU server: what to run

Target: one **NVIDIA Tesla T4** (16 GB, Turing, 70 W). Agentry runs in its own container and calls the GPU server over the network.

## Stack on the GPU server

| Layer | Pick | Notes |
| --- | --- | --- |
| OS | Ubuntu Server 24.04 LTS (or Proxmox with the driver on the host, like lab-pve) | |
| Driver | NVIDIA 580-series proprietary | `nvidia-smi` should list the T4. |
| Inference | **Ollama** | Chat + embeddings in one server, loads/unloads models on demand. |
| Optional | Open WebUI | Chat UI for staff on the same models. |

vLLM isn't worth it on a single T4: no BF16, and 16 GB only fits 14B models with 4-bit AWQ/GPTQ. Revisit if you add a bigger card and run several agents at once.

**Cooling:** the T4 is passively cooled and expects server-chassis airflow. In a tower or a quiet chassis it throttles. Check under load:
`nvidia-smi --query-gpu=temperature.gpu,clocks.sm,clocks_throttle_reasons.active --format=csv -l 5`

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

- `MAX_LOADED_MODELS=2` keeps the chat and embedding models in VRAM together.
- `KV_CACHE_TYPE=q8_0` halves context memory; needs flash attention on.
- Agentry also sends the context length per agent (default 16384).
- Ollama has no authentication. Allow 11434 only from the Agentry container's IP: `ufw allow from <agentry-ip> to any port 11434 proto tcp`.

## Models for the T4

```bash
ollama pull qwen3:14b
ollama pull nomic-embed-text
```

| Use | Model | VRAM (Q4) | Notes |
| --- | --- | --- | --- |
| Chat (start here) | `qwen3:14b` | ~9.3 GB | Good instruction following and JSON. Set the agent's **Thinking** to Off unless quality suffers; thinking roughly doubles run time. |
| Chat (try) | `gpt-oss:20b` | ~13–14 GB | Mixture-of-experts, so fast for its size. Tight on 16 GB with 16k context + embeddings; Ollama may swap models between steps. |
| Chat (lighter) | `gemma3:12b` | ~8 GB | If 14B is too slow. |
| Embeddings | `nomic-embed-text` | ~0.3 GB | Default for gap finding. |
| Embeddings (alt) | `bge-m3` | ~1.2 GB | Better with long or non-English tickets. Changing the embedding model re-embeds everything once. |

Budget with `qwen3:14b`: ~9.3 GB weights + ~1.5–2 GB for 16k context + 0.3 GB embeddings ≈ 12 GB, leaving headroom. Agentry's Model servers page shows what's loaded and flags models that spilled onto the CPU.

Too big for a T4 without spilling to CPU: `mistral-small3.2:24b`, `gemma3:27b`, `qwen3:30b-a3b`, `qwen3:32b`.

Newer releases (e.g. Qwen 3.5) may be on <https://ollama.com/library>. Try them on a second agent in Review mode before switching.

## 8 GB GPUs (e.g. RTX PRO 2000, RTX 4060)

- Chat: `qwen3:8b`, Thinking off, context 12288. Drop to 8192 if `ollama ps` shows a CPU/GPU split.
- Embeddings: `nomic-embed-text`.
- Same Ollama settings as above. If the GPU also drives displays, Windows takes some of the VRAM.
- `qwen3:4b` if 8b is too slow; edits are noticeably weaker.

## Picking between models

1. Create one agent per model with the same connection, Review mode, `Solutions per run` 10.
2. Run each once and compare proposals side by side.
3. Watch for invented steps, dropped commands or links, JSON warnings in the run log, and run duration.
