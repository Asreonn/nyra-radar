# Architecture

Radar is one Python package with two processes and no container dependency:

1. `nyra-radar-api` serves FastAPI, the static responsive browser UI, images, and the stable `/api/v1` contract.
2. `nyra-radar-worker` runs a cooperative two-minute cycle. Collection and cheap CPU work always proceed in bounded batches; heavy work passes through `ResourceGate`.
3. SQLite in WAL mode is the source of truth. FTS5 provides lexical retrieval and an in-process NumPy matrix provides semantic retrieval. There is no vector database, Redis, or second model server.
4. The existing Ollama service supplies embeddings and candidate-only JSON analysis. Radar never spawns an LLM worker. Nyra's on-demand llama process, GPU utilization, free VRAM, free RAM, and battery policy gate heavy stages.

Pipeline stages are independently recorded: normalized, extraction, image, embedding, cluster, candidate score, and summary. Failures retain the item and are retryable. Input hashes prevent unchanged content from being recomputed.

Source discovery mines exact feed metadata and repeatedly linked domains into auditable `DiscoveryCandidate` records. High-confidence candidates enter bounded trials; trial item yield, duplicate rate, failures and quality determine promotion or rejection.

Preference learning starts with seed topics, attaches local keyword topics to items, and folds explicit events into decayed interest and source-affinity scores. Liked and saved items also form an embedding taste profile: `semantic_preference` is an item's similarity percentile against their centroid, pulled down by its percentile against the centroid of items marked "less like this" (each side needs two examples). A dislike's reason decides what it teaches — topics, the subreddit/site affinity, the taste profile, or nothing — and `undo` appends an inverse event. Reddit feedback about a source targets the subreddit, whichever feed delivered the post. It never removes manual browse/search, and mixed feeds enforce `adjacent` and `wildcard` quotas.

Reddit is the primary community source. Engagement is normalised per subreddit (75th percentile of recently observed votes/comments) or, for Atom-collected posts without votes, derived from the post's hot/top listing position. The Today digest has a dedicated `reddit` section, diversifies Reddit per subreddit instead of per `reddit.com` domain, and lets Reddit take at most half of every other section. `/api/v1/community` offers Reddit-style browsing (hot, top, new, discussed, for-you) per subreddit, and `/api/v1/items/{id}/comments` fetches and caches Reddit/HN threads (`comment_summaries`; the worker prefetches the best candidates).

A Reddit account can be connected without a password through its private feed token (reddit.com/prefs/feeds): the front page follows the user's subscriptions, and the upvoted/saved listings become `like`/`save` events. Posts that only arrived because the user already reacted to them on Reddit are kept out of the feeds. The token lives only in source identifiers and is redacted in every API response and stored error.

The browser UI (`apps/web/dist`) is dependency-free HTML/CSS/ES modules served as static files; there is no build step.
