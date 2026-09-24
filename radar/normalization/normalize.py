"""RawItem -> normalized field dict (pure; no DB access)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from radar.clustering.fingerprint import simhash, title_hash
from radar.extraction.canonical_url import canonicalize, domain_of, url_hash
from radar.normalization.models import RawItem
from radar.util import normalize_ws, sha1_hex, strip_html, truncate, utcnow

_EN = frozenset(["the", "and", "of", "to", "in", "is", "for", "that", "with", "on", "this", "are", "be", "as", "by", "it", "from", "at", "an", "was", "not", "we", "you"])


def detect_language(text: str) -> str | None:
    words = re.findall(r"[a-zA-Z']+", (text or "").lower())[:200]
    if len(words) < 5:
        non_ascii = sum(1 for ch in text or "" if ord(ch) > 0x2FF)
        return None if non_ascii < 3 else "und"
    hits = sum(1 for w in words if w in _EN)
    if hits / len(words) > 0.08:
        return "en"
    non_ascii = sum(1 for ch in text if ord(ch) > 0x2FF)
    return "und" if non_ascii > len(text) * 0.2 else None


@dataclass
class Normalized:
    fields: dict[str, Any]
    raw: RawItem

    @property
    def url_hash(self) -> str:
        return self.fields["url_hash"]


def embed_text(title: str, body: str | None, limit: int = 900) -> str:
    """The exact text we embed; its hash decides whether re-embedding is needed."""
    return normalize_ws(f"{title}. {truncate(body or '', limit)}")


def normalize(raw: RawItem, source_id: int | None) -> Normalized:
    canonical = canonicalize(raw.url)
    title = normalize_ws(strip_html(raw.title))[:500]
    text_plain = strip_html(raw.text) if raw.text_is_html else normalize_ws(raw.text or "")
    excerpt = normalize_ws(raw.excerpt or "") or truncate(text_plain, 600) or None
    link = canonicalize(raw.link_url) if raw.link_url else None
    if link == canonical:
        link = None
    body_for_hash = text_plain or excerpt or ""
    fields: dict[str, Any] = {
        "canonical_url": canonical,
        "url_hash": url_hash(canonical),
        "source_id": source_id,
        "source_item_id": (raw.source_item_id or "")[:300] or None,
        "content_type": raw.content_type,
        "title": title,
        "raw_text": raw.text,
        "clean_text": text_plain or None,
        "excerpt": truncate(excerpt, 700) if excerpt else None,
        "author": (raw.author or "")[:300] or None,
        "published_at": raw.published_at,
        "fetched_at": utcnow(),
        "language": raw.language or detect_language(f"{title} {body_for_hash[:800]}"),
        "domain": domain_of(link or canonical),
        "link_url": link,
        "link_hash": url_hash(link) if link else None,
        "external_score": raw.external_score,
        "external_comment_count": raw.external_comment_count,
        "meta": {k: v for k, v in raw.meta.items() if v not in (None, "", [], {})},
        "title_hash": title_hash(title),
        "content_hash": sha1_hex(f"{title}\n{body_for_hash[:5000]}"),
        "simhash": simhash(f"{title} {body_for_hash[:4000]}"),
    }
    if raw.image_candidates:
        fields["meta"]["image_candidates"] = raw.image_candidates[:8]
    return Normalized(fields=fields, raw=raw)
