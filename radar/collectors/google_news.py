"""Google News RSS search collector (query-based discovery of news coverage)."""
from __future__ import annotations

import re
from urllib.parse import quote_plus

from radar.collectors.base import Collector, CollectorError, FetchResult, SourceSpec
from radar.collectors.feedutil import entry_published, parse_feed
from radar.net import HttpError
from radar.normalization.models import RawItem
from radar.util import normalize_ws, strip_html


def search_url(query: str, lang: str = "en-US", country: str = "US") -> str:
    return (f"https://news.google.com/rss/search?q={quote_plus(query)}&hl={lang}&gl={country}"
            f"&ceid={country}:{lang.split('-')[0]}")


class GoogleNewsCollector(Collector):
    kind = "google_news"
    content_type = "article"

    async def fetch(self, source: SourceSpec) -> FetchResult:
        query = source.identifier
        window = source.config.get("when", "2d")
        url = search_url(f"{query} when:{window}" if window else query,
                         source.config.get("lang", "en-US"), source.config.get("country", "US"))
        try:
            resp = await self.http.get(url)
        except HttpError as exc:
            raise CollectorError(str(exc), exc.status_code) from exc
        try:
            parsed = parse_feed(resp.content)
        except ValueError as exc:
            raise CollectorError(str(exc), resp.status_code) from exc
        items: list[RawItem] = []
        for entry in parsed.entries[: source.limit(25)]:
            title = normalize_ws(strip_html(entry.get("title", "")))
            publisher = (entry.get("source") or {}).get("title")
            publisher_url = (entry.get("source") or {}).get("href")
            if publisher and title.endswith(f" - {publisher}"):
                title = title[: -len(publisher) - 3]
            link = entry.get("link")
            if not title or not link:
                continue
            desc = strip_html(entry.get("summary", ""))
            desc = re.sub(r"\s*" + re.escape(publisher or "") + r"\s*$", "", desc) if publisher else desc
            items.append(RawItem(
                url=link,
                title=title,
                content_type="article",
                source_item_id=(entry.get("id") or link)[:300],
                excerpt=desc[:400] or None,
                author=publisher,
                published_at=entry_published(entry),
                meta={"publisher": publisher, "publisher_url": publisher_url, "query": query,
                      "via": "google_news", "unresolved_redirect": True},
            ))
        return FetchResult(items=items, status_code=resp.status_code)
