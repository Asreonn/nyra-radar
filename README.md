# Nyra Radar

Nyra Radar is a self-hosted personal technology intelligence radar and open-source discovery engine. It continuously collects from heterogeneous sources, preserves raw sightings, extracts and clusters coverage, treats repositories as evolving projects, learns explicit preferences, and reserves feed capacity for exploration.

It is deliberately not an RSS inbox: the main unit is a ranked story or project signal, while the complete unranked collection remains manually browsable.

## Current installation

- Code: `/home/asreonn/Apps/nyra-radar`
- UI/API: <http://127.0.0.1:47400>
- OpenAPI: <http://127.0.0.1:47400/docs>
- Database: `/home/asreonn/.local/share/nyra-radar/radar.db`
- Configuration: `/home/asreonn/.config/nyra-radar/config.toml`
- Image cache: `/home/asreonn/.cache/nyra-radar/images`
- Logs: `journalctl --user -u nyra-radar-api -u nyra-radar-worker`

The API and worker are installed as enabled systemd user services. The worker is low-priority, resource-capped, and will not start heavy model/image/project work while RAM/VRAM is low, the GPU is busy, or Nyra's own `llama-server` is active.

## Run and operate

```bash
cd /home/asreonn/Apps/nyra-radar
.venv/bin/radar doctor
.venv/bin/radar collect --force
.venv/bin/radar serve
.venv/bin/radar worker
```

Service commands:

```bash
systemctl --user status nyra-radar.target nyra-radar-api nyra-radar-worker
systemctl --user restart nyra-radar.target
journalctl --user -f -u nyra-radar-api -u nyra-radar-worker
```

Reinstall/link the units with `scripts/install-user-service.sh`.

## Sources and pipeline

Implemented collectors:

- RSS/Atom and arbitrary feed URLs
- Hacker News top/best/new/ask/show plus Algolia keyword searches
- Reddit OAuth, anonymous JSON, and RSS fallback
- GitHub search, Trending, organizations/users, repository releases
- OSS Insight trending repositories
- Google News RSS search
- YouTube public Atom feeds
- website change watches
- public Telegram channel pages

Collection is idempotent. URL canonicalization removes tracking and normalizes common mirrors; every original URL remains in `item_sightings`. Articles use `trafilatura` with a densest-block fallback. Images are MIME/dimension checked, downscaled, content-addressed WebP files. Embeddings use the existing Ollama `nomic-embed-text` model, with a deterministic hash fallback.

Deduplication and clustering are layered: canonical URL, linked target, normalized title, SimHash, then embedding cosine similarity in a time window. A multi-source cluster appears once in the home feed and exposes every report in its Coverage section.

Ranking stores every normalized component and evidence in `item_scores`: interest, semantic preference, freshness, novelty, source quality/affinity, content type, community signal, project momentum, coverage, optional LLM quality, duplicate/spam penalties, and ignored-item decay. Feed construction applies source diversity and explicit section budgets; `adjacent` and `wildcard` retain serendipity.

GitHub projects have separate metadata, snapshots, locally measured star growth, cross-community mentions, README content, release/activity fields, license and hydration state. Unknown values remain null; no growth metric is fabricated.

## Nyra tool API

The Python adapter is `radar_client.RadarClient`:

```python
from radar_client import RadarClient

radar = RadarClient()
digest = radar.radar_digest(period="24h")
results = radar.radar_search("local coding agent")
project = radar.radar_project(results["results"][0]["repository_id"])
related = radar.radar_related(results["results"][0]["id"])
feedback = radar.radar_feedback(results["results"][0]["id"], "like")
```

Stable REST endpoints are versioned under `/api/v1`. The next Nyra voice integration point is `core/src/tools.ts`: register the five methods above and dispatch them to `http://127.0.0.1:47400/api/v1/...`. Keep Radar a separate service; do not import its database into Nyra.

## Test and verification

```bash
.venv/bin/ruff check radar radar_client tests
.venv/bin/pytest -q
.venv/bin/python scripts/ui_check.py
```

The responsive check drives real Chromium at 360×800, 412×915, and 1920×1080, asserts a loaded feed, zero horizontal overflow, mobile navigation visibility, and at least 44 px core touch targets. Screenshots are in `docs/screenshots/`. See `docs/acceptance-report.md` for live-source, API, ranking, dedup, resource-safety, and latency results.

## Security and privacy

The default bind is loopback-only. Non-loopback requests require the generated bearer token unless explicitly disabled. Secrets may be supplied by commands (for example `gh auth token`) and are never logged. Runtime state follows XDG paths and no Radar code or data is written inside Nyra.

## Known limitations

- Reddit currently redirects anonymous JSON to login on this network; the adapter and RSS fallback are implemented, but reliable Reddit collection requires OAuth credentials here.
- Telegram supports public web channels only; private channels require credentials and are disabled.
- GitHub GraphQL-only enrichment uses the existing `gh` token; anonymous REST falls back to its lower limit.
- Automatic source trials require enough observation time (default three days) before promotion/rejection.
- Ranking improves as explicit feedback and local repository snapshots accumulate.

Upstream references, exact commits, extracted ideas, and license findings are documented in `docs/upstream-audit.md`; generated notices are in `THIRD_PARTY_NOTICES.md`.
