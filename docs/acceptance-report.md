# Acceptance report — 2026-09-25

## Live collection and processing

- 12 seed sources attempted in 21.2 s.
- 239 observations, 236 new collected items; three Reddit sources failed because this network redirects anonymous JSON to login.
- After repository linking: 238 items, 76 repositories, 219 story clusters.
- Ollama `nomic-embed-text`: 238 items embedded and clustered in 5.5 s including topic classification and scoring.
- Local Qwen structured enrichment: 1/1 candidate completed in 9.2 s.
- Automatic discovery started two bounded source trials from observed feed metadata.

## Dedup and story grouping

The live cluster “Meta's new AI fidget is a ... Tamagotchi?” contains four distinct canonical articles, five raw sightings and three sources. All original URLs remain stored. `build_feed()` returned exactly one card for that cluster. `/api/v1/stories/140` exposes every member source.

The hermetic test also ingests three rewritten/linked reports, verifies three raw items remain, verifies one `StoryCluster`, then verifies one home-feed card.

## Ranking and exploration

Explicit positive feedback on item 237 changed:

- interest: 0.591289 → 0.653114
- final score: 0.415100 → 0.456638

The resulting 40-item mixed feed still contained eight `adjacent`/`wildcard` exploration items.

## Nyra API

The installed service executed all five calls through `radar_client`:

- `radar_digest(period="24h")`: 8 requested items across wildcard, important, adjacent, for-you and OSS sections.
- `radar_search("local coding agent")`: five results returned.
- `radar_project(id)`: returned `0xSero/omarchy-local-ai` with repository structure.
- `radar_related(id)`: three related results returned.
- `radar_feedback(id, "like")`: `ok: true`, and the user vote changed.

## API latency

Twelve localhost requests per endpoint, warm process, SQLite with 238 items:

- health: median 1.29 ms, p95 4.48 ms
- 40-item browse: median 7.98 ms, p95 12.30 ms
- 40 projects: median 15.68 ms, p95 16.76 ms
- fresh 40-item digest including rescore/persistence: median 297.99 ms, p95 381.27 ms

Database size at measurement: 3.8 MiB.

## Resource safety

With the background worker running, a separate llama.cpp Qwen 4B interactive request generated 1,800 tokens at about 60 tokens/s. During inference the gate sampled six times:

- free VRAM: 974 MiB
- GPU utilization: 94–96%
- `heavy_allowed`: false on every sample
- reason: `low free VRAM, GPU busy`

The request completed, no OOM/freeze occurred, and the temporary server was stopped. Radar uses the existing Ollama server rather than spawning its own; worker service limits are Nice=10, idle I/O, CPUQuota=150%, MemoryHigh=3G, MemoryMax=5G.

## UI and manual exploration

Real Chromium loaded the live 40-card feed at all required sizes:

- 360×800: scroll width 360, no overflow, mobile nav visible, smallest core target 44 px.
- 412×915: scroll width 412, no overflow, mobile nav visible, smallest core target 44 px.
- 1920×1080: scroll width 1905 (scrollbar excluded), no overflow, desktop sidebar visible.

Manual UI routes cover complete paginated collection browsing, query search, content filters, newest/novelty sorting, projects, saved items and reading history. Story Coverage exposes all cluster sources. Evidence images: `docs/screenshots/radar-360x800.png`, `radar-412x915.png`, `radar-1920x1080.png`, and `radar-story-coverage.png`.

## Automated checks

- Ruff: clean.
- Pytest: 5 passed (canonical URL, GitHub parsing, idempotent sightings, clustering/feed dedup, preference learning, stable API contract).
- Chromium responsive assertions: 3/3 passed.

## Addendum — Reddit-first update (2026-09-25)

Measured in a fresh scratch database (hash embeddings, no LLM) against the live sources:

- Reddit access probe: `old.reddit.com` JSON and RSS now answer `302 → /login?reason=lor2`, `www.reddit.com/*.json` answers 403, and `www.reddit.com/r/<sub>/<sort>/.rss` answers 200 with a descriptive User-Agent (browser-like agents hit 429 sooner).
- Collection: 18 seed sources, 407 observations, 398 new items. All 9 Reddit sources succeeded via Atom (242 posts); previously every Reddit source failed. The blocked JSON endpoint is skipped for six hours after the first login redirect. GitHub search/trending failed in this sandbox (rate limit/403), unrelated to this change.
- Discussion threads: 8/8 top Reddit candidates cached via the thread Atom feed; HN threads load through Algolia.
- Mixed 48-item digest: a 14-thread `reddit` section across 8 subreddits leads; Reddit fills at most half of every other section, so Hacker News appears in *For you*, *Top stories* and *Off the beaten path*, alongside the 10-item HN rail.
- Account connect: an invalid feed token yields 403 on the upvoted/saved feeds (reported in the account card); tokens are redacted from identifiers, stored errors and API responses.
- Automated checks: Ruff clean; Pytest 13 passed (adds Atom parsing, JSON→RSS fallback with memo, vote preservation, Reddit section and per-subreddit cap, community sorts, comments/sources API, account signals and taste profile); `scripts/ui_check.py` passes on Today and Reddit at 360×800, 412×915 and 1920×1080 with no overflow and 44 px phone touch targets.
- Screenshots: `docs/screenshots/radar-1920x1080.png`, `radar-360x800.png`, `radar-412x915.png`, `radar-reddit.png`, `radar-story-coverage.png`.

## Addendum — feedback and minimal UI (2026-09-25)

- Feedback: 👍 / 👎 with reasons (topic, source, quality, seen) / save / hide / undo, verified in Chromium: a "Less from r/LocalLLaMA" answer removed the post after 3.5 s and moved r/LocalLLaMA into *Less from* in Settings; Hide → Undo restored the post; the like toggle round-trips through `undo`.
- Tune page returned 12 posts from 12 different subreddits/sources; two ratings updated the counter and the learned profile.
- Negative taste: three "not interested" crypto posts pushed a new crypto post's `semantic_preference` below an unrelated one (test).
- Automated checks: Ruff clean; Pytest 18 passed; `scripts/ui_check.py` passes on Today and Reddit at 360×800, 412×915 and 1920×1080 (no overflow, 44 px phone targets).
