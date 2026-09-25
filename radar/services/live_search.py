"""Search beyond what has been collected: Reddit's public search feed and Hacker News (Algolia).

Results are ingested like any other item, so they open in the reader with full text and
comments, but posts that only arrived through a search are marked `meta.via_search` and kept
out of the ranked feeds (they may be old, and they were not chosen by the user's sources).
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from radar.collectors.hackernews import ALGOLIA, story_to_raw
from radar.collectors.reddit import WWW, reddit_rss, rss_entry_to_raw
from radar.db.models import Item
from radar.extraction.canonical_url import canonicalize, url_hash
from radar.net import Http
from radar.normalization.models import RawItem
from radar.ranking import score_item
from radar.services.ingest import ingest_items

WHERE = ("reddit", "hackernews")


async def _reddit(http: Http, q: str, limit: int, period: str) -> list[RawItem]:
    parsed = await reddit_rss(http, f"{WWW}/search/.rss", {"q": q, "sort": "relevance", "t": period,
                                                          "limit": min(50, limit), "type": "link"})
    out = []
    for rank, entry in enumerate(parsed.entries):
        raw = rss_entry_to_raw(entry)
        if raw:
            raw.meta.update({"reddit_sort": "search", "reddit_rank": rank, "reddit_listing_size": len(parsed.entries)})
            out.append(raw)
    return out


async def _hackernews(http: Http, q: str, limit: int) -> list[RawItem]:
    data = (await http.get(f"{ALGOLIA}/search", params={"query": q, "tags": "story",
                                                        "hitsPerPage": min(50, limit)})).json()
    return [raw for hit in data.get("hits", []) if (raw := story_to_raw(hit))]


async def live_search(session: Session, http: Http, q: str, where: str = "reddit", limit: int = 25,
                      period: str = "year") -> list[Item]:
    if where not in WHERE:
        raise ValueError(f"unsupported search target: {where}")
    raws: list[RawItem] = await (_reddit(http, q, limit, period) if where == "reddit" else _hackernews(http, q, limit))
    hashes = [url_hash(canonicalize(r.url)) for r in raws]
    known = set(session.scalars(select(Item.url_hash).where(Item.url_hash.in_(hashes)))) if hashes else set()
    ingest_items(session, None, raws)
    by_hash: dict[str, Item] = {i.url_hash: i for i in session.scalars(select(Item).where(Item.url_hash.in_(hashes)))}
    ordered: list[Item] = []
    for h in hashes:
        item = by_hash.get(h)
        if item is None or item in ordered:
            continue
        if h not in known:  # arrived only because of this search
            meta: dict[str, Any] = dict(item.meta or {})
            item.meta = meta | {"via_search": True}
        if item.score is None:
            score_item(session, item)
        ordered.append(item)
    return ordered
