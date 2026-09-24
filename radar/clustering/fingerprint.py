"""Cheap lexical fingerprints: normalized-title hash and 64-bit SimHash.

These are dedup layers 2 and 3 (after canonical URL, before embeddings).
"""
from __future__ import annotations

import hashlib
import re
import unicodedata

_PREFIXES = re.compile(r"^(show hn|ask hn|launch hn|tell hn|\[?(news|release|update|discussion|video|p|r|d)\]?)"
                       r"\s*[:\-–—|]\s*", re.IGNORECASE)
_SUFFIX = re.compile(r"\s+[\-–—|:]\s+[^\-–—|:]{2,40}$")  # " - Publisher" / " | Site"
_NON_WORD = re.compile(r"[^\w\s]", re.UNICODE)
_WS = re.compile(r"\s+")
_TOKEN = re.compile(r"[a-z0-9][a-z0-9+#.\-]*[a-z0-9+#]|[a-z0-9]")
STOP = frozenset(["a", "an", "the", "and", "or", "of", "to", "in", "on", "for", "with", "by", "at", "from", "is", "are", "was", "were", "be", "been", "this", "that", "it", "its", "as", "into", "over", "new", "how", "why", "what", "you", "your", "we", "our", "i", "my"])


def normalize_title(title: str, strip_suffix: bool = True) -> str:
    t = unicodedata.normalize("NFKC", title or "").strip()
    t = _PREFIXES.sub("", t)
    if strip_suffix:
        t = _SUFFIX.sub("", t)
    t = _NON_WORD.sub(" ", t.lower())
    return _WS.sub(" ", t).strip()


def title_hash(title: str) -> str | None:
    norm = normalize_title(title)
    if len(norm) < 12 or len(norm.split()) < 3:  # too generic to dedup safely ("Release v1.2")
        return None
    return hashlib.sha1(norm.encode()).hexdigest()


def tokens(text: str) -> list[str]:
    return [t for t in _TOKEN.findall((text or "").lower()) if t not in STOP and len(t) > 1]


def _h64(token: str) -> int:
    return int.from_bytes(hashlib.blake2b(token.encode(), digest_size=8).digest(), "big")


def simhash(text: str, shingle: int = 3) -> int | None:
    toks = tokens(text)
    if len(toks) < 8:
        return None
    feats = [" ".join(toks[i:i + shingle]) for i in range(max(1, len(toks) - shingle + 1))]
    v = [0] * 64
    for f in feats:
        h = _h64(f)
        for bit in range(64):
            v[bit] += 1 if (h >> bit) & 1 else -1
    value = 0
    for bit in range(64):
        if v[bit] > 0:
            value |= 1 << bit
    # store as signed 64-bit for SQLite INTEGER
    return value - (1 << 64) if value >= (1 << 63) else value


def hamming(a: int, b: int) -> int:
    return ((a ^ b) & ((1 << 64) - 1)).bit_count()
