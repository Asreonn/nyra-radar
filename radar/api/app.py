"""FastAPI application: browser API and stable Nyra tool surface."""
from __future__ import annotations

import asyncio
import functools
import logging
import re
import secrets
import threading
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Annotated, Any
from urllib.parse import urlsplit

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from radar import paths
from radar.api.serializers import cluster_dict, item_dict
from radar.clustering.embeddings import make_embedder
from radar.clustering.service import related_items
from radar.collectors import COLLECTORS
from radar.collectors.reddit import full_size_image
from radar.collectors.youtube import resolve_channel_id
from radar.config import get_config
from radar.db import get_engine, migrate, session_factory, session_scope
from radar.db.models import (
    DigestItem,
    DigestSession,
    DiscoveryCandidate,
    Image,
    Item,
    Repository,
    SavedItem,
    Source,
    StoryCluster,
)
from radar.discovery.service import decide_trial, start_trial
from radar.enrichment.comments import get_thread
from radar.enrichment.pipeline import cache_item_image, extract_item, image_cache
from radar.extraction.canonical_url import github_repo_from_url, url_hash, youtube_video_id
from radar.extraction.images import ImageRejected
from radar.net import Http, HttpError
from radar.ranking import build_feed, last_rescored, rescore, score_item, score_unscored
from radar.services import reddit_account
from radar.services.brief import BriefUnavailable, cached_brief, make_brief
from radar.services.catalog import catalog
from radar.services.collect import fetch_source
from radar.services.community import (
    SORTS,
    community_feed,
    keep_reading,
    list_subreddits,
    mask_feed_url,
    normalize_subreddit,
)
from radar.services.feedback import explain_interests, fold_events, record, undo_last
from radar.services.live_search import live_search
from radar.services.search import hybrid, index_items
from radar.services.settings import all_values, set_value
from radar.services.taste import profile, tune_candidates, why
from radar.util import iso, parse_datetime, redact_feed_tokens, utcnow

BODY_REQUIRED = Body(...)
BODY_OPTIONAL = Body(default={})


def _period_hours(period: str) -> int:
    match = re.fullmatch(r"(\d+)([hdw])", period.strip().lower())
    if not match:
        raise HTTPException(422, "period must look like 24h, 7d, or 2w")
    value = int(match.group(1))
    return value * {"h": 1, "d": 24, "w": 168}[match.group(2)]


def db_session():
    session = session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


DB = Annotated[Session, Depends(db_session)]


def commits(fn):
    """Commit inside the endpoint call, before it returns. The dependency's own commit runs later,
    after a trip through the event loop; a write transaction left open across that trip can stall an
    async endpoint waiting (on the loop) for SQLite's write lock, and with it every other request."""
    if asyncio.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def run_async(*args, **kwargs):
            result = await fn(*args, **kwargs)
            kwargs["db"].commit()
            return result
        return run_async

    @functools.wraps(fn)
    def run(*args, **kwargs):
        result = fn(*args, **kwargs)
        kwargs["db"].commit()
        return result
    return run


_http: Http | None = None


def api_http() -> Http:
    """One paced HTTP client for on-demand fetches (discussion threads) from the API process."""
    global _http
    if _http is None:
        _http = Http(max_concurrency=10)  # pictures, threads and articles load side by side
    return _http


_rescore = {"running": False, "again": False}
_rescore_lock = threading.Lock()
# The worker re-ranks every cycle; the API only steps in when it has not for a while (worker stopped).
RESCORE_MAX_AGE = timedelta(minutes=15)


def _rescore_job(days: int) -> None:
    with _rescore_lock:
        if _rescore["running"]:
            _rescore["again"] = True  # one more pass after the current one sees the latest feedback
            return
        _rescore["running"] = True
    try:
        while True:
            with session_scope() as session:
                rescore(session, days=days, background=True)
            with _rescore_lock:
                if not _rescore["again"]:
                    return
                _rescore["again"] = False
    except Exception as exc:  # noqa: BLE001 - the worker rescores every cycle anyway
        logging.getLogger(__name__).warning("background rescore failed: %s", exc)
    finally:
        with _rescore_lock:
            _rescore["running"] = False


