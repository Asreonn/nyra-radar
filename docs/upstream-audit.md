# Upstream / Reference Audit

All five repositories were cloned into `references/` on 2026-09-25. They are **reference
material only**. None of them is the application root, and none runs as a Radar service.
Each license was read from the `LICENSE` file at the exact commit below. The machine-readable
form is `references/manifest.json`, and `THIRD_PARTY_NOTICES.md` is generated from it by
`scripts/update_notices.py`.

## Summary table

| Project | Commit | License | Useful Components | Components We Will Reuse | Components We Will Reimplement | Reason |
| ------- | ------ | ------- | ----------------- | ------------------------ | ------------------------------ | ------ |
| wildlifechorus/condenseit | `a3129e1dda3245067d2b8db27e683b7555d18985` | MIT (LICENSE file, "CondenseIt contributors") | Collectors for RSS, Reddit JSON, HN, YouTube (yt-dlp/transcripts), Google News RSS, GitHub releases, website-change watcher, podcasts; og:image thumbnails; rating-based preference engine (TF-IDF + category/source + implicit signals + embedding centroids + time decay); LLM rerank blend; provider fallback chain + budget; Ollama lifecycle | Preference time decay and explicit/implicit event weighting; og:image → twitter:image fallback order (adapted with attribution) | Every collector (sync `httpx.Client` → async, unified `RawItem`), the preference engine (star ratings → event model + topic graph + semantic vector), the provider layer (→ one OpenAI-compatible/Ollama client + resource manager), and the DB (sqlite-utils ad-hoc `ALTER TABLE` migrations → SQLAlchemy + Alembic) | Sync, per-digest batch design with a star-rating UX. Radar needs continuous async ingestion, a normalized multi-type model and event-based learning. The formulas are sound, the architecture is not reusable as-is. |
| Thysrael/Horizon | `74a70a29ac45ae9ce6d6f82c465452a157aa7a46` | MIT (LICENSE file, "Thysrael") | Async scrapers: HN (Firebase + comments), Reddit (JSON, RSS, old.reddit HTML fallback, comments), GitHub (user events, releases), OSS Insight trends, Telegram `t.me/s`, Google News, GDELT, Twitter; profile-based analysis/enrichment prompts; cross-source URL dedup; topic dedup; MCP server with run store | Tracking-param stripping list for URL identity; OSS Insight endpoint + field mapping; old.reddit fallback idea; `t.me/s` parsing approach (adapted with attribution) | Collectors (Horizon emits flat `ContentItem`s per run, Radar upserts into a persistent normalized DB with sightings), enrichment prompts (Radar uses JSON-schema structured outputs per content type), the MCP/tool layer (→ REST + JSON tool schemas + Python client) | Horizon is a run-oriented briefing generator (fetch → score → summarize → send). Radar is a persistent, incremental, interactive system, so the scraper shapes and run store do not fit. Its collector coverage and source-specific processing profiles directly shaped `radar/collectors/` and `radar/enrichment/`. |
| slb350/news-llama | `8c07beacaaed915607ef6227187f5ddee4ccbfb7` | MIT (LICENSE file, "News Llama Contributors") | `SourceDiscoveryEngine` 5-tier progressive discovery; autonomous weekly discovery (list mining, direct search, health check, quality scoring, blacklist, auto-promote to Tier 1 at 0.8); SQLAlchemy + Alembic models (`discovered_sources`, `source_health`, `source_contributions`); duplicate detector | Auditable candidate → trial → promoted/rejected lifecycle and quality gating (adapted with attribution) | Discovery uses exact RSS metadata and repeated-link mining with measured trial metrics; the LLM layer is local structured JSON | The best reference for discovery. It still promotes on static heuristics instead of *measured trial behavior*, so Radar gates promotion on real trial data and preserves an audit row for every decision. |
| philoking/cruxwire | `822944e33742a466eaadb5ff95852a4e34d5d206` | MIT (LICENSE file, "Jason Burns") | Ollama scoring + `nomic-embed-text` embeddings; anchor-based cosine clustering; coverage boost; taste vector from saves/opens; per-source affinity (client-side); read/Later/History; semantic search; magazine-style single-file UI (`digest.html`) | Anchor-style time-windowed clustering, coverage-aware scoring and server-side source affinity (adapted with attribution) | Storage (JSON files → SQLite), clustering (batch O(n²) → incremental sliding window plus hard identity joins), affinity (browser-local → server-side), UI (independently written responsive application) | Its clustering and affinity design transfers well. JSON/per-device state does not satisfy Radar's persistent API and Nyra integration requirements. |
| XimilalaXiang/YourRSS | `f809e0bec653e3ba99f41272da35dd7d0a39b945` | **Unclear.** The README says "MIT" but the commit has **no LICENSE file**. It is a fork of `HarrisHan/ai-daily-digest`, whose terms were not verified | Two-phase scoring (cheap score for all → detailed summary for top N), concurrency-bounded batch scoring, preference memory via Cortex recall (like/dislike/prefer/forget), full-text analysis | **None** (ideas only) | Two-stage cheap-first → LLM-for-top-candidates pipeline, persistent preference memory, explicit like/dislike, "prefer/forget" as interest boost/mute/reset, full-text-based enrichment. All written independently | Without a LICENSE file the permissive grant is not clearly established, so no code, prompts or constants were copied. It is also JS + FreshRSS + Cortex. Only the concepts are used, and FreshRSS/Cortex are not dependencies. |

