"""FastAPI application: browser API and stable Nyra tool surface."""
from __future__ import annotations

import re
import secrets
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from radar import paths
from radar.api.serializers import cluster_dict, item_dict
from radar.clustering.embeddings import make_embedder
from radar.clustering.service import related_items
from radar.collectors import COLLECTORS
from radar.config import get_config
from radar.db import get_engine, migrate, session_factory
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
from radar.net import Http
from radar.ranking import build_feed, rescore, score_item
from radar.services import reddit_account
from radar.services.collect import fetch_source
from radar.services.community import (
    SORTS,
    community_feed,
    list_subreddits,
    mask_feed_url,
    normalize_subreddit,
)
from radar.services.feedback import explain_interests, fold_events, record
from radar.services.search import hybrid
from radar.services.settings import all_values, set_value
from radar.util import iso, redact_feed_tokens, utcnow

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


_http: Http | None = None


def api_http() -> Http:
    """One paced HTTP client for on-demand fetches (discussion threads) from the API process."""
    global _http
    if _http is None:
        _http = Http(max_concurrency=4)
    return _http


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _http
    paths.ensure_dirs()
    migrate(get_engine())
    yield
    if _http is not None:
        await _http.close()
        _http = None


app = FastAPI(title="Nyra Radar", version="1.0.0", lifespan=lifespan,
              description="Local-first personal technology intelligence API")


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
    return {"item": item_dict(db, item, detail=True)}


@app.get("/api/v1/items/{item_id}/comments")
async def comments(item_id: int, db: DB, refresh: bool = False) -> dict:
    item = db.get(Item, item_id)
    if item is None:
        raise HTTPException(404, "item not found")
    thread = await get_thread(db, api_http(), get_config(), item, refresh=refresh)
    return {"item_id": item_id, **thread}


@app.get("/api/v1/communities")
def communities(db: DB) -> dict:
    """Configured and observed subreddits with counts and collection health."""
    cfg = get_config().reddit
    return {"platform": "reddit", "oauth": bool(cfg.client_id), "communities": list_subreddits(db)}


@app.get("/api/v1/reddit/account")
def reddit_account_status(db: DB) -> dict:
    return reddit_account.status(db)


@app.post("/api/v1/reddit/account")
async def reddit_account_connect(db: DB, payload: dict[str, Any] = BODY_REQUIRED) -> dict:
    """Connect with the private feed URL from reddit.com/prefs/feeds and run a first sync."""
    try:
        sources = reddit_account.connect(db, str(payload.get("feed_url", "")))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    db.commit()
    return await _sync_account(db, sources)


@app.post("/api/v1/reddit/account/sync")
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
    rescore(db, days=30)
    return {**reddit_account.status(db), "sync": results}


@app.delete("/api/v1/reddit/account")
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


@app.get("/api/v1/digest")
def digest(db: DB, period: str = "24h", mode: str = "mixed", limit: int = Query(40, le=100),
           requested_by: str = "api") -> dict:
    hours = _period_hours(period)
    rescore(db, days=max(2, hours // 24 + 1))
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
def feedback(item_id: int, db: DB, payload: dict[str, Any] = BODY_REQUIRED) -> dict:
    item = db.get(Item, item_id)
    if item is None:
        raise HTTPException(404, "item not found")
    action = str(payload.get("action", ""))
    try:
        event = record(db, item, action, payload.get("context"))
        fold_events(db)
        score_item(db, item)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"ok": True, "event_id": event.id, "item_id": item_id, "action": action,
            "user": item_dict(db, item)["user"]}


@app.post("/api/v1/items/{item_id}/save")
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


@app.post("/api/v1/sources")
def add_source(db: DB, payload: dict[str, Any] = BODY_REQUIRED) -> dict:
    kind = str(payload.get("kind", ""))
    identifier = str(payload.get("identifier", "")).strip()
    name, url = payload.get("name"), payload.get("url")
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
def update_setting(key: str, db: DB, payload: dict[str, Any] = BODY_REQUIRED) -> dict:
    try:
        value = set_value(db, key, payload.get("value"))
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"key": key, "value": value}


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