_rescore_timer: threading.Timer | None = None


def request_rescore(days: int = 8, delay: float = 3.0) -> None:
    """Re-rank in the background (a full pass takes a few seconds); pages never wait for it. A burst
    of clicks (rating a dozen posts on Tune) settles into one pass `delay` seconds after the last."""
    global _rescore_timer
    with _rescore_lock:
        if _rescore_timer is not None:
            _rescore_timer.cancel()
        _rescore_timer = threading.Timer(delay, _rescore_job, args=(days,))
        _rescore_timer.daemon = True
        _rescore_timer.start()


def _warm_vector_index() -> None:
    from radar.clustering.vector_index import get_index
    from radar.db.models import Embedding

    try:
        with session_factory()() as session:
            model = session.scalar(select(Embedding.model).limit(1))
            if model:
                get_index(session, model)
    except Exception as exc:  # noqa: BLE001 - warming is an optimisation; search builds the index itself
        logging.getLogger(__name__).debug("vector index warm-up skipped: %s", exc)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _http
    paths.ensure_dirs()
    migrate(get_engine())
    asyncio.get_running_loop().run_in_executor(None, _warm_vector_index)  # first search is instant
    yield
    if _http is not None:
        await _http.close()
        _http = None


app = FastAPI(title="Nyra Radar", version="1.0.0", lifespan=lifespan,
              description="Local-first personal technology intelligence API")


@app.exception_handler(OperationalError)
async def database_busy(_request: Request, exc: OperationalError):
    """SQLite write contention (worker vs. UI) is transient: tell the client to retry."""
    if "locked" in str(exc).lower() or "busy" in str(exc).lower():
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": "database busy, retry"}, status_code=503, headers={"Retry-After": "1"})
    raise exc


@app.middleware("http")
async def private_access(request: Request, call_next):
    cfg = get_config().server
    host = request.client.host if request.client else "127.0.0.1"
    local = host in ("127.0.0.1", "::1", "localhost", "testclient")
    if not local and not cfg.allow_unauthenticated_lan:
        token = request.headers.get("authorization", "").removeprefix("Bearer ") or request.query_params.get("token")
        if not cfg.token or not secrets.compare_digest(token or "", cfg.token):
            from fastapi.responses import JSONResponse
            return JSONResponse({"detail": "authentication required"}, status_code=401)
    return await call_next(request)


@app.get("/api/v1/health")
def health(db: DB) -> dict:
    return {"status": "ok", "version": app.version, "time": iso(utcnow()),
            "counts": {"items": db.scalar(select(func.count(Item.id))) or 0,
                       "sources": db.scalar(select(func.count(Source.id))) or 0,
                       "projects": db.scalar(select(func.count(Repository.id))) or 0,
                       "clusters": db.scalar(select(func.count(StoryCluster.id))) or 0}}


@app.get("/api/v1/items")
def browse_items(db: DB, q: str | None = None, content_type: str | None = None,
                 source_id: int | None = None, saved: bool = False, history: bool = False,
                 period: str | None = None,
                 sort: str = Query("newest", pattern="^(newest|score|novelty)$"), limit: int = Query(50, le=200),
                 offset: int = Query(0, ge=0)) -> dict:
    stmt = select(Item).where(Item.is_hidden.is_(False))
    if content_type:
        stmt = stmt.where(Item.content_type == content_type)
    if source_id:
        stmt = stmt.where(Item.source_id == source_id)
    if saved:
        stmt = stmt.join(SavedItem, SavedItem.item_id == Item.id)
    if history:
        stmt = stmt.where(Item.is_read.is_(True))
    if period:
        cutoff = utcnow() - timedelta(hours=_period_hours(period))
        stmt = stmt.where(func.coalesce(Item.published_at, Item.first_seen_at) >= cutoff)
    if q:
        ids = [r["item_id"] for r in hybrid(db, q, None, None, limit=1000)]
        if not ids:
            return {"items": [], "total": 0, "offset": offset, "limit": limit}
        stmt = stmt.where(Item.id.in_(ids))
    if sort == "newest":
        stmt = stmt.order_by(Item.published_at.desc(), Item.first_seen_at.desc())
    else:
        from radar.db.models import ItemScore
        stmt = stmt.join(ItemScore, ItemScore.item_id == Item.id)
        stmt = stmt.order_by((ItemScore.novelty if sort == "novelty" else ItemScore.final_score).desc())
    rows = db.scalars(stmt.offset(offset).limit(limit)).all()
    return {"items": [item_dict(db, x) for x in rows], "total": len(rows), "offset": offset, "limit": limit}


