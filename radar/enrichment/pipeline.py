"""Bounded enrichment stages. Every stage is independently retryable."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from radar.config import Config
from radar.db.models import Article, Image, Item
from radar.extraction.article import extract_article
from radar.extraction.canonical_url import url_hash
from radar.extraction.images import ImageCache, ImageRejected, order_candidates
from radar.net import Http, HttpError
from radar.services.search import index_items
from radar.services.settings import get as get_setting
from radar.util import utcnow


async def extract_pending(session: Session, http: Http, limit: int = 30) -> dict[str, int]:
    items = session.scalars(select(Item).where(Item.extraction_state == "pending")
                            .order_by(Item.first_seen_at.desc()).limit(limit)).all()
    done = failed = 0
    touched = []
    for item in items:
        target = item.link_url or item.canonical_url
        try:
            response = await http.get(target, headers={"Accept": "text/html,application/xhtml+xml"},
                                      max_bytes=6_000_000, retries=1)
            result = extract_article(response.text, response.url, item.clean_text or item.excerpt)
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
            cands = list((item.meta or {}).get("image_candidates", [])) + result.meta.image_candidates()
            item.meta = dict(item.meta or {}) | {"image_candidates": order_candidates(cands),
                                                 "page_feeds": result.meta.feeds}
            item.extraction_state = "done"
            done += 1
            touched.append(item.id)
        except (HttpError, ValueError, OSError) as exc:
            item.extraction_state = "failed"
            item.stage_attempts = dict(item.stage_attempts or {}) | {"extraction": str(exc)[:300]}
            failed += 1
        session.commit()
    index_items(session, touched)
    return {"attempted": len(items), "done": done, "failed": failed}


async def cache_images(session: Session, http: Http, config: Config, limit: int = 20) -> dict[str, int]:
    from radar.paths import image_cache_dir

    items = session.scalars(select(Item).where(Item.image_state == "pending")
                            .order_by(Item.first_seen_at.desc()).limit(limit)).all()
    cache = ImageCache(http, image_cache_dir(), max_width=int(get_setting(session, "images.max_width", 1280)))
    done = failed = 0
    for item in items:
        candidates = order_candidates(list((item.meta or {}).get("image_candidates", [])))
        accepted = False
        for kind, url in candidates[:5]:
            row = session.scalar(select(Image).where(Image.url_hash == url_hash(url)))
            if row and row.status == "ok":
                item.image_id, item.image_url, item.image_cache_path = row.id, url, row.cache_path
                accepted = True
                break
            if row and row.status in ("failed", "rejected") and row.fail_count >= 2:
                continue
            if row is None:
                row = Image(original_url=url, url_hash=url_hash(url), kind=kind)
                session.add(row)
                session.flush()
            try:
                image, etag = await cache.download(url, kind, row.etag)
                if image:
                    row.content_hash, row.mime, row.width, row.height = image.content_hash, image.mime, image.width, image.height
                    row.bytes, row.cache_path, row.status = image.bytes, image.rel_path, "ok"
                    row.fetched_at, row.etag = utcnow(), etag
                if row.status == "ok":
                    item.image_id, item.image_url, item.image_cache_path = row.id, url, row.cache_path
                    accepted = True
                    break
            except ImageRejected:
                row.fail_count += 1
                row.status = "rejected"
        item.image_state = "done" if accepted else "none"
        done += int(accepted)
        failed += int(not accepted and bool(candidates))
        session.commit()
    return {"attempted": len(items), "done": done, "failed": failed}
