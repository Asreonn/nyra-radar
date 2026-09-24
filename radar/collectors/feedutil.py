"""Shared RSS/Atom parsing helpers (feedparser based)."""
from __future__ import annotations

import re
from typing import Any

import feedparser

from radar.extraction.canonical_url import extract_urls
from radar.normalization.models import RawItem
from radar.util import clamp_future, normalize_ws, parse_datetime, strip_html

_IMG_SRC = re.compile(r"<img[^>]+src=[\"']([^\"']+)[\"']", re.IGNORECASE)
_HREF = re.compile(r"href=[\"'](https?://[^\"']+)[\"']", re.IGNORECASE)


def parse_feed(content: bytes) -> Any:
    parsed = feedparser.parse(content)
    if parsed.bozo and not parsed.entries:
        exc = parsed.get("bozo_exception")
        raise ValueError(f"unparseable feed: {type(exc).__name__ if exc else 'unknown'}")
    return parsed


def entry_html(entry: Any) -> str:
    if entry.get("content"):
        return "".join(c.get("value", "") for c in entry["content"])
    return entry.get("summary", "") or entry.get("description", "") or ""


def entry_images(entry: Any, html: str) -> list[str]:
    urls: list[str] = []
    for key in ("media_thumbnail", "media_content"):
        for media in entry.get(key, []) or []:
            url = media.get("url")
            mtype = media.get("type", "") or ""
            if url and (not mtype or mtype.startswith("image") or key == "media_thumbnail"):
                urls.append(url)
    for enc in entry.get("enclosures", []) or []:
        if (enc.get("type") or "").startswith("image") and enc.get("href"):
            urls.append(enc["href"])
    if entry.get("image") and isinstance(entry["image"], dict) and entry["image"].get("href"):
        urls.append(entry["image"]["href"])
    urls += _IMG_SRC.findall(html or "")
    out: list[str] = []
    for u in urls:
        if u.startswith("//"):
            u = "https:" + u
        if u.startswith("http") and u not in out and not _looks_like_tracker(u):
            out.append(u)
    return out[:4]


def _looks_like_tracker(url: str) -> bool:
    low = url.lower()
    return any(t in low for t in ("feedburner.com/~r", "pixel", "/1x1", "doubleclick", "stats.wp.com", "gravatar"))


def entry_published(entry: Any):
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        if entry.get(key):
            return clamp_future(parse_datetime(entry[key]))
    for key in ("published", "updated"):
        if entry.get(key):
            return clamp_future(parse_datetime(entry[key]))
    return None


def entry_to_raw(entry: Any, content_type: str, feed_title: str | None = None) -> RawItem | None:
    link = entry.get("link") or ""
    if not link and entry.get("links"):
        link = entry["links"][0].get("href", "")
    title = normalize_ws(strip_html(entry.get("title", "")))
    if not link or not title:
        return None
    html = entry_html(entry)
    text = strip_html(html)
    item = RawItem(
        url=link,
        title=title,
        content_type=content_type,
        source_item_id=(entry.get("id") or entry.get("guid") or link)[:300],
        text=html or None,
        text_is_html=True,
        excerpt=text[:600] if text else None,
        author=normalize_ws(entry.get("author", "")) or None,
        published_at=entry_published(entry),
        language=(entry.get("language") or None),
        meta={"feed_title": feed_title, "tags": [t.get("term") for t in entry.get("tags", []) if t.get("term")][:12]},
    )
    for url in entry_images(entry, html):
        item.add_image("feed", url)
    item.links = [u for u in _HREF.findall(html or "")][:40] + extract_urls(text, 20)
    return item