@app.get("/api/v1/items/{item_id}")
def get_item(item_id: int, db: DB) -> dict:
    item = db.get(Item, item_id)
    if item is None:
        raise HTTPException(404, "item not found")
    return {"item": {**item_dict(db, item, detail=True), "why": why(db, item)}}


@app.post("/api/v1/items/{item_id}/hydrate")
@commits
async def hydrate(item_id: int, db: DB) -> dict:
    """Make an opened item complete: the linked article when it has not been extracted yet, and its
    picture. The discussion (and, for Reddit, the thread's fuller copy of the post) comes from
    /comments, which the reader requests in parallel, so neither waits on the other."""
    item = db.get(Item, item_id)
    if item is None:
        raise HTTPException(404, "item not found")
    http = api_http()
    extract_tries = int((item.stage_attempts or {}).get("on_demand_extractions", 0))
    wants_article = (bool(item.link_url) and not youtube_video_id(item.link_url)) or item.content_type in ("article", "rss_entry")
    # Every stage commits before the next network wait, so no write lock is held across I/O.
    if wants_article and item.extraction_state in ("pending", "failed") and extract_tries < 2:
        item.stage_attempts = dict(item.stage_attempts or {}) | {"on_demand_extractions": extract_tries + 1}
        db.commit()
        if item.link_url and github_repo_from_url(item.link_url):
            item.extraction_state = "skipped"
        else:
            await extract_item(db, http, item)
        db.commit()
    if item.image_state == "pending":
        await cache_item_image(db, image_cache(db, http), item)
    index_items(db, [item.id])
    db.commit()
    return {"item": {**item_dict(db, item, detail=True), "why": why(db, item)}}


@app.get("/api/v1/items/{item_id}/comments")
@commits
async def comments(item_id: int, db: DB, refresh: bool = False, cached: bool = False) -> dict:
    """The discussion. `cached=true` answers from the local copy only (instant); otherwise a stale
    copy is refreshed. Rate limits come back as `retry_in` seconds instead of an error."""
    item = db.get(Item, item_id)
    if item is None:
        raise HTTPException(404, "item not found")
    thread = await get_thread(db, api_http(), get_config(), item, refresh=refresh, cached_only=cached)
    db.commit()
    if thread.get("post_updated"):
        index_items(db, [item.id])
        db.commit()
        thread["item"] = {**item_dict(db, item, detail=True), "why": why(db, item)}
    return thread


UPDATE_SCOPES = {"reddit": "reddit_post", "hn": "hackernews_post", "videos": "youtube_video"}


@app.get("/api/v1/updates")
def updates(db: DB, since: str, scope: str = "today") -> dict:
    """How many posts arrived since `since` (ISO time), so an open page can offer "N new posts"."""
    from radar.db.models import ItemScore
    cutoff = parse_datetime(since)
    if cutoff is None:
        raise HTTPException(422, "since must be an ISO time")
    stmt = select(func.count(Item.id)).where(Item.first_seen_at > cutoff, Item.is_hidden.is_(False),
                                             Item.duplicate_of.is_(None),
                                             func.json_extract(Item.meta, "$.via_search").is_(None))
    if scope in UPDATE_SCOPES:
        stmt = stmt.where(Item.content_type == UPDATE_SCOPES[scope])
    else:
        stmt = stmt.join(ItemScore, ItemScore.item_id == Item.id).where(ItemScore.is_candidate.is_(True))
    return {"new": db.scalar(stmt) or 0, "now": iso(utcnow())}


