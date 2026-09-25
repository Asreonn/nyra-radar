"""Community browsing (Reddit first, Hacker News too): per-subreddit feeds and source health.

This is the "browse it like Reddit" surface next to the mixed digest. Sorting works only on
stored evidence: `hot` combines the normalised community signal (votes per subreddit, or
Reddit's own listing position for Atom-collected posts), the interest model and age; `top` and
`discussed` use raw counts where known.
"""
from __future__ import annotations

import re
from datetime import timedelta
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from radar.db.models import CommunityPost, Item, ItemScore, Source
from radar.util import iso, redact_feed_tokens, utcnow

SORTS = ("hot", "top", "new", "discussed", "best")
HOT_HALF_LIFE_HOURS = 18.0
SUBREDDIT = re.compile(r"^[A-Za-z0-9_]{2,50}(\+[A-Za-z0-9_]{2,50})*$")


def mask_feed_url(value: str) -> str:
    """Hide private feed tokens (reddit.com/prefs/feeds) wherever identifiers are displayed."""
    return redact_feed_tokens(value) or value


def feed_excluded(item: Item) -> bool:
    """Posts the user already reacted to on Reddit, or that only arrived through a live search,
    stay out of the ranked feeds (they remain searchable and openable)."""
    meta = item.meta or {}
    return bool(meta.get("reddit_account") or meta.get("via_search"))


def normalize_subreddit(value: str) -> str | None:
    """'r/LocalLLaMA', '/r/rust/', 'https://www.reddit.com/r/linux/top' -> subreddit name.
    Reddit feed URLs (private front page, multireddits as .rss) are kept whole."""
    text = value.strip()
    if text.startswith("http"):
        parts = urlsplit(text)
        if not (parts.hostname or "").endswith("reddit.com"):
            return None
        if parts.path.rstrip("/").endswith((".rss", ".json")) or "feed=" in parts.query:
            return urlunsplit(("https", "www.reddit.com", parts.path, parts.query, ""))
        match = re.match(r"^/r/([^/]+)", parts.path)
        text = match.group(1) if match else ""
    text = text.removeprefix("/").removeprefix("r/").strip("/")
    return text if SUBREDDIT.match(text) else None


def _votes_known(post: CommunityPost, item: Item) -> bool:
    return post.platform != "reddit" or bool(post.score or post.num_comments) or \
        (item.meta or {}).get("via") != "rss"


def _age_hours(item: Item) -> float:
    return max(0.0, (utcnow() - (item.published_at or item.first_seen_at)).total_seconds() / 3600)


def _sort_key(sort: str, item: Item, post: CommunityPost, score: ItemScore | None):
    signal = score.community if score else 0.3
    if sort == "new":
        return item.published_at or item.first_seen_at
    if sort == "top":
        return (post.score or 0, signal)
    if sort == "discussed":
        return (post.num_comments or 0, signal)
    if sort == "best":
        return score.final_score if score else 0.0
    interest = score.interest if score else 0.5
    return (0.15 + signal) * (0.75 + 0.5 * interest) * 2 ** (-_age_hours(item) / HOT_HALF_LIFE_HOURS)


def community_feed(session: Session, *, platform: str = "reddit", community: str | None = None,
                   sort: str = "hot", hours: int = 72, limit: int = 50, offset: int = 0) -> list[Item]:
    cutoff = utcnow() - timedelta(hours=hours)
    stmt = (select(Item, CommunityPost, ItemScore)
            .join(CommunityPost, CommunityPost.item_id == Item.id)
            .outerjoin(ItemScore, ItemScore.item_id == Item.id)
            .where(CommunityPost.platform == platform, Item.is_hidden.is_(False),
                   Item.duplicate_of.is_(None), func.coalesce(Item.published_at, Item.first_seen_at) >= cutoff))
    if community:
        names = [c.strip().lower() for c in community.split(",") if c.strip()]
        stmt = stmt.where(func.lower(CommunityPost.community).in_(names))
    rows = session.execute(stmt.order_by(Item.first_seen_at.desc()).limit(3000)).all()
    rows = [r for r in rows if not feed_excluded(r[0])]
    rows.sort(key=lambda r: _sort_key(sort, *r), reverse=True)
    return [r[0] for r in rows[offset:offset + limit]]


