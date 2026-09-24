"""Hacker News collector.

- kind `hackernews`: identifier is a list (top | best | new | ask | show). Story IDs come
  from the Firebase list endpoint and are hydrated with ONE Algolia request per 50 IDs
  (points, comments, url, text), falling back to per-item Firebase calls.
- kind `hn_search`: identifier is a keyword query (Algolia search_by_date), used for
  topic tracking and as the broad-search fallback in source discovery.
Comments are fetched separately (radar.enrichment.comments) and only for candidates.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from radar.collectors.base import Collector, CollectorError, FetchResult, SourceSpec
from radar.net import HttpError
from radar.normalization.models import CommunityInfo, RawItem
from radar.util import parse_datetime, strip_html

FIREBASE = "https://hacker-news.firebaseio.com/v0"
ALGOLIA = "https://hn.algolia.com/api/v1"
LISTS = {"top": "topstories", "best": "beststories", "new": "newstories", "ask": "askstories",
         "show": "showstories"}


def discussion_url(story_id: int | str) -> str:
    return f"https://news.ycombinator.com/item?id={story_id}"


def _post_kind(title: str, has_url: bool) -> str:
    low = title.lower()
    if low.startswith("show hn"):
        return "show"
    if low.startswith("ask hn"):
        return "ask"
    if low.startswith("launch hn"):
        return "launch"
    return "story" if has_url else "self"


def story_to_raw(hit: dict[str, Any]) -> RawItem | None:
    sid = hit.get("objectID") or hit.get("id") or hit.get("story_id")
    title = (hit.get("title") or "").strip()
    if not sid or not title or hit.get("dead") or hit.get("deleted"):
        return None
    url = hit.get("url") or None
    text_html = hit.get("story_text") or hit.get("text") or ""
    points = int(hit.get("points") if hit.get("points") is not None else hit.get("score") or 0)
    comments = int(hit.get("num_comments") if hit.get("num_comments") is not None else hit.get("descendants") or 0)
    created = hit.get("created_at_i") or hit.get("time") or hit.get("created_at")
    kind = _post_kind(title, bool(url))
    item = RawItem(
        url=discussion_url(sid),
        title=title,
        content_type="hackernews_post",
        source_item_id=str(sid),
        text=text_html or None,
        text_is_html=True,
        excerpt=strip_html(text_html)[:600] or None,
        author=hit.get("author") or hit.get("by"),
        published_at=parse_datetime(created),
        link_url=url,
        external_score=points,
        external_comment_count=comments,
        community=CommunityInfo(platform="hackernews", post_id=str(sid), discussion_url=discussion_url(sid),
                                community="hn", score=points, num_comments=comments, post_kind=kind,
                                is_self=not bool(url)),
        meta={"hn_kind": kind},
    )
    if url:
        item.links.append(url)
    return item


class HackerNewsCollector(Collector):
    kind = "hackernews"
    content_type = "hackernews_post"

    async def fetch(self, source: SourceSpec) -> FetchResult:
        list_name = LISTS.get(source.identifier, source.identifier)
        limit = source.limit(30)
        min_points = int(source.config.get("min_points", 0))
        try:
            ids = (await self.http.get(f"{FIREBASE}/{list_name}.json")).json()[:limit]
        except HttpError as exc:
            raise CollectorError(str(exc), exc.status_code) from exc
        hits = await self._hydrate(ids)
        items = []
        for hit in hits:
            raw = story_to_raw(hit)
            if raw and (raw.external_score or 0) >= min_points:
                raw.meta["hn_list"] = source.identifier
                items.append(raw)
        return FetchResult(items=items, status_code=200)

    async def _hydrate(self, ids: list[int]) -> list[dict[str, Any]]:
        found: dict[str, dict[str, Any]] = {}
        for i in range(0, len(ids), 50):
            chunk = ids[i:i + 50]
            tags = "(" + ",".join(f"story_{x}" for x in chunk) + ")"
            try:
                data = (await self.http.get(f"{ALGOLIA}/search", params={"tags": tags, "hitsPerPage": 60})).json()
                for hit in data.get("hits", []):
                    found[str(hit.get("objectID"))] = hit
            except HttpError:
                pass
        missing = [x for x in ids if str(x) not in found]
        if missing:
            async def one(sid: int) -> dict[str, Any] | None:
                try:
                    data = (await self.http.get(f"{FIREBASE}/item/{sid}.json")).json()
                    return data if data and data.get("type") in ("story", "job", "poll") else None
                except HttpError:
                    return None
            for data in await asyncio.gather(*(one(x) for x in missing[:40])):
                if data:
                    found[str(data["id"])] = data
        order = {str(x): n for n, x in enumerate(ids)}
        return sorted(found.values(), key=lambda h: order.get(str(h.get("objectID") or h.get("id")), 1e9))


class HNSearchCollector(Collector):
    kind = "hn_search"
    content_type = "hackernews_post"

    async def fetch(self, source: SourceSpec) -> FetchResult:
        days = float(source.config.get("days", 4))
        min_points = int(source.config.get("min_points", 5))
        params = {
            "query": source.identifier,
            "tags": source.config.get("tags", "story"),
            "numericFilters": f"created_at_i>{int(time.time() - days * 86400)},points>={min_points}",
            "hitsPerPage": source.limit(25),
        }
        try:
            data = (await self.http.get(f"{ALGOLIA}/search_by_date", params=params)).json()
        except HttpError as exc:
            raise CollectorError(str(exc), exc.status_code) from exc
        items = []
        for hit in data.get("hits", []):
            raw = story_to_raw(hit)
            if raw:
                raw.meta["hn_query"] = source.identifier
                items.append(raw)
        return FetchResult(items=items, status_code=200)