@app.get("/api/v1/communities")
def communities(db: DB) -> dict:
    """Configured and observed subreddits with counts and collection health."""
    cfg = get_config().reddit
    return {"platform": "reddit", "oauth": bool(cfg.client_id), "communities": list_subreddits(db)}


@app.get("/api/v1/reddit/account")
def reddit_account_status(db: DB) -> dict:
    return reddit_account.status(db)


@app.post("/api/v1/reddit/account")
@commits
async def reddit_account_connect(db: DB, payload: dict[str, Any] = BODY_REQUIRED) -> dict:
    """Connect with the private feed URL from reddit.com/prefs/feeds and run a first sync."""
    try:
        sources = reddit_account.connect(db, str(payload.get("feed_url", "")))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    db.commit()
    return await _sync_account(db, sources)


@app.post("/api/v1/reddit/account/sync")
@commits
async def reddit_account_sync(db: DB) -> dict:
    sources = reddit_account.account_sources(db)
    if not sources:
        raise HTTPException(404, "no Reddit account connected")
    return await _sync_account(db, sources)


async def _sync_account(db: Session, sources: list[Source]) -> dict:
    results = {}
    for source in sources:
        stats = await fetch_source(db, source, api_http(), get_config())
        results[source.config["account"]] = {"seen": stats.seen, "new": stats.new,
                                              "error": redact_feed_tokens(source.last_error)}
        db.commit()
    db.commit()
    request_rescore(30, delay=0)  # seconds of work: not on the event loop
    return {**reddit_account.status(db), "sync": results}


@app.delete("/api/v1/reddit/account")
@commits
def reddit_account_disconnect(db: DB) -> dict:
    """Delete the account sources (and with them the stored feed token). Learned signals stay."""
    return {"ok": True, "removed": reddit_account.disconnect(db)}


@app.get("/api/v1/community")
def community(db: DB, platform: str = Query("reddit", pattern="^(reddit|hackernews)$"),
              community: str | None = None, sort: str = Query("hot", pattern="^(" + "|".join(SORTS) + ")$"),
              period: str = "3d", limit: int = Query(50, le=200), offset: int = Query(0, ge=0)) -> dict:
    """Reddit-style browsing: one subreddit (or a comma list, or all) sorted hot/top/new/discussed/best."""
    items = community_feed(db, platform=platform, community=community, sort=sort,
                           hours=_period_hours(period), limit=limit, offset=offset)
    return {"platform": platform, "community": community, "sort": sort, "period": period,
            "items": [item_dict(db, x) for x in items], "offset": offset, "limit": limit}


@app.get("/api/v1/stories/{cluster_id}")
def story(cluster_id: int, db: DB) -> dict:
    cluster = db.get(StoryCluster, cluster_id)
    if cluster is None:
        raise HTTPException(404, "story not found")
    return {"story": cluster_dict(db, cluster)}


@app.get("/api/v1/search")
async def search(q: str = Query(min_length=2), db: DB = None, limit: int = Query(30, le=100)) -> dict:
    embedder = make_embedder(get_config())
    query_vec = (await embedder.embed([q], kind="query"))[0]
    from radar.clustering.vector_index import get_index
    index = get_index(db, embedder.model_id)
    ranked = hybrid(db, q, query_vec, index, limit=limit)
    items = {i.id: i for i in db.scalars(select(Item).where(Item.id.in_([r["item_id"] for r in ranked])))}
    return {"query": q, "results": [{**item_dict(db, items[r["item_id"]]), "search": r}
                                      for r in ranked if r["item_id"] in items], "count": len(ranked)}


@app.get("/api/v1/search/live")
@commits
async def search_live(db: DB, q: str = Query(min_length=2), where: str = Query("reddit", pattern="^(reddit|hackernews)$"),
                      limit: int = Query(25, le=50), period: str = Query("year", pattern="^(day|week|month|year|all)$")) -> dict:
    """Search Reddit or Hacker News beyond what Radar has collected; results open in the reader."""
    try:
        items = await live_search(db, api_http(), q, where, limit, period)
    except (HttpError, ValueError) as exc:
        if getattr(exc, "status_code", None) == 429 or not str(exc).strip():
            raise HTTPException(502, f"{'Reddit' if where == 'reddit' else 'Hacker News'} is rate-limiting "
                                     "searches right now — try again in a minute") from exc
        raise HTTPException(502, f"{where} search failed: {redact_feed_tokens(str(exc))}") from exc
    db.commit()
    return {"query": q, "where": where, "items": [item_dict(db, x) for x in items]}


