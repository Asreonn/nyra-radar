"""HTML page metadata: OpenGraph, Twitter cards, canonical link, icons, feeds, dates.

Image fallback order og:image → twitter:image mirrors CondenseIt (MIT); see
THIRD_PARTY_NOTICES.md. Implemented on lxml rather than regexes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urljoin

from lxml import html as lxml_html

from radar.util import normalize_ws, parse_datetime


@dataclass
class PageMeta:
    url: str
    title: str | None = None
    description: str | None = None
    site_name: str | None = None
    canonical_url: str | None = None
    og_image: str | None = None
    twitter_image: str | None = None
    icon: str | None = None
    author: str | None = None
    published_at: object | None = None
    language: str | None = None
    og_type: str | None = None
    feeds: list[str] = field(default_factory=list)
    raw: dict[str, str] = field(default_factory=dict)

    def image_candidates(self) -> list[tuple[str, str]]:
        out = []
        if self.og_image:
            out.append(("og", self.og_image))
        if self.twitter_image and self.twitter_image != self.og_image:
            out.append(("twitter", self.twitter_image))
        if self.icon:
            out.append(("site_icon", self.icon))
        return out


def parse_metadata(html: str, base_url: str) -> PageMeta:
    meta = PageMeta(url=base_url)
    if not html:
        return meta
    try:
        doc = lxml_html.fromstring(html[:3_000_000])
    except (ValueError, TypeError, lxml_html.etree.ParserError):
        return meta
    tags: dict[str, str] = {}
    for el in doc.iter("meta"):
        key = (el.get("property") or el.get("name") or el.get("itemprop") or "").strip().lower()
        content = el.get("content")
        if key and content and key not in tags:
            tags[key] = content.strip()
    meta.raw = tags

    def pick(*keys: str) -> str | None:
        for k in keys:
            if tags.get(k):
                return tags[k]
        return None

    title_el = doc.find(".//title")
    meta.title = normalize_ws(pick("og:title", "twitter:title") or (title_el.text if title_el is not None else "")) or None
    meta.description = normalize_ws(pick("og:description", "description", "twitter:description") or "") or None
    meta.site_name = pick("og:site_name", "application-name")
    meta.og_type = pick("og:type")
    meta.author = pick("author", "article:author", "twitter:creator", "parsely-author")
    meta.published_at = parse_datetime(pick("article:published_time", "og:published_time", "date",
                                            "parsely-pub-date", "dc.date", "datepublished"))
    img = pick("og:image:secure_url", "og:image", "og:image:url")
    tw = pick("twitter:image", "twitter:image:src")
    meta.og_image = urljoin(base_url, img) if img else None
    meta.twitter_image = urljoin(base_url, tw) if tw else None
    html_el = doc if doc.tag == "html" else doc.getroottree().getroot()
    meta.language = (html_el.get("lang") or "").split("-")[0].lower() or None
    best_icon: tuple[int, str] | None = None
    for link in doc.iter("link"):
        rel = (link.get("rel") or "").lower()
        href = link.get("href")
        if not href:
            continue
        if "canonical" in rel.split():
            meta.canonical_url = urljoin(base_url, href)
        elif "alternate" in rel and (link.get("type") or "").lower() in (
                "application/rss+xml", "application/atom+xml", "application/feed+json"):
            meta.feeds.append(urljoin(base_url, href))
        elif "icon" in rel:
            size = 0
            sizes = link.get("sizes") or ""
            if "x" in sizes:
                try:
                    size = int(sizes.split("x")[0])
                except ValueError:
                    size = 0
            if "apple-touch-icon" in rel:
                size = max(size, 180)
            if best_icon is None or size > best_icon[0]:
                best_icon = (size, urljoin(base_url, href))
    if best_icon:
        meta.icon = best_icon[1]
    return meta