def community_info(post: CommunityPost, item: Item) -> dict:
    meta = item.meta or {}
    known = _votes_known(post, item)
    listing = None
    if meta.get("reddit_rank") is not None:
        listing = {"sort": meta.get("reddit_sort"), "rank": int(meta["reddit_rank"]) + 1,
                   "size": meta.get("reddit_listing_size")}
    return {"platform": post.platform, "name": post.community, "kind": post.post_kind,
            "is_self": post.is_self, "flair": post.flair, "upvote_ratio": post.upvote_ratio,
            "score": post.score if known else None, "comments": post.num_comments if known else None,
            "votes_known": known, "discussion_url": post.discussion_url, "linked_url": post.linked_url,
            "media_url": meta.get("media_url"), "media": meta.get("media_type"), "listing": listing}


def _source_label(source: Source) -> str:
    if source.identifier.startswith("http"):  # front page / upvoted / saved / multireddit feed
        return source.name.removeprefix("Reddit · ")
    return source.identifier.removeprefix("r/")


def list_subreddits(session: Session) -> list[dict]:
    """Subreddits with collected counts plus the health of the source that feeds them."""
    day = utcnow() - timedelta(hours=24)
    counts = session.execute(
        select(func.lower(CommunityPost.community), func.max(CommunityPost.community), func.count(Item.id),
               func.sum(case((Item.first_seen_at >= day, 1), else_=0)), func.max(Item.first_seen_at))
        .join(Item, Item.id == CommunityPost.item_id)
        .where(CommunityPost.platform == "reddit", Item.is_hidden.is_(False))
        .group_by(func.lower(CommunityPost.community))
    ).all()
    stats = {key: {"name": name, "items": total, "items_24h": int(recent or 0), "latest_at": iso(latest)}
             for key, name, total, recent, latest in counts if key}
    out: list[dict] = []
    for source in session.scalars(select(Source).where(Source.kind == "reddit").order_by(Source.name)):
        label = _source_label(source)
        key = label.lower()
        row = stats.pop(key, None) or {"name": label, "items": 0, "items_24h": 0, "latest_at": None}
        out.append({**row, "name": row["name"] or label, "feed": source.identifier.startswith("http"),
                    "source": {"id": source.id, "status": source.status,
                               "last_error": redact_feed_tokens(source.last_error),
                               "last_success_at": iso(source.last_success_at),
                               "next_fetch_at": iso(source.next_fetch_at),
                               "access": (source.state or {}).get("last_access"),
                               "consecutive_failures": source.consecutive_failures}})
    # Subreddits that only arrive through the front-page feed or cross-links.
    out += [{**row, "feed": False, "source": None} for row in stats.values()]
    out.sort(key=lambda r: (r["source"] is None, -r["items_24h"], -r["items"], r["name"].lower()))
    return out



def keep_reading(session: Session, *, offset: int = 0, limit: int = 20, hours: int = 96,
                 exclude: set[int] | None = None) -> list[Item]:
    """Everything worth reading, best first, one item per story, for the endless tail of Today."""
    cutoff = utcnow() - timedelta(hours=hours)
    rows = session.execute(
        select(Item, ItemScore).join(ItemScore, ItemScore.item_id == Item.id)
        .where(Item.first_seen_at >= cutoff, Item.is_hidden.is_(False), Item.duplicate_of.is_(None))
        .order_by(ItemScore.final_score.desc()).limit(3000)
    ).all()
    seen_clusters: set[int] = set()
    out: list[Item] = []
    for item, _score in rows:
        if feed_excluded(item) or (exclude and item.id in exclude):
            continue
        if item.cluster_id:
            if item.cluster_id in seen_clusters:
                continue
            seen_clusters.add(item.cluster_id)
        out.append(item)
    return out[offset:offset + limit]
