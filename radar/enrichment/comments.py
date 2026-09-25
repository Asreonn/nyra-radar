"""Discussion threads for community posts (Reddit, Hacker News).

Threads are fetched lazily and cached in `comment_summaries`: the worker prefetches the top
candidates (`pipeline.comments_per_cycle`), and the API fetches on demand when a post is
opened. Nothing here invents counts; Reddit's Atom thread feed has no votes or depth, so those
fields stay null.

- Reddit: OAuth JSON -> anonymous old.reddit JSON (skipped while a network-wide block is
  remembered in KV `reddit.json_blocked_until`) -> www.reddit.com thread Atom feed.
- Hacker News: a single Algolia `items/<id>` request returns the whole tree.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from radar.collectors.feedutil import entry_html, entry_published
from radar.collectors.reddit import (
    JSON_BLOCK_HOURS,
    WWW,
    RedditAuth,
    post_to_raw,
    reddit_json,
    reddit_rss,
    rss_entry_to_raw,
    selftext_with_images,
)
from radar.config import Config
from radar.db.models import KV, CommentSummary, CommunityPost, Item, ItemScore
from radar.extraction.images import order_candidates, reopen_image_stage
from radar.net import Http, HttpError
from radar.normalization.models import RawItem
from radar.util import html_to_text, iso, normalize_ws, parse_datetime, strip_html, truncate, utcnow

MAX_COMMENTS = 60
MAX_DEPTH = 5
BOT_AUTHORS = {"AutoModerator", "[deleted]"}
BLOCK_KEY = "reddit.json_blocked_until"


def _comment(cid: str, author: str | None, text: str, depth: int, created: Any,
             score: int | None = None) -> dict[str, Any]:
    return {"id": cid, "author": author, "text": text, "depth": depth, "score": score,
            "created_at": iso(parse_datetime(created))}


def _walk_reddit(children: list[dict[str, Any]], depth: int, out: list[dict[str, Any]]) -> None:
    for child in children:
        if len(out) >= MAX_COMMENTS or child.get("kind") != "t1":
            continue
        data = child.get("data") or {}
        body = (data.get("body") or "").strip()
        if body and data.get("author") not in BOT_AUTHORS and body not in ("[removed]", "[deleted]"):
            out.append(_comment(data.get("id") or "", data.get("author"), body, depth, data.get("created_utc"),
                                data.get("score")))
        replies = data.get("replies")
        if isinstance(replies, dict) and depth + 1 < MAX_DEPTH:
            _walk_reddit((replies.get("data") or {}).get("children") or [], depth + 1, out)


def _walk_hn(children: list[dict[str, Any]], depth: int, out: list[dict[str, Any]]) -> None:
    for child in children:
        if len(out) >= MAX_COMMENTS:
            return
        text = html_to_text(child.get("text"))
        if text and child.get("author"):
            out.append(_comment(str(child.get("id")), child.get("author"), text, depth,
                                child.get("created_at_i"), child.get("points")))
        if depth + 1 < MAX_DEPTH:
            _walk_hn(child.get("children") or [], depth + 1, out)


def _json_blocked(session: Session) -> bool:
    row = session.get(KV, BLOCK_KEY)
    until = parse_datetime((row.value or {}).get("until")) if row and row.value else None
    return bool(until and until > utcnow())


def _mark_json_blocked(session: Session) -> None:
    row = session.get(KV, BLOCK_KEY) or KV(key=BLOCK_KEY)
    session.add(row)
    row.value = {"until": iso(utcnow() + timedelta(hours=JSON_BLOCK_HOURS))}


async def _reddit_thread(session: Session, http: Http, config: Config,
                         post: CommunityPost) -> tuple[list[dict[str, Any]], str, RawItem | None]:
    """Comments plus the post itself as the thread presents it (the most complete copy of its text)."""
    sub = post.community or "all"
    auth = RedditAuth(http, config)
    if auth.enabled or not _json_blocked(session):
        try:
            data = await reddit_json(http, auth, f"/r/{sub}/comments/{post.post_id}",
                                     {"limit": MAX_COMMENTS, "depth": MAX_DEPTH, "sort": "top"})
            out: list[dict[str, Any]] = []
            first = None
            if isinstance(data, list) and len(data) > 1:
                _walk_reddit((data[1].get("data") or {}).get("children") or [], 0, out)
                children = (data[0].get("data") or {}).get("children") or []
                first = post_to_raw(children[0].get("data") or {}) if children else None
            return out, "oauth" if auth.enabled else "json", first
        except (HttpError, ValueError, KeyError):
            if not auth.enabled:
                _mark_json_blocked(session)
    parsed = await reddit_rss(http, f"{WWW}/r/{sub}/comments/{post.post_id}/.rss",
                              {"limit": MAX_COMMENTS, "sort": "top"})
    out = []
    first = None
    for entry in parsed.entries:
        cid = entry.get("id") or ""
        author = (entry.get("author") or "").removeprefix("/u/").strip()
        if cid.startswith("t3_"):
            first = rss_entry_to_raw(entry)
            continue
        if not cid.startswith("t1_") or author in BOT_AUTHORS:
            continue
        text, _ = selftext_with_images(entry_html(entry))
        if text:
            out.append(_comment(cid.removeprefix("t1_"), author or None, text, 0, entry_published(entry)))
    return out[:MAX_COMMENTS], "rss", first


def refresh_post(item: Item, raw: RawItem | None) -> bool:
    """Take the thread's copy of a Reddit post when it is fuller than what the listing gave us:
    longer text, inline images, better picture candidates."""
    if raw is None:
        return False
    changed = False
    text = (raw.text or "").strip()
    if text and len(text) > len(item.raw_text or ""):
        item.raw_text = text
        clean = strip_html(text) if raw.text_is_html else normalize_ws(text)
        if len(clean) > len(item.clean_text or ""):
            item.clean_text = clean
            item.excerpt = truncate(clean, 700)
            item.embedding_state = "stale" if item.embedding_state == "done" else item.embedding_state
        changed = True
    meta = dict(item.meta or {})
    for key in ("inline_images", "media_type", "media_url"):
        if raw.meta.get(key) and not meta.get(key):
            meta[key] = raw.meta[key]
            changed = True
    before = [tuple(c) for c in meta.get("image_candidates", [])]
    after = order_candidates(before + [tuple(c) for c in raw.image_candidates])
    if after != before:
        meta["image_candidates"] = after
        reopen_image_stage(item, before, after)
        changed = True
    if changed:
        item.meta = meta
    return changed


async def _hn_thread(http: Http, post: CommunityPost) -> tuple[list[dict[str, Any]], str]:
    data = (await http.get(f"https://hn.algolia.com/api/v1/items/{post.post_id}", retries=1)).json()
    out: list[dict[str, Any]] = []
    _walk_hn(data.get("children") or [], 0, out)
    return out, "algolia"


async def fetch_thread(session: Session, http: Http, config: Config, item: Item) -> CommentSummary:
    """Fetch and cache the discussion for one community item. Raises HttpError/ValueError."""
    post = session.get(CommunityPost, item.id)
    if post is None or post.platform not in ("reddit", "hackernews"):
        raise ValueError("item has no supported discussion thread")
    session.commit()  # no transaction stays open across the network fetch (WAL snapshot rule)
    try:
        if post.platform == "reddit":
            comments, via, first = await _reddit_thread(session, http, config, post)
            refresh_post(item, first)
        else:
            comments, via = await _hn_thread(http, post)
    except (HttpError, ValueError):
        item.comments_state = "failed"
        raise
    row = session.scalar(select(CommentSummary).where(CommentSummary.item_id == item.id))
    if row is None:
        row = CommentSummary(item_id=item.id, platform=post.platform)
        session.add(row)
    row.comments = comments
    row.sampled = len(comments)
    row.fetched_at = utcnow()
    row.state = "fetched"
    row.summary = {"via": via}
    item.comments_state = "done"
    return row


def thread_dict(row: CommentSummary | None, *, stale: bool = False, error: str | None = None) -> dict:
    return {"comments": row.comments if row else [], "count": row.sampled if row else 0,
            "fetched_at": iso(row.fetched_at) if row else None,
            "via": (row.summary or {}).get("via") if row else None, "stale": stale, "error": error}


async def get_thread(session: Session, http: Http, config: Config, item: Item, *,
                     max_age_minutes: int = 20, refresh: bool = False) -> dict:
    """Cached thread when fresh enough, otherwise a live fetch that degrades to the cache."""
    row = session.scalar(select(CommentSummary).where(CommentSummary.item_id == item.id))
    if row and not refresh and row.fetched_at >= utcnow() - timedelta(minutes=max_age_minutes):
        return thread_dict(row)
    try:
        return thread_dict(await fetch_thread(session, http, config, item))
    except (HttpError, ValueError) as exc:
        return thread_dict(row, stale=row is not None, error=str(exc)[:300])


async def prefetch_threads(session: Session, http: Http, config: Config, limit: int = 6) -> dict[str, int]:
    """Worker stage: cache threads of the best-ranked community candidates."""
    rows = session.scalars(
        select(Item).join(ItemScore, ItemScore.item_id == Item.id)
        .join(CommunityPost, CommunityPost.item_id == Item.id)
        .where(ItemScore.is_candidate.is_(True), Item.comments_state == "none", Item.is_hidden.is_(False),
               CommunityPost.platform.in_(("reddit", "hackernews")),
               # Atom-collected Reddit posts have unknown comment counts; HN counts are exact.
               or_(CommunityPost.platform == "reddit", CommunityPost.num_comments > 0))
        .order_by(ItemScore.final_score.desc()).limit(limit)
    ).all()
    done = failed = 0
    for item in rows:
        try:
            await fetch_thread(session, http, config, item)
            done += 1
        except (HttpError, ValueError):
            failed += 1
        session.commit()
    return {"attempted": len(rows), "done": done, "failed": failed}
