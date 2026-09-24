# Architecture

Radar is one Python package with two processes and no container dependency:

1. `nyra-radar-api` serves FastAPI, the static responsive browser UI, images, and the stable `/api/v1` contract.
2. `nyra-radar-worker` runs a cooperative two-minute cycle. Collection and cheap CPU work always proceed in bounded batches; heavy work passes through `ResourceGate`.
3. SQLite in WAL mode is the source of truth. FTS5 provides lexical retrieval and an in-process NumPy matrix provides semantic retrieval. There is no vector database, Redis, or second model server.
4. The existing Ollama service supplies embeddings and candidate-only JSON analysis. Radar never spawns an LLM worker. Nyra's on-demand llama process, GPU utilization, free VRAM, free RAM, and battery policy gate heavy stages.

Pipeline stages are independently recorded: normalized, extraction, image, embedding, cluster, candidate score, and summary. Failures retain the item and are retryable. Input hashes prevent unchanged content from being recomputed.

Source discovery mines exact feed metadata and repeatedly linked domains into auditable `DiscoveryCandidate` records. High-confidence candidates enter bounded trials; trial item yield, duplicate rate, failures and quality determine promotion or rejection.

Preference learning starts with seed topics, attaches local keyword topics to items, and folds explicit events into decayed interest and source-affinity scores. It never removes manual browse/search, and mixed feeds enforce `adjacent` and `wildcard` quotas.