**InReader** was deliberately not cloned or consulted because of its licensing.

## Per-project notes

### CondenseIt (`references/condenseit`)
- **Architecture:** a Click CLI and a FastAPI admin. `pipeline/orchestrator.py` (1k lines)
  runs the collectors synchronously, dedups by content hash, balances articles per category,
  summarizes with a provider fallback chain, and renders a digest. A Preact/Tailwind frontend
  handles admin tasks.
- **Dependencies:** fastapi, feedparser, trafilatura, httpx, sqlite-utils, numpy, ollama, yt-dlp,
  youtube-transcript-api, cryptography (for key storage).
- **Schema:** `articles`, `digests`, `ratings`, `read_articles`, `read_later`, `spending`, …
  created through sqlite-utils with ad-hoc `ALTER TABLE` column migrations.
- **Collectors:** each one is a sync class returning dicts (`url, title, content, source, category,
  content_hash, published_at`). Reddit uses the `www.reddit.com/*.json` endpoint, which returned 403
  from this machine during inspection.
- **Ranking:** `learning/preference_engine.py` is an additive ladder of 10 tiers (keywords,
  term/bigram overlap, TF-IDF cosine, category/source mean ratings, implicit signals, synonyms,
  embedding centroids, LLM topics), each decayed by rating age. An optional LLM rerank is blended in.
- **LLM:** `providers/` (Ollama, OpenAI, OpenRouter) with a fallback chain and a spending budget.
- **Tests:** 144 test functions (pytest), including collector tests with fixtures.

### Horizon (`references/Horizon`)
- **Architecture:** `orchestrator.py` (1.1k lines) runs async scrapers → cross-source URL
  merge → AI analysis per profile (0–10 score) → topic dedup → enrichment (web search +
  comments) → a summary in several languages → delivery (email/webhook/WeChat). Includes an MCP server.
- **Dependencies:** httpx, feedparser, anthropic/openai/google-genai, trafilatura, bs4, ddgs, mcp.
- **Schema:** none. Runs are stored as files by `mcp/run_store.py`.
- **Collectors:** `scrapers/*.py` covers HN, Reddit (JSON → RSS → old.reddit HTML), GitHub, OSS Insight,
  Telegram, Google News, GDELT, Twitter (cookies/playwright), and OpenBB.
- **Tests:** 483 test functions, well mocked.

### News Llama (`references/news-llama`)
- **Architecture:** a Flask/FastAPI web app with users, interests and newsletter generation.
  `processors/source_discovery.py` implements the tiered discovery.
  `web/services/autonomous_discovery_service.py` runs weekly list mining, direct LLM search,
  a health check, quality scoring and Tier-1 auto-promotion.
- **Dependencies:** open-agent-sdk, feedparser, asyncpraw, tweepy, newspaper3k, textblob, ddgs.
- **Schema:** SQLAlchemy + Alembic (`users`, `newsletters`, `user_interests`, `tier1_sources`,
  `source_blacklist`, `discovered_sources`, `source_health`, `source_contributions`).
- **Tests:** 528 test functions.

### Cruxwire (`references/cruxwire`)
- **Architecture:** `pipeline.py` fetches feeds → Ollama scores/summarizes/categorizes →
  `nomic-embed-text` embeddings → cluster → taste boost → retention/carry-forward →
  `digest.json`. `server.py` serves `digest.html`, a large vanilla JS app that handles
  read/Later/History/affinity in the browser.
- **Dependencies:** standard library only (urllib) plus Ollama over HTTP.
- **Schema:** JSON files (`settings.json`, `digest.json`, user state, embeddings index).
- **Tests:** none in the repository. `TUNING.md` documents the knobs thoroughly.

### YourRSS (`references/YourRSS`)
- **Architecture:** an agent skill (`SKILL.md`) plus five Node scripts: FreshRSS fetch, RSS
  fetch, Cortex memory API, and scoring (two-phase, batched, concurrency-limited).
- **Schema:** none of its own. It depends on FreshRSS and the Cortex memory server.
- **Tests:** none.
- **License:** see the table. Treated as ideas-only.

## What Radar does better than the sum of the references

- **One persistent store** (SQLite WAL, 25+ normalized tables, Alembic) instead of JSON files,
  per-run stores, or per-device state.
- **Incremental, idempotent stage machine** (`processing_state`, `embedding_state`,
  `summary_state`, plus input hashes), so nothing is re-embedded or re-summarized unless its input changed.
- **Five-layer dedup** (canonical URL → title hash → SimHash → embedding → story cluster). Raw
  URLs and cross-source sightings are kept, never deleted.
- **Deterministic, inspectable ranking** with every component stored, and explanations derived
  only from those stored components.
- **Budgeted exploration** (55/15/15/10/5) so the feed is never pure exploitation.
- **Measured source trials** with an audit trail, instead of heuristic auto-promotion.
- **Project intelligence**: append-only repository snapshots, locally computed star deltas,
  mention velocity, and Reddit/HN/article cross-links.
- **VRAM-aware resource manager** that yields to interactive Nyra inference.
