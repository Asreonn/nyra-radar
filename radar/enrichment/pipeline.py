"""Bounded enrichment stages. Every stage is independently retryable."""
from __future__ import annotations

import asyncio

from sqlalchemy import select
from sqlalchemy.orm import Session

from radar.config import Config
from radar.db.models import Article, Image, Item, ItemScore
from radar.extraction.article import extract_article
from radar.extraction.canonical_url import url_hash, youtube_video_id
from radar.extraction.images import ImageCache, ImageRejected, order_candidates, reopen_image_stage
from radar.net import Http, HttpError
from radar.services.search import index_items
from radar.services.settings import get as get_setting
from radar.util import utcnow


async def extract_item(session: Session, http: Http, item: Item) -> bool:
    """Fetch and extract the page an item points to (the linked article for community posts)."""
    target = item.link_url or item.canonical_url
    if item.content_type != "youtube_video" and youtube_video_id(target):
        item.extraction_state = "skipped"  # script-rendered watch page; the reader embeds the video
        return False
    session.commit()  # no transaction stays open across the fetch (see cache_item_image)
    try:
        response = await http.get(target, headers={"Accept": "text/html,application/xhtml+xml"},
                                  max_bytes=6_000_000, retries=1)
        # Parsing a large page takes CPU seconds; off the event loop, the API keeps answering meanwhile.
        result = await asyncio.to_thread(extract_article, response.text, response.url, item.clean_text or item.excerpt)
    except (HttpError, ValueError, OSError) as exc:
        item.extraction_state = "failed"
        item.stage_attempts = dict(item.stage_attempts or {}) | {"extraction": str(exc)[:300]}
        return False
    article = session.get(Article, item.id) or Article(item_id=item.id)
    session.add(article)
    article.site_name = result.meta.site_name
    article.raw_extraction = result.raw_text
    article.extraction_method = result.method
    article.word_count = result.word_count
    article.reading_minutes = round(result.word_count / 220, 1) if result.word_count else None
    article.page_canonical_url = result.meta.canonical_url
    article.og = result.meta.raw
    article.extracted_at = utcnow()
    article.http_status = response.status_code
    if result.clean_text and len(result.clean_text) > len(item.clean_text or ""):
        item.clean_text = result.clean_text
        item.excerpt = item.excerpt or result.meta.description
        item.embedding_state = "stale" if item.embedding_state == "done" else "pending"
    before = [tuple(c) for c in (item.meta or {}).get("image_candidates", [])]
    after = order_candidates(before + result.meta.image_candidates())
    item.meta = dict(item.meta or {}) | {"image_candidates": after, "page_feeds": result.meta.feeds}
    reopen_image_stage(item, before, after)
    item.extraction_state = "done"
    return True


async def extract_pending(session: Session, http: Http, limit: int = 30) -> dict[str, int]:
    # Best-ranked first: the items most likely to be read get their article and picture soonest.
    items = session.scalars(
        select(Item).outerjoin(ItemScore, ItemScore.item_id == Item.id).where(Item.extraction_state == "pending")
        .order_by(ItemScore.final_score.desc().nulls_last(), Item.first_seen_at.desc()).limit(limit)).all()
    done = failed = 0
    touched = []
    for item in items:
        if await extract_item(session, http, item):
            done += 1
            touched.append(item.id)
        else:
            failed += 1
        session.commit()
    index_items(session, touched)
    return {"attempted": len(items), "done": done, "failed": failed}


async def cache_item_image(session: Session, cache: ImageCache, item: Item) -> bool:
    """Try the item's candidates best-first. Downloads happen before any row is written, so the
    SQLite write lock is never held across network I/O (the API stays responsive to clicks)."""
    candidates = order_candidates(list((item.meta or {}).get("image_candidates", [])))
    for kind, url in candidates[:5]:
        row = session.scalar(select(Image).where(Image.url_hash == url_hash(url)))
        if row and row.status == "ok":
            item.image_id, item.image_url, item.image_cache_path = row.id, url, row.cache_path
            item.image_state = "done"
            return True
        if row and row.status in ("failed", "rejected") and row.fail_count >= 2:
            continue
        # End the transaction before waiting on the network: under WAL a transaction that read before
        # another connection committed cannot write afterwards ("database is locked" at once).
        session.commit()
        try:
            image, etag = await cache.download(url, kind, row.etag if row else None)
        except ImageRejected:
            row = row or Image(original_url=url, url_hash=url_hash(url), kind=kind)
            session.add(row)
            row.fail_count = (row.fail_count or 0) + 1
            row.status = "rejected"
            continue
        if image is None and (row is None or row.status != "ok"):
            continue
        row = row or Image(original_url=url, url_hash=url_hash(url), kind=kind)
        session.add(row)
        if image:
            row.content_hash, row.mime, row.width, row.height = image.content_hash, image.mime, image.width, image.height
            row.bytes, row.cache_path, row.status = image.bytes, image.rel_path, "ok"
            row.fetched_at, row.etag = utcnow(), etag
        session.flush()
        item.image_id, item.image_url, item.image_cache_path = row.id, url, row.cache_path
        item.image_state = "done"
        return True
    item.image_state = "none"
    return False


def image_cache(session: Session, http: Http) -> ImageCache:
    from radar.paths import image_cache_dir

    return ImageCache(http, image_cache_dir(), max_width=int(get_setting(session, "images.max_width", 1280)))


async def cache_images(session: Session, http: Http, config: Config, limit: int = 20) -> dict[str, int]:
    items = session.scalars(select(Item).where(Item.image_state == "pending")
                            .order_by(Item.first_seen_at.desc()).limit(limit)).all()
    cache = image_cache(session, http)
    done = failed = 0
    for item in items:
        had_candidates = bool((item.meta or {}).get("image_candidates"))
        accepted = await cache_item_image(session, cache, item)
        done += int(accepted)
        failed += int(not accepted and had_candidates)
        session.commit()
    return {"attempted": len(items), "done": done, "failed": failed}
