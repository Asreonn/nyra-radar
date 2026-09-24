"""Article full-text extraction with a fallback chain.

trafilatura → readability-style densest-block heuristic (lxml) → feed summary.
The raw extractor output and the cleaned text are stored separately. A failed
extraction never drops an item; it still has its title, community and project data.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from lxml import html as lxml_html

from radar.extraction.metadata import PageMeta, parse_metadata
from radar.util import normalize_ws

log = logging.getLogger(__name__)

_BOILER = re.compile(r"(cookie|subscribe to our newsletter|sign up for|all rights reserved|advertisement)",
                     re.IGNORECASE)


@dataclass
class Extraction:
    raw_text: str | None
    clean_text: str | None
    method: str
    meta: PageMeta
    word_count: int = 0


def clean_text(text: str | None) -> str:
    if not text:
        return ""
    lines = []
    seen: set[str] = set()
    for line in text.splitlines():
        line = normalize_ws(line)
        if not line or line in seen:
            continue
        if len(line) < 120 and _BOILER.search(line):
            continue
        seen.add(line)
        lines.append(line)
    return "\n".join(lines)


def _trafilatura(html: str, url: str) -> str | None:
    try:
        import trafilatura
    except ImportError:  # pragma: no cover
        return None
    try:
        return trafilatura.extract(html, url=url, include_comments=False, include_tables=False,
                                   favor_recall=True, deduplicate=True)
    except Exception as exc:  # noqa: BLE001 - trafilatura exposes many internal exception types
        log.debug("trafilatura failed for %s: %s", url, exc)
        return None


def _densest_block(html: str) -> str | None:
    """Readability-style fallback: the element with the most paragraph text."""
    try:
        doc = lxml_html.fromstring(html)
    except (ValueError, lxml_html.etree.ParserError):
        return None
    for bad in doc.xpath("//script|//style|//nav|//footer|//header|//aside|//form|//noscript"):
        bad.drop_tree()
    best, best_len = None, 0
    for el in doc.iter("article", "main", "div", "section"):
        paras = el.findall(".//p")
        length = sum(len(p.text_content()) for p in paras)
        if length > best_len:
            best, best_len = el, length
    if best is None or best_len < 200:
        return None
    return "\n".join(normalize_ws(p.text_content()) for p in best.findall(".//p") if p.text_content().strip())


def extract_main_text(html: str, url: str) -> str | None:
    return _trafilatura(html, url) or _densest_block(html)


def extract_article(html: str, url: str, fallback_text: str | None = None) -> Extraction:
    meta = parse_metadata(html, url)
    raw = _trafilatura(html, url)
    method = "trafilatura"
    if not raw or len(raw) < 200:
        dense = _densest_block(html)
        if dense and len(dense) > len(raw or ""):
            raw, method = dense, "densest_block"
    if not raw and fallback_text:
        raw, method = fallback_text, "feed_summary"
    if not raw and meta.description:
        raw, method = meta.description, "meta_description"
    cleaned = clean_text(raw)
    return Extraction(raw_text=raw, clean_text=cleaned or None, method=method if raw else "none", meta=meta,
                      word_count=len(cleaned.split()))
