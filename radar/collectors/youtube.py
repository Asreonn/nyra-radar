"""YouTube channel/playlist collector via the public Atom feeds (no API key)."""
from __future__ import annotations

import re

from radar.collectors.base import Collector, CollectorError, FetchResult, SourceSpec
from radar.collectors.feedutil import entry_published, parse_feed
from radar.net import BROWSER_UA, Http, HttpError
from radar.normalization.models import RawItem
from radar.util import normalize_ws

FEED = "https://www.youtube.com/feeds/videos.xml"
_CHANNEL_ID = re.compile(r'"(?:channelId|externalId)":"(UC[A-Za-z0-9_-]{22})"')
_META_CHANNEL = re.compile(r'<meta itemprop="(?:channelId|identifier)" content="(UC[A-Za-z0-9_-]{22})"')


def feed_url(identifier: str) -> str:
    if identifier.startswith("http"):
        return identifier
    if identifier.startswith("PL"):
        return f"{FEED}?playlist_id={identifier}"
    return f"{FEED}?channel_id={identifier}"


async def resolve_channel_id(http: Http, url_or_handle: str) -> str | None:
    """Resolve youtube.com/@handle or /c/name to a UC… channel id by reading the page."""
    if re.fullmatch(r"UC[A-Za-z0-9_-]{22}", url_or_handle):
        return url_or_handle
    url = url_or_handle if url_or_handle.startswith("http") else f"https://www.youtube.com/{url_or_handle.lstrip('/')}"
    try:
        resp = await http.get(url, headers={"User-Agent": BROWSER_UA}, max_bytes=3_000_000)
    except HttpError:
        return None
    text = resp.text
    m = _META_CHANNEL.search(text) or _CHANNEL_ID.search(text)
    return m.group(1) if m else None


class YouTubeCollector(Collector):
    kind = "youtube"
    content_type = "youtube_video"

    async def fetch(self, source: SourceSpec) -> FetchResult:
        try:
            resp = await self.http.get(feed_url(source.identifier))
        except HttpError as exc:
            raise CollectorError(str(exc), exc.status_code, permanent=exc.status_code == 404) from exc
        try:
            parsed = parse_feed(resp.content)
        except ValueError as exc:
            raise CollectorError(str(exc), resp.status_code) from exc
        channel = parsed.feed.get("title")
        items: list[RawItem] = []
        for entry in parsed.entries[: source.limit(15)]:
            vid = entry.get("yt_videoid")
            link = entry.get("link") or (f"https://www.youtube.com/watch?v={vid}" if vid else None)
            if not link:
                continue
            desc = ""
            if entry.get("media_group") or entry.get("summary"):
                desc = entry.get("summary", "")
            stats = entry.get("media_statistics") or {}
            rating = entry.get("media_starrating") or {}
            item = RawItem(
                url=link,
                title=normalize_ws(entry.get("title", "")),
                content_type="youtube_video",
                source_item_id=vid or link,
                text=desc or None,
                excerpt=desc[:600] or None,
                author=entry.get("author") or channel,
                published_at=entry_published(entry),
                external_score=int(stats.get("views", 0) or 0) or None,
                meta={"channel": channel, "video_id": vid, "views": stats.get("views"),
                      "likes": rating.get("count")},
            )
            for thumb in entry.get("media_thumbnail", []) or []:
                item.add_image("youtube_thumb", thumb.get("url"))
            if vid:
                item.add_image("youtube_thumb", f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg")
            items.append(item)
        return FetchResult(items=items, status_code=resp.status_code)