@app.get("/api/v1/feed")
def keep_reading_feed(db: DB, offset: int = Query(0, ge=0), limit: int = Query(20, le=60),
                      exclude: str = "") -> dict:
    """Ranked, endless "keep reading" list (Today's tail)."""
    skip = {int(x) for x in exclude.split(",") if x.strip().isdigit()}
    items = keep_reading(db, offset=offset, limit=limit, exclude=skip)
    return {"items": [item_dict(db, x) for x in items], "offset": offset, "limit": limit}


@app.get("/api/v1/items/{item_id}/brief")
def get_brief(item_id: int, db: DB) -> dict:
    item = db.get(Item, item_id)
    if item is None:
        raise HTTPException(404, "item not found")
    return {"item_id": item_id, "brief": cached_brief(db, item)}


@app.post("/api/v1/items/{item_id}/brief")
@commits
async def post_brief(item_id: int, db: DB, refresh: bool = False) -> dict:
    """TL;DR of the post/article and of its discussion, from the local model (cached)."""
    item = db.get(Item, item_id)
    if item is None:
        raise HTTPException(404, "item not found")
    try:
        brief = await make_brief(db, get_config(), item, refresh=refresh)
    except BriefUnavailable as exc:
        db.commit()
        raise HTTPException(503, str(exc)) from exc
    return {"item_id": item_id, "brief": brief}


