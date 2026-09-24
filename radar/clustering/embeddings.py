"""Embedding providers and vector helpers.

Default is Ollama's `nomic-embed-text` (already installed, 768-d, ~190 items/s here).
`hash` is a deterministic offline lexical fallback (feature hashing of unigrams and
bigrams). It keeps the pipeline working with no model, and it keeps tests hermetic.
Vectors are stored L2-normalised as float32 BLOBs; cosine = dot product.
"""
from __future__ import annotations

import hashlib
import itertools
import logging
import math
import re
from typing import Protocol

import httpx
import numpy as np

from radar.config import Config

log = logging.getLogger(__name__)

NOMIC_PREFIX = {"document": "search_document: ", "query": "search_query: ", "cluster": "clustering: "}


class Embedder(Protocol):
    model_id: str
    dim: int

    async def embed(self, texts: list[str], kind: str = "document") -> np.ndarray: ...


def l2(mat: np.ndarray) -> np.ndarray:
    mat = np.asarray(mat, dtype=np.float32)
    if mat.ndim == 1:
        n = float(np.linalg.norm(mat))
        return mat / n if n > 0 else mat
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return mat / norms


def to_blob(vec: np.ndarray) -> bytes:
    return np.asarray(vec, dtype=np.float32).tobytes()


def from_blob(blob: bytes | None) -> np.ndarray | None:
    if not blob:
        return None
    return np.frombuffer(blob, dtype=np.float32)


class HashEmbedder:
    """Deterministic lexical embedding (no model). Good enough for exact-ish grouping."""

    _tok = re.compile(r"[a-z0-9][a-z0-9+#.\-]*[a-z0-9+#]|[a-z0-9]")

    def __init__(self, dim: int = 512):
        self.dim = dim
        self.model_id = f"hash-{dim}"

    def _vec(self, text: str) -> np.ndarray:
        toks = self._tok.findall(text.lower())
        feats = toks + [f"{a}_{b}" for a, b in itertools.pairwise(toks)]
        v = np.zeros(self.dim, dtype=np.float32)
        counts: dict[str, int] = {}
        for f in feats:
            counts[f] = counts.get(f, 0) + 1
        for f, c in counts.items():
            h = int.from_bytes(hashlib.blake2b(f.encode(), digest_size=8).digest(), "big")
            sign = 1.0 if (h >> 63) & 1 else -1.0
            v[h % self.dim] += sign * (1.0 + math.log(c))
        return l2(v)

    async def embed(self, texts: list[str], kind: str = "document") -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        return np.vstack([self._vec(t) for t in texts])


class OllamaEmbedder:
    def __init__(self, base_url: str, model: str, timeout: float = 120.0):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.model_id = f"ollama:{model}"
        self.dim = 768 if "nomic" in model else 0
        self.timeout = timeout

    def _prefix(self, kind: str) -> str:
        return NOMIC_PREFIX.get(kind, "") if "nomic" in self.model else ""

    async def embed(self, texts: list[str], kind: str = "document") -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim or 1), dtype=np.float32)
        prefix = self._prefix(kind)
        payload = {"model": self.model, "input": [prefix + t[:6000] for t in texts], "keep_alive": "3m",
                   "truncate": True}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(f"{self.base_url}/api/embed", json=payload)
            resp.raise_for_status()
            data = resp.json()
        mat = l2(np.asarray(data["embeddings"], dtype=np.float32))
        self.dim = mat.shape[1]
        return mat


class OpenAIEmbedder:
    def __init__(self, base_url: str, model: str, api_key: str = "", timeout: float = 120.0):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.model_id = f"openai:{model}"
        self.api_key = api_key
        self.dim = 0
        self.timeout = timeout

    async def embed(self, texts: list[str], kind: str = "document") -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim or 1), dtype=np.float32)
        prefix = NOMIC_PREFIX.get(kind, "") if "nomic" in self.model else ""
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(f"{self.base_url}/embeddings", headers=headers,
                                     json={"model": self.model, "input": [prefix + t[:6000] for t in texts]})
            resp.raise_for_status()
            data = resp.json()["data"]
        mat = l2(np.asarray([d["embedding"] for d in sorted(data, key=lambda d: d["index"])], dtype=np.float32))
        self.dim = mat.shape[1]
        return mat


def _ollama_has(base: str, model: str) -> bool:
    try:
        tags = httpx.get(f"{base.rstrip('/')}/api/tags", timeout=3).json()
        names = {m.get("name", "") for m in tags.get("models", [])}
        return model in names or f"{model}:latest" in names
    except (httpx.HTTPError, ValueError):
        return False


def make_embedder(config: Config) -> Embedder:
    cfg = config.embeddings
    provider = cfg.provider
    if provider == "hash":
        return HashEmbedder()
    if provider in ("auto", "ollama"):
        base = cfg.base_url or "http://127.0.0.1:11434"
        if provider == "ollama" or _ollama_has(base, cfg.model):
            return OllamaEmbedder(base, cfg.model)
        log.warning("embedding model %s not available on %s; using offline hash embedder", cfg.model, base)
        return HashEmbedder()
    if provider == "openai":
        key = config.secret(config.llm.api_key, config.llm.api_key_command)
        return OpenAIEmbedder(cfg.base_url or "http://127.0.0.1:8080/v1", cfg.model, key)
    return HashEmbedder()
