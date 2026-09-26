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

## Addendum — Nyra palette, Reddit media and compact comments (2026-09-25)

- Palette and typeface taken from Nyra (`Asreonn/nyra` `src/index.css`): dark first, accent `#b79bff` (light `#6d4dff`), Noto Sans.
- Reddit media: Atom thumbnails on `preview.redd.it/<id>` are upgraded to the original `i.redd.it/<id>` (140×132 → 1194×1134 in the probe); already-cached thumbnails are re-fetched when the better candidate appears. After one re-collection 50 Reddit posts carried full-size images; tall images render with a blurred fill.
- Detail: full Reddit text; comments compact (3 top-level shown, 24 folded reply threads on a 60-comment HN thread, "Show more" → all 60, "Read more" unclamps); lightbox opens and closes with Esc; infinite scroll 30 → 60 posts; `j`/`k` focus and `?` help work.
- Fixed a latent import cycle (normalize → clustering package → clustering.service → normalize) that broke `import radar.services.collect` when imported first.
- Automated checks: Ruff clean; Pytest 20 passed; `scripts/ui_check.py` passes on Today and Reddit at 360×800, 412×915 and 1920×1080.

## Addendum — complete posts, YouTube, Discover, live search, summaries (2026-09-25)

- Missing pictures, root causes: link posts waited on a 12-per-cycle extraction and, when the image stage had already run, never retried after og:image arrived (60 of 114 link posts in the scratch DB had no picture); text posts' inline images were double-escaped links. Now: extraction batch from settings (30) and best-ranked first, new candidates re-open the image stage, inline Reddit images become full-size candidates and render in place, images are cached every cycle.
- Complete posts on open: hydrate returned in 0.79 s with 55 comments via the thread feed; a text post's inline chart rendered at its original 1194×1134 through `/media/remote`.
- YouTube: 24 channel handles resolved to ids and each feed fetched (15 entries each); 8 default channels collected 101 videos with Shorts skipped. 25 of 26 candidate RSS feeds verified (Import AI 403, not included).
- Live search: Hacker News 25 results; Reddit search feed 200 (after a temporary 429 from this sandbox's IP — the UI now reports rate limiting plainly).
- Concurrency: two SQLite issues found and fixed — image rows were flushed before their download (holding the write lock across network I/O), and read-then-write transactions could hit the WAL snapshot conflict; downloads now happen before writes, network-bound stages commit first, FTS indexing writes first, the worker commits right after ranking, and a busy database answers 503 which the UI retries. A save during a running hydrate took 8 ms.
- Same-source items no longer form "stories" (two videos of one channel shared description boilerplate).
- Automated checks: Ruff clean; Pytest 25 passed; `scripts/ui_check.py` (now waits for rendered entries) passes on Today and Reddit at 360×800, 412×915 and 1920×1080.

## Addendum — responsiveness, instant media, Reddit rate limits (2026-09-26)

- Reported: a Reddit post linking YouTube showed no video, repeated its description, listed its own repost as "coverage", and failed its comments with a raw `HTTP 429 …` URL; media appeared only after everything had loaded.
- Root causes measured in the scratch install: the Today digest re-ranked every item on each request (2.4 s); article parsing ran on the API event loop; write transactions stayed open between an endpoint's return and its dependency commit, and py-spy showed the loop blocked in an FTS write waiting for that lock (6 s without any response); `Http` slept up to 60 s inside a request on 429; pictures appeared only after the worker cached them; YouTube links had no cover (watch pages are script-rendered).
- After: digest 0.2–0.3 s (≤ 1.1 s while a background pass runs); during the same open-a-video-post sequence the health endpoint's worst latency was 0.13 s; the reader title renders in 0.05–0.12 s and the comments come from the stored copy first.
- Rate limits: a 429 starts the shared cooldown (`x-ratelimit-reset`); verified with this sandbox's IP, the reader showed "Reddit asked Radar to slow down … Trying again in 14 s" and loaded the comments by itself afterwards.
- Media: the Reddit view rendered 25 media boxes for 30 posts and every picture loaded while scrolling (two fetched on first view via `/media/item`); YouTube-linked posts show the video cover and play in place (feed and reader); a Reddit video resolved to `CMAF_720.mp4` with its audio track paired. Playback itself could not be observed here: the bundled open-source Chromium reports no H.264 support (VP9 only), and the reader then links to Reddit.
- Automated checks: Ruff clean; Pytest 31 passed (new `tests/test_speed.py`: shared cooldown and deferral, cached comments with `retry_in`, media blocks, on-first-view pictures, new-post counts, background re-rank); `scripts/ui_check.py` passes on Today and Reddit at 360×800, 412×915 and 1920×1080. Screenshots: `radar-reddit.png`, `radar-videos.png`, `radar-reader-video.png`, `radar-reader-mobile.png`.
