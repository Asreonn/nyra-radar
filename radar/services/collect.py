"""Collector orchestration, health accounting, and adaptive scheduling."""
from __future__ import annotations

import random
import time
from datetime import timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from radar.collectors import COLLECTORS, CollectorError, MissingCredentials, SourceSpec
from radar.config import Config
from radar.db.models import FetchLog, Source
from radar.net import Http
from radar.services.ingest import IngestStats, ingest_items
from radar.services.settings import get as get_setting
from radar.util import utcnow

DEFAULT_SOURCES = (
    ("hackernews", "Hacker News · top", "top", "https://news.ycombinator.com", {}),
    ("hackernews", "Hacker News · best", "best", "https://news.ycombinator.com/best", {}),
    ("reddit", "Reddit · LocalLLaMA", "LocalLLaMA", "https://reddit.com/r/LocalLLaMA",
     {"sorts": ["hot", "new"], "min_score": 3}),
    ("reddit", "Reddit · selfhosted", "selfhosted", "https://reddit.com/r/selfhosted", {}),
    ("reddit", "Reddit · opensource", "opensource", "https://reddit.com/r/opensource", {}),
    ("github_trending", "GitHub Trending · daily", "daily", "https://github.com/trending", {}),
    ("github_search", "GitHub · local AI", "local AI OR local LLM", "https://github.com/search",
     {"created_within_days": 30, "min_stars": 15}),
    ("github_search", "GitHub · coding agents", 'topic:ai-agent language:Python', "https://github.com/search",
     {"created_within_days": 30, "min_stars": 20}),
    ("rss", "Ars Technica", "https://feeds.arstechnica.com/arstechnica/index",
     "https://arstechnica.com", {}),
    ("rss", "TechCrunch", "https://techcrunch.com/feed/", "https://techcrunch.com", {}),
    ("rss", "The Register", "https://www.theregister.com/headlines.atom", "https://theregister.com", {}),
    ("google_news", "Google News · local AI", '"local AI" OR "local LLM"', "https://news.google.com", {}),
)


def ensure_default_sources(session: Session) -> int:
    count = 0
    for kind, name, identifier, url, config in DEFAULT_SOURCES:
        exists = session.scalar(select(Source).where(Source.kind == kind, Source.identifier == identifier))
        if exists is None:
            session.add(Source(kind=kind, name=name, identifier=identifier, url=url, config=config,
                               status="active", origin="seed"))
            count += 1
    return count


def _spec(source: Source) -> SourceSpec:
    return SourceSpec(id=source.id, kind=source.kind, identifier=source.identifier, name=source.name,
                      config=source.config or {}, etag=source.etag, last_modified=source.last_modified,
                      state=source.state or {}, status=source.status, last_success_at=source.last_success_at)


def _next_time(session: Session, source: Source, failed: bool = False) -> object:
    intervals = get_setting(session, "scheduler.intervals_min", {})
    base = source.interval_minutes or int(intervals.get(source.kind, 60))
    if failed:
        base = min(24 * 60, base * 2 ** min(source.consecutive_failures, 5))
    jitter = float(get_setting(session, "scheduler.jitter_pct", 0.15))
    return utcnow() + timedelta(minutes=base * random.uniform(1 - jitter, 1 + jitter))


async def fetch_source(session: Session, source: Source, http: Http, config: Config) -> IngestStats:
    started = time.monotonic()
    now = utcnow()
    source.last_fetch_at = now
    source.total_fetches += 1
    collector_cls = COLLECTORS.get(source.kind)
    if collector_cls is None:
        source.status = "disabled"
        source.last_error = f"unknown collector kind: {source.kind}"
        source.next_fetch_at = None
        return IngestStats()
    result = None
    try:
        result = await collector_cls(http, config).fetch(_spec(source))
        stats = ingest_items(session, source, result.items)
        source.successful_fetches += 1
        source.consecutive_failures = 0
        source.last_success_at = now
        source.last_error = None
        source.etag = result.etag or source.etag
        source.last_modified = result.last_modified or source.last_modified
        if result.state is not None:
            source.state = result.state
        source.next_fetch_at = _next_time(session, source)
        observations = source.item_count + source.duplicate_count
        source.duplicate_rate = source.duplicate_count / observations if observations else 0.0
        success_rate = source.successful_fetches / max(1, source.total_fetches)
        source.quality = max(0.05, min(1.0, 0.45 * success_rate + 0.35 * (1 - source.duplicate_rate) +
                                      0.20 * min(1.0, source.item_count / 25)))
        session.add(FetchLog(source_id=source.id, duration_ms=round((time.monotonic() - started) * 1000),
                             ok=True, status_code=result.status_code, items_seen=stats.seen,
                             items_new=stats.new, items_updated=stats.updated))
        return stats
    except MissingCredentials as exc:
        source.status = "disabled"
        source.last_error = str(exc)
        source.next_fetch_at = None
        error = exc
    except (CollectorError, OSError, ValueError) as exc:
        source.consecutive_failures += 1
        source.last_error = str(exc)[:2000]
        source.next_fetch_at = _next_time(session, source, failed=True)
        error = exc
    session.add(FetchLog(source_id=source.id, duration_ms=round((time.monotonic() - started) * 1000),
                         ok=False, status_code=getattr(error, "status_code", None), error=str(error)[:2000]))
    return IngestStats()


async def collect_due(session: Session, http: Http, config: Config, *, force: bool = False,
                      limit: int = 20) -> dict[str, int]:
    now = utcnow()
    query = select(Source).where(Source.status.in_(("active", "trial")))
    if not force:
        query = query.where(or_(Source.next_fetch_at.is_(None), Source.next_fetch_at <= now))
    sources = session.scalars(query.order_by(Source.next_fetch_at.asc()).limit(limit)).all()
    totals = {"sources": 0, "seen": 0, "new": 0, "updated": 0, "failed": 0}
    for source in sources:
        before = source.successful_fetches
        stats = await fetch_source(session, source, http, config)
        totals["sources"] += 1
        totals["seen"] += stats.seen
        totals["new"] += stats.new
        totals["updated"] += stats.updated
        totals["failed"] += int(source.successful_fetches == before)
        session.commit()  # keep each source isolated and visible to the UI
    return totals
