"""Generic RSS/Atom collector with conditional GET (ETag / Last-Modified)."""
from __future__ import annotations

from radar.collectors.base import Collector, CollectorError, FetchResult, SourceSpec
from radar.collectors.feedutil import entry_to_raw, parse_feed
from radar.net import HttpError


class RSSCollector(Collector):
    kind = "rss"
    content_type = "rss_entry"

    async def fetch(self, source: SourceSpec) -> FetchResult:
        headers = {"Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.8"}
        if source.etag:
            headers["If-None-Match"] = source.etag
        if source.last_modified:
            headers["If-Modified-Since"] = source.last_modified
        try:
            resp = await self.http.get(source.identifier, headers=headers, max_bytes=8_000_000)
        except HttpError as exc:
            raise CollectorError(str(exc), exc.status_code, permanent=exc.status_code in (404, 410)) from exc
        if resp.status_code == 304:
            return FetchResult(not_modified=True, status_code=304, etag=source.etag,
                               last_modified=source.last_modified)
        try:
            parsed = parse_feed(resp.content)
        except ValueError as exc:
            raise CollectorError(str(exc), resp.status_code) from exc
        feed_title = parsed.feed.get("title")
        ctype = source.config.get("content_type", self.content_type)
        items = []
        for entry in parsed.entries[: source.limit(40)]:
            raw = entry_to_raw(entry, ctype, feed_title)
            if raw:
                raw.meta["feed_url"] = source.identifier
                items.append(raw)
        return FetchResult(items=items, etag=resp.headers.get("etag"),
                           last_modified=resp.headers.get("last-modified"), status_code=resp.status_code)
