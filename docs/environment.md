# Machine / Environment Report

Inspected 2026-09-25, before Radar was built. Re-run `radar doctor` for a live view.

## Hardware and OS

| Item | Value |
|---|---|
| OS | CachyOS (Arch), kernel 7.2.4-3-cachyos, Hyprland (Wayland) session |
| CPU | Intel i7 14th gen, 28 logical threads |
| RAM | 15 GiB. **11 GiB was in use and the 15 GiB of swap was almost full** at inspection time (browser, Penpot JVM, Steam, other agents) |
| GPU | NVIDIA RTX 4060 Laptop, 8188 MiB VRAM (≈90 MiB used while idle) |
| Power | Laptop: `ACAD` + `BAT1` under `/sys/class/power_supply` |
| Disk | `/home`: 1.3 TB, 430 GB free |

**Consequence:** RAM is the scarce resource, not CPU. Radar has no PyTorch, no local
transformer runtime, no vector DB, no Redis, and no second LLM. Embeddings and the LLM
use the Ollama service that is already running.

## Existing Nyra

| Item | Value |
|---|---|
| Location | `~/Apps/nyra` (git repo, no commits yet, every file untracked) |
| Stack | Tauri 2 desktop shell (`src-tauri/`), React 19 + Vite UI (`src/`), Node/TypeScript "core" backend (`core/src/*.ts`, bundled with esbuild) |
| Core HTTP | `127.0.0.1:47321` (`NYRA_PORT`), token-protected. Runtime handshake in `$XDG_RUNTIME_DIR/nyra/session.json` (`port`, `token`) |
| Tool registry | `core/src/tools.ts`: a flat array of `{name, description, properties, required}` converted to OpenAI function tools and dispatched by a `switch` in `dispatch()` |
| Config/state | `~/.config/nyra` (empty), `~/.local/share/nyra/nyra.db` (`node:sqlite`, with `kv/projects/threads/messages/docs/links` tables), `~/.local/state/nyra/nyra.log`, `~/.cache/nyra` |
| LLM | Nyra **spawns its own** `/usr/bin/llama-server` on demand (`core/src/llama.ts`): port 8091–8110, `-a nyra --metrics -np 1 -ngl 99`, Qwen3.5-4B Q4_K_M (`~/.local/share/pc-voice-assistant/models/qwen/`), plus Qwen3.5-2B and Gemma-3-4B in `~/.local/share/nyra/models/`. The PID is in `$XDG_RUNTIME_DIR/nyra/llama.pid`. The model is unloaded when the Nyra UI closes |
| Status at inspection | Not running (stale `core.pid` / `llama.pid`) |
| Voice | Voice, wake-word and speech daemons are retired (`pc-voice-assistant.service` is masked) |

**Radar's policy toward Nyra:** Radar never touches Nyra's files. It only *reads*
`$XDG_RUNTIME_DIR/nyra/llama.pid` and `/proc` to see whether Nyra's llama-server is
running inference. Radar lives in its own directory, `~/Apps/nyra-radar`, and has its own
data directories. Integration goes through Radar's HTTP API and the `radar_client` package.

## Local AI infrastructure

| Endpoint | What | Use in Radar |
|---|---|---|
| `127.0.0.1:11434` | **Ollama 0.34** systemd *system* service (`OLLAMA_HOST=127.0.0.1:11434`). Models: `obsidian-qwen3.5-4b` (3.0 GB, thinking+tools capable, 8k default ctx), `nomic-embed-text` (274 MB, 768-d) | **Default LLM** (native `/api/chat` with `think:false` and JSON-schema `format`) and **default embedder** (`/api/embed`) |
| 8091–8110 | Nyra's on-demand llama-server (only while the Nyra UI is open) | Watched by the resource manager as the signal that the user is in an interactive session |
| `/usr/bin/llama-*` | llama.cpp binaries (server, embedding, bench, …) | Not spawned by Radar; any configured OpenAI-compatible URL also works |

