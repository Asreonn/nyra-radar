# Nyra Radar

Nyra Radar is a self-hosted personal technology intelligence radar and open-source discovery engine. It continuously collects from heterogeneous sources, preserves raw sightings, extracts and clusters coverage, treats repositories as evolving projects, learns explicit preferences, and reserves feed capacity for exploration.

It is deliberately not an RSS inbox: the main unit is a ranked story or project signal, while the complete unranked collection remains manually browsable. Reddit is the primary community source: Today opens with the best threads from your subreddits, and a dedicated Reddit view lets you browse them like Reddit itself (hot/top/new/discussed, per subreddit, with the discussion thread inline). Hacker News follows as its own section and view. The UI is a single, quiet reading column.

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
- Reddit: OAuth, anonymous JSON, and the `www.reddit.com` Atom feed (the path that works without an account today); subreddits, multireddits (`rust+golang`) and private account feeds
- GitHub search, Trending, organizations/users, repository releases
- OSS Insight trending repositories
- Google News RSS search
- YouTube public Atom feeds
- website change watches
- public Telegram channel pages

Collection is idempotent. URL canonicalization removes tracking and normalizes common mirrors; every original URL remains in `item_sightings`. Articles use `trafilatura` with a densest-block fallback. Images are MIME/dimension checked, downscaled, content-addressed WebP files. Embeddings use the existing Ollama `nomic-embed-text` model, with a deterministic hash fallback.

Deduplication and clustering are layered: canonical URL, linked target, normalized title, SimHash, then embedding cosine similarity in a time window. A multi-source cluster appears once in the home feed and exposes every report in its Coverage section.

Ranking stores every normalized component and evidence in `item_scores`: interest, semantic preference (similarity percentile to your liked/saved items, pulled down by similarity to items you marked "less like this"), freshness, novelty, source quality/affinity, content type (`ranking.content_type_preference`, Reddit 0.8 / HN 0.65 by default), community signal (normalized per subreddit, or the hot/top listing position when votes are unknown), project momentum, coverage, optional LLM quality, duplicate/spam penalties, and ignored-item decay. Feed construction applies explicit section budgets — `reddit` (30% by default) first — per-subreddit and per-domain diversity, and caps Reddit at half of each other section; `adjacent` and `wildcard` retain serendipity. The Settings view has sliders for the Reddit share and the per-type preferences.

## Teaching Radar what you want

Every post has four quiet controls: 👍 **More like this**, 👎 **Less like this**, **Save**, and **Hide** (no opinion). After 👎 Radar asks *why*, and each answer teaches something different:

| Answer | Learns |
|---|---|
| Not interested in this | topic interests + negative taste example |
| Less from r/… / site | only that subreddit's (or site's) affinity, strongly |
| Low quality / clickbait | source affinity + negative taste example |
| Already seen it | nothing — just hidden |

Everything can be undone (`{"action": "undo"}` appends an inverse event, so the log stays append-only). **Tune your feed** (`#/tune`, `GET /api/v1/tune`) shows a dozen recent posts spread across all subreddits and sources to rate quickly. Each post's detail page explains *why you're seeing it* in plain sentences, and **Settings → What Radar has learned** (`GET /api/v1/profile`) lists the subreddits, sites and topics it now favours or avoids.

## Reddit

Subreddits are added from the Reddit view (names, `r/name`, links or `a+b` multireddits) or `POST /api/v1/sources {"kind": "reddit", "identifier": "rust"}`. Without credentials Radar reads Reddit's public Atom feeds, which carry no vote counts; posts then rank by their Hot/Top position. For votes, comment counts and flair, add a Reddit app's `client_id`/`client_secret` under `[reddit]` in `config.toml`.

**Connect your account (no password):** open <https://www.reddit.com/prefs/feeds> while logged in, copy the RSS link next to "your front page", and paste it into *Connect your Reddit* (Reddit view or Settings). Radar then

- follows your front page, i.e. exactly the subreddits you subscribe to;
- imports your upvoted posts as likes and your saved posts as saves (re-synced hourly), which train the topic interests, source affinity and the embedding taste profile;
- keeps posts you already upvoted/saved out of your feeds.

The link is a read-only token. It is stored only in the source identifiers and redacted in every API response and stored error; *Disconnect* deletes it (learned interests stay).

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

Stable REST endpoints are versioned under `/api/v1`. Reddit-specific additions: `GET /communities` (subreddits with counts and collection health), `GET /community?platform=reddit|hackernews&community=…&sort=hot|top|new|discussed|best&period=3d`, `GET /items/{id}/comments`, and `GET|POST|DELETE /reddit/account` plus `POST /reddit/account/sync`. The next Nyra voice integration point is `core/src/tools.ts`: register the five methods above and dispatch them to `http://127.0.0.1:47400/api/v1/...`. Keep Radar a separate service; do not import its database into Nyra.

## Test and verification

```bash
.venv/bin/ruff check radar radar_client tests
.venv/bin/pytest -q
.venv/bin/python scripts/ui_check.py
```

The responsive check drives real Chromium at 360×800, 412×915, and 1920×1080 on Today and Reddit, asserts loaded Reddit cards, zero horizontal overflow, mobile navigation visibility, and at least 44 px core touch targets on phones. Screenshots are in `docs/screenshots/`. See `docs/acceptance-report.md` for live-source, API, ranking, dedup, resource-safety, and latency results.

## Security and privacy

The default bind is loopback-only. Non-loopback requests require the generated bearer token unless explicitly disabled. Secrets may be supplied by commands (for example `gh auth token`) and are never logged. Runtime state follows XDG paths and no Radar code or data is written inside Nyra.

## Known limitations

- Reddit redirects anonymous JSON (and old.reddit RSS) to login on this network; collection therefore runs on the `www.reddit.com` Atom feeds, which have no vote/comment counts and rate-limit bursts (429). OAuth credentials restore vote counts.
- Account connect relies on Reddit's private feed tokens for the upvoted/saved listings; a wrong token is visible as a 403 on those feeds in the account card.
- Telegram supports public web channels only; private channels require credentials and are disabled.
- GitHub GraphQL-only enrichment uses the existing `gh` token; anonymous REST falls back to its lower limit.
- Automatic source trials require enough observation time (default three days) before promotion/rejection.
- Ranking improves as explicit feedback and local repository snapshots accumulate.

Upstream references, exact commits, extracted ideas, and license findings are documented in `docs/upstream-audit.md`; generated notices are in `THIRD_PARTY_NOTICES.md`.