@app.get("/api/v1/digest")
@commits
def digest(db: DB, period: str = "24h", mode: str = "mixed", limit: int = Query(40, le=100),
           requested_by: str = "api") -> dict:
    hours = _period_hours(period)
    days = max(2, hours // 24 + 1)
    # Fresh arrivals get a score now; the full re-rank (seconds) runs behind the response.
    score_unscored(db, days=days)
    last = last_rescored(db)
    if last is None or last < utcnow() - RESCORE_MAX_AGE:
        request_rescore(days, delay=0)
    entries = build_feed(db, limit=limit, period_hours=hours, mode=mode)
    digest_id = f"dg_{utcnow():%Y%m%d_%H%M}_{secrets.token_hex(2)}"
    sections: dict[str, list[int]] = {}
    session = DigestSession(id=digest_id, period=period, mode=mode, requested_by=requested_by)
    db.add(session)
    db.flush()
    for position, entry in enumerate(entries, 1):
        sections.setdefault(entry.section, []).append(entry.item.id)
        db.add(DigestItem(digest_id=digest_id, position=position, item_id=entry.item.id,
                          section=entry.section, score=entry.score, reason=entry.reason))
    session.sections = [{"key": key, "item_ids": ids} for key, ids in sections.items()]
    return {"digest_id": digest_id, "period": period, "mode": mode, "generated_at": iso(session.created_at),
            "sections": [{"key": key, "items": [{**item_dict(db, e.item), "reason": e.reason}
                                                   for e in entries if e.section == key]}
                         for key in sections], "count": len(entries)}


@app.get("/api/v1/projects")
def projects(db: DB, sort: str = "momentum", language: str | None = None,
             limit: int = Query(50, le=200)) -> dict:
    stmt = select(Repository)
    if language:
        stmt = stmt.where(Repository.language == language)
    field = Repository.first_seen_at if sort == "newest" else Repository.stars if sort == "stars" else Repository.momentum
    repos = db.scalars(stmt.order_by(field.desc()).limit(limit)).all()
    return {"projects": [item_dict(db, db.get(Item, r.item_id)) for r in repos if r.item_id], "count": len(repos)}


@app.get("/api/v1/projects/{repo_id}")
def project(repo_id: int, db: DB) -> dict:
    repo = db.get(Repository, repo_id)
    if repo is None or not repo.item_id:
        raise HTTPException(404, "project not found")
    return {"project": item_dict(db, db.get(Item, repo.item_id), detail=True)}


@app.get("/api/v1/items/{item_id}/related")
def related(item_id: int, db: DB, limit: int = Query(10, le=50)) -> dict:
    item = db.get(Item, item_id)
    if item is None:
        raise HTTPException(404, "item not found")
    return {"item_id": item_id, "related": [{**item_dict(db, other), "similarity": round(sim, 4)}
                                              for other, sim in related_items(db, item, limit)]}


@app.post("/api/v1/items/{item_id}/feedback")
@commits
def feedback(item_id: int, db: DB, payload: dict[str, Any] = BODY_REQUIRED) -> dict:
    item = db.get(Item, item_id)
    if item is None:
        raise HTTPException(404, "item not found")
    action = str(payload.get("action", ""))
    context = dict(payload.get("context") or {})
    try:
        if action == "undo":
            event = undo_last(db, item)
            if event is None:
                raise ValueError("nothing to undo")
        else:
            if context.pop("refine", False):  # e.g. a reason chosen right after "less like this"
                undo_last(db, item)
            event = record(db, item, action, context)
        db.flush()
        fold_events(db)
        score_item(db, item)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if action in ("like", "dislike", "undo", "save", "hide", "skip"):
        db.commit()
        request_rescore()  # the taste profile changed: every other item's rank follows, in the background
    return {"ok": True, "event_id": event.id, "item_id": item_id, "action": action,
            "user": item_dict(db, item)["user"]}


@app.get("/api/v1/tune")
def tune(db: DB, limit: int = Query(12, ge=1, le=40)) -> dict:
    """A diverse sample of recent unrated posts to rate (the "Tune your feed" page)."""
    return {"items": [item_dict(db, x) for x in tune_candidates(db, limit)]}


@app.get("/api/v1/profile")
def taste_profile(db: DB) -> dict:
    """What Radar has learned from feedback, in plain terms."""
    return profile(db)


@app.post("/api/v1/items/{item_id}/save")
@commits
def save(item_id: int, db: DB, payload: dict[str, Any] = BODY_OPTIONAL) -> dict:
    item = db.get(Item, item_id)
    if item is None:
        raise HTTPException(404, "item not found")
    row = db.scalar(select(SavedItem).where(SavedItem.item_id == item_id))
    if row is None:
        row = SavedItem(item_id=item_id, note=payload.get("note"))
        db.add(row)
        record(db, item, "save")
    return {"ok": True, "saved": True, "item_id": item_id}


@app.delete("/api/v1/items/{item_id}/save")
@commits
def unsave(item_id: int, db: DB) -> dict:
    row = db.scalar(select(SavedItem).where(SavedItem.item_id == item_id))
    if row:
        db.delete(row)
    item = db.get(Item, item_id)
    if item:
        record(db, item, "unsave")
    return {"ok": True, "saved": False, "item_id": item_id}


@app.get("/api/v1/interests")
def interests(db: DB) -> dict:
    return {"interests": explain_interests(db)}


@app.get("/api/v1/sources")
def sources(db: DB) -> dict:
    rows = db.scalars(select(Source).order_by(Source.status, Source.name)).all()
    return {"sources": [{"id": s.id, "kind": s.kind, "name": s.name, "identifier": mask_feed_url(s.identifier),
                         "url": s.url, "status": s.status, "origin": s.origin, "quality": s.quality,
                         "access": (s.state or {}).get("last_access"),
                         "item_count": s.item_count, "duplicate_rate": s.duplicate_rate,
                         "last_success_at": iso(s.last_success_at), "next_fetch_at": iso(s.next_fetch_at),
                         "last_error": redact_feed_tokens(s.last_error)} for s in rows]}


@app.get("/api/v1/catalog")
def source_catalog(db: DB) -> dict:
    """Curated subreddits, YouTube channels and feeds by topic, with follow state."""
    return {"groups": catalog(db)}


@app.post("/api/v1/sources")
@commits
async def add_source(db: DB, payload: dict[str, Any] = BODY_REQUIRED) -> dict:
    kind = str(payload.get("kind", ""))
    identifier = str(payload.get("identifier", "")).strip()
    name, url = payload.get("name"), payload.get("url")
    if kind == "youtube" and not re.fullmatch(r"UC[A-Za-z0-9_-]{22}|PL[A-Za-z0-9_-]+", identifier):
        # @handle, channel URL or /c/name -> UC… id (the public feed needs the id)
        resolved = await resolve_channel_id(api_http(), identifier)
        if not resolved:
            raise HTTPException(422, "could not find that YouTube channel (try its @handle or channel URL)")
        url = url or (identifier if identifier.startswith("http") else f"https://www.youtube.com/{identifier.lstrip('/')}")
        name = name or f"YouTube · {identifier.rstrip('/').rsplit('/', 1)[-1].lstrip('@')}"
        identifier = resolved
    if kind == "reddit":
        identifier = normalize_subreddit(identifier) or ""
        if not identifier:
            raise HTTPException(422, "expected a subreddit name (r/LocalLLaMA) or a reddit.com feed URL")
        feed = identifier.startswith("http")
        name = name or ("Reddit · Front page" if feed else f"Reddit · {identifier}")
        url = url or ("https://www.reddit.com" if feed else f"https://www.reddit.com/r/{identifier}")
    if kind not in COLLECTORS or not identifier:
        raise HTTPException(422, "valid collector kind and identifier are required")
    existing = db.scalar(select(Source).where(Source.kind == kind,
                                              func.lower(Source.identifier) == identifier.lower()))
    if existing:
        raise HTTPException(409, "source already exists")
    source = Source(kind=kind, identifier=identifier, name=str(name or identifier)[:200],
                    url=url, config=payload.get("config") or {}, status="active", origin="manual")
    db.add(source)
    db.flush()
    return {"ok": True, "source_id": source.id}


@app.patch("/api/v1/sources/{source_id}")
@commits
def update_source(source_id: int, db: DB, payload: dict[str, Any] = BODY_REQUIRED) -> dict:
    source = db.get(Source, source_id)
    if source is None:
        raise HTTPException(404, "source not found")
    if "status" in payload:
        status = str(payload["status"])
        if status not in ("active", "muted", "disabled", "rejected"):
            raise HTTPException(422, "invalid source status")
        source.status = status
    if "weight" in payload:
        source.weight = max(0.0, min(3.0, float(payload["weight"])))
    if "interval_minutes" in payload:
        source.interval_minutes = max(5, int(payload["interval_minutes"]))
    return {"ok": True, "source_id": source.id, "status": source.status}


@app.get("/api/v1/discovery")
def discovery(db: DB) -> dict:
    rows = db.scalars(select(DiscoveryCandidate).order_by(DiscoveryCandidate.updated_at.desc())).all()
    return {"candidates": [{"id": x.id, "kind": x.kind, "name": x.name, "identifier": x.identifier,
                            "status": x.status, "method": x.method, "confidence": x.confidence,
                            "evidence_count": x.evidence_count, "reason": x.decision_reason} for x in rows]}


@app.post("/api/v1/discovery/{candidate_id}/{action}")
@commits
def discovery_action(candidate_id: int, action: str, db: DB) -> dict:
    candidate = db.get(DiscoveryCandidate, candidate_id)
    if candidate is None:
        raise HTTPException(404, "candidate not found")
    if action == "trial":
        source = start_trial(db, candidate)
    elif action in ("promote", "reject"):
        decide_trial(db, candidate, action == "promote", "manual decision from API")
        source = db.get(Source, candidate.source_id) if candidate.source_id else None
    else:
        raise HTTPException(422, "action must be trial, promote, or reject")
    return {"ok": True, "candidate_id": candidate.id, "status": candidate.status,
            "source_id": source.id if source else None}


@app.get("/api/v1/settings")
def settings(db: DB) -> dict:
    return {"settings": all_values(db)}


@app.patch("/api/v1/settings/{key}")
@commits
def update_setting(key: str, db: DB, payload: dict[str, Any] = BODY_REQUIRED) -> dict:
    try:
        value = set_value(db, key, payload.get("value"))
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    if key.startswith(("ranking.", "feed.")):
        db.commit()
        request_rescore()
    return {"key": key, "value": value}


REMOTE_IMAGE_HOSTS = ("i.redd.it", "preview.redd.it", "external-preview.redd.it", "i.imgur.com", "i.ytimg.com")


@app.get("/media/remote")
async def remote_media(u: str, db: DB):
    """Images referenced inside posts and comments, fetched once and served from the local cache
    (no hotlinking from the browser). Only well-known image hosts are allowed."""
    url = full_size_image(u) or u
    parts = urlsplit(url)
    if parts.scheme != "https" or (parts.hostname or "") not in REMOTE_IMAGE_HOSTS:
        raise HTTPException(404, "host not allowed")
    row = db.scalar(select(Image).where(Image.url_hash == url_hash(url)))
    if row is None or row.status != "ok" or not (paths.image_cache_dir() / (row.cache_path or "")).is_file():
        if row is not None and row.status in ("failed", "rejected") and row.fail_count >= 2:
            raise HTTPException(404, "image unavailable")
        row = row or Image(original_url=url, url_hash=url_hash(url), kind="inline")
        db.add(row)
        try:
            image, _ = await image_cache(db, api_http()).download(url, "inline")
        except ImageRejected as exc:
            row.fail_count = (row.fail_count or 0) + 1
            row.status = "rejected"
            db.commit()
            raise HTTPException(404, "image unavailable") from exc
        if image is None:
            raise HTTPException(404, "image unavailable")
        row.content_hash, row.mime, row.width, row.height = image.content_hash, image.mime, image.width, image.height
        row.bytes, row.cache_path, row.status, row.fetched_at = image.bytes, image.rel_path, "ok", utcnow()
        db.commit()
    return FileResponse(paths.image_cache_dir() / row.cache_path, media_type="image/webp",
                        headers={"Cache-Control": "public, max-age=604800"})


_item_image_locks: dict[int, asyncio.Lock] = {}


@app.get("/media/item/{item_id}")
async def item_media(item_id: int, db: DB):
    """An item's picture, cached on first request. Feeds point here for items whose image the worker
    has not cached yet, so pictures appear while scrolling instead of after the next worker cycle."""
    lock = _item_image_locks.setdefault(item_id, asyncio.Lock())
    async with lock:  # the same picture requested twice at once is downloaded once
        item = db.get(Item, item_id)
        if item is None:
            raise HTTPException(404, "item not found")
        if item.image_id is None and item.image_state == "pending":
            await cache_item_image(db, image_cache(db, api_http()), item)
            db.commit()
        image = db.get(Image, item.image_id) if item.image_id else None
    _item_image_locks.pop(item_id, None)
    if image is None or image.status != "ok" or not image.cache_path:
        raise HTTPException(404, "no image")
    path = paths.image_cache_dir() / image.cache_path
    if not path.is_file():
        raise HTTPException(404, "cached image missing")
    return FileResponse(path, media_type="image/webp", headers={"Cache-Control": "public, max-age=604800"})


@app.get("/media/{image_id}")
def media(image_id: int, db: DB):
    image = db.get(Image, image_id)
    if image is None or image.status != "ok" or not image.cache_path:
        raise HTTPException(404, "image not found")
    path = paths.image_cache_dir() / image.cache_path
    if not path.is_file():
        raise HTTPException(404, "cached image missing")
    return FileResponse(path, media_type="image/webp", headers={"Cache-Control": "public, max-age=604800"})


WEB = paths.web_dist_dir()
if WEB.exists():
    app.mount("/assets", StaticFiles(directory=WEB / "assets"), name="assets")


@app.get("/{path:path}", include_in_schema=False)
def spa(path: str = ""):
    index = WEB / "index.html"
    if index.exists():
        return FileResponse(index)
    return HTMLResponse("<h1>Nyra Radar</h1><p>Web assets are not installed.</p>", status_code=503)