Measured on this machine (inspection time):
- qwen3.5-4b through Ollama: cold load 5.1 s, ~46 tokens/s generation, **3.1 GB VRAM** at a 4k context.
- nomic-embed-text: cold load 1.0 s; a batch of 64 short texts takes 0.33 s (~190 items/s); ~0.4 GB VRAM.
- nomic-embed-text cosine similarity: the same story reworded scores 0.89–0.96; related but different tech news scores 0.67–0.70. Clustering thresholds are calibrated to this.

VRAM budget: Nyra's Qwen-4B server (~4 GB) plus Radar's Ollama Qwen-4B (~3.1 GB) would nearly fill
8 GB. The resource manager therefore (a) pauses Radar LLM jobs while Nyra infers, (b) asks
Ollama to unload Radar's chat model if free VRAM falls below a threshold, and (c) otherwise
lets Ollama's keep-alive handle unloading, so models are not reloaded unnecessarily.

## Toolchains

| Tool | Version / path |
|---|---|
| System Python | 3.14.7 (`/usr/bin/python3`) |
| uv-managed Python | 3.12 (`~/.local/bin/python3.12`), 3.14 |
| uv | 0.12.3. **Radar venv: `.venv` (Python 3.12), created with uv** |
| pipx | `/usr/bin/pipx` |
| Node | v24.19.0 via fnm, npm, pnpm (no yarn, no bun) |
| Docker / Podman | Both installed. Docker runs Penpot (9001, 1080) and Termix (8080). **Radar does not need either** |
| SQLite (Python) | 3.53.1 with FTS5 |
| GitHub CLI | `gh` logged in as `Asreonn` with a keyring token. Radar can use `gh auth token` for 5000 req/h |

## Ports in use (127.0.0.1 unless noted)

`53`, `1080` (mailcatcher), `2222`, `5355`, `8077` (basic-memory MCP), `8080` (Termix),
`9001` (Penpot), `11434` (Ollama), `27036` (Steam, 0.0.0.0), `46289`, `51427`, Steam ephemeral ports,
Tailscale `100.64.212.2:443/46565` plus IPv6.

**Radar uses `127.0.0.1:47400`**. The port is free and next to Nyra's 47321 range.

## User systemd services (relevant)

`basic-memory-mcp.service`, several `dungeon-*` agent/playtest services (some failed, and unrelated),
`cursor-agent-reaper.service`, `pc-voice-assistant.service` (masked). Nyra itself has **no**
systemd unit; it runs through `~/Apps/nyra/nyra.sh`. Radar adds `nyra-radar-api.service` and
`nyra-radar-worker.service` (user units), grouped by `nyra-radar.target`.

## Network reachability (live probes)

| Source | Result |
|---|---|
| HN Firebase + Algolia | 200 |
| GitHub REST (unauthenticated) | 200. Authenticated through `gh auth token` |
| OSS Insight `api.ossinsight.io/v1/trends/repos` | 200 |
| github.com/trending (HTML) | 200 |
| Google News RSS search | 200 (after a redirect to add `hl/gl/ceid`) |
| YouTube channel RSS | 200 |
| GDELT doc API | 200 (slow) |
| **Reddit `www.reddit.com/*.json`** | **403** without OAuth |
| Reddit `old.reddit.com/*.json` with a browser-like UA | **200** |
| Reddit `.rss` | 200, but **429** after 2–3 quick requests |

Decision: the Reddit collector uses `old.reddit.com` JSON with strict per-host pacing
(1 request / 3 s). It falls back to RSS, and uses OAuth (`oauth.reddit.com`) automatically
if a client id/secret is configured.

## Directories Radar uses

| Purpose | Path |
|---|---|
| Code | `~/Apps/nyra-radar` |
| State (SQLite DB, WAL) | `~/.local/share/nyra-radar/radar.db` |
| Config | `~/.config/nyra-radar/config.toml` (mode 0600) |
| Cache (images, HTTP) | `~/.cache/nyra-radar/` |
| Logs | journald (`journalctl --user -u nyra-radar-api -u nyra-radar-worker`) |
| Runtime | `$XDG_RUNTIME_DIR/nyra-radar/` |
