"""Idempotent ingestion: RawItems -> Items (+ sightings, community stats, repo links).

Re-encountering the same URL/post/repository updates the existing row (engagement,
text) and records a sighting; it never creates a duplicate row. Changed text resets
the downstream stages that depend on it; unchanged text leaves them alone.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from radar.db.models import CommunityPost, Item, ItemLink, ItemSighting, Source
from radar.extraction.canonical_url import (
    canonicalize,
    classify_link,
    domain_of,
    github_repo_from_url,
    url_hash,
)
from radar.normalization.models import RawItem
from radar.normalization.normalize import Normalized, normalize
from radar.services import repos as repo_svc
from radar.services.search import index_items
from radar.util import iso, utcnow

log = logging.getLogger(__name__)

MAX_NEW_REPOS_PER_ITEM = 4
MAX_LINKS_PER_ITEM = 30
ARTICLE_TYPES = {"article", "rss_entry"}


@dataclass
class IngestStats:
    seen: int = 0
    new: int = 0
    updated: int = 0
    cross_source_duplicates: int = 0
    repos_linked: int = 0
    new_item_ids: list[int] = field(default_factory=list)
    touched_item_ids: list[int] = field(default_factory=list)


def _initial_states(item: Item, norm: Normalized) -> None:
    ctype = item.content_type
    # Only items that point at an external article get full-text extraction.
    target = item.link_url if ctype in ("reddit_post", "hackernews_post", "telegram_post") else item.canonical_url
    needs_extraction = ctype in ARTICLE_TYPES or (target and ctype in ("reddit_post", "hackernews_post",
                                                                        "telegram_post"))
    if norm.raw.meta.get("unresolved_redirect") or (target and github_repo_from_url(target)):
        needs_extraction = False
    item.extraction_state = "pending" if needs_extraction else "skipped"
    item.image_state = "pending"
    item.embedding_state = "pending"
    item.cluster_state = "pending"
    item.processing_state = "normalized"


def _update_existing(item: Item, norm: Normalized, source: Source | None) -> bool:
    f = norm.fields
    changed = False
    for attr in ("external_score", "external_comment_count"):
        if f.get(attr) is not None and getattr(item, attr) != f[attr]:
            setattr(item, attr, f[attr])
            changed = True
    if f["content_hash"] != item.content_hash and f["title"]:
        new_len = len(f.get("clean_text") or "")
        old_len = len(item.clean_text or "")
        # Keep richer text we already have (e.g. extracted article) unless the source text grew.
        if f["title"] != item.title or new_len >= old_len or item.extraction_state != "done":
            item.title = f["title"]
            if new_len >= old_len:
                item.clean_text = f.get("clean_text")
                item.raw_text = f.get("raw_text")
                item.excerpt = f.get("excerpt") or item.excerpt
            item.content_hash = f["content_hash"]
            item.title_hash = f["title_hash"]
            item.simhash = f["simhash"] or item.simhash
            item.embedding_state = "stale" if item.embedding_state == "done" else item.embedding_state
            changed = True
    if not item.published_at and f.get("published_at"):
        item.published_at = f["published_at"]
        changed = True
    known = {tuple(c) for c in (item.meta or {}).get("image_candidates", [])}
    if f.get("meta"):
        merged = dict(item.meta or {})
        for k, v in f["meta"].items():
            if k not in merged or k in ("image_candidates", "reddit_sort", "reddit_rank", "reddit_listing_size",
                                        "reddit_seen_at", "hn_list", "views", "ossinsight"):
                merged[k] = v
        if merged != item.meta:
            item.meta = merged
    if (item.image_state in ("none", "failed") and f["meta"].get("image_candidates")
            and any(tuple(c) not in known for c in f["meta"]["image_candidates"])):
        item.image_state = "pending"
    return changed


def _upsert_community(session: Session, item: Item, raw: RawItem) -> None:
    info = raw.community
    if info is None:
        return
    post = session.get(CommunityPost, item.id)
    now = utcnow()
    if post is None:
        post = CommunityPost(item_id=item.id, platform=info.platform, community=info.community,
                             post_id=info.post_id or item.source_item_id or str(item.id),
                             discussion_url=info.discussion_url, score_history=[])
        session.add(post)
    # Feeds without vote counts (Reddit Atom) must not erase counts learned from the API.
    for attr in ("score", "num_comments", "upvote_ratio", "flair"):
        value = getattr(info, attr)
        if value is not None:
            setattr(post, attr, value)
    if info.community:
        post.community = info.community
    post.is_self = info.is_self
    post.post_kind = info.post_kind
    post.linked_url = item.link_url
    if info.score is None:
        return
    history = list(post.score_history or [])
    last = history[-1] if history else None
    if not last or (last[1], last[2]) != (info.score, info.num_comments):
        if last and (now - _parse_iso(last[0])).total_seconds() < 900:
            history[-1] = [iso(now), info.score, info.num_comments]
        else:
            history.append([iso(now), info.score, info.num_comments])
        post.score_history = history[-48:]


def _parse_iso(value: str):
    from radar.util import parse_datetime

    return parse_datetime(value) or utcnow()


def _record_sighting(session: Session, item: Item, source: Source | None, raw_url: str,
                     source_item_id: str | None) -> None:
    if source is None:
        return
    now = utcnow()
    stmt = sqlite_insert(ItemSighting).values(
        item_id=item.id, source_id=source.id, source_item_id=source_item_id, raw_url=raw_url[:2000],
        first_seen_at=now, last_seen_at=now, seen_count=1)
    stmt = stmt.on_conflict_do_update(
        index_elements=["item_id", "source_id", "raw_url"],
        set_={"last_seen_at": now, "seen_count": ItemSighting.seen_count + 1})
    session.execute(stmt)


def _link_repositories(session: Session, item: Item, raw: RawItem, stats: IngestStats) -> None:
    via = f"{item.content_type}"
    if item.content_type == "github_repository" and raw.repo:
        repo = repo_svc.upsert_repository(session, raw.repo.full_name, raw.repo, via=raw.meta.get("via", via))
        session.flush()
        repo.item_id = item.id
        item.repository_id = repo.id
        if raw.repo.stars is not None:
            item.external_score = raw.repo.stars
        return
    if item.content_type == "github_release" and raw.repo:
        repo = repo_svc.upsert_repository(session, raw.repo.full_name, raw.repo, via="release")
        session.flush()
        item.repository_id = repo.id
        repo_svc.ensure_repo_item(session, repo, via="release")
        repo_svc.add_mention(session, repo, item, relation="release_of")
        stats.repos_linked += 1
        return
    names: dict[str, None] = {}
    for url in ([raw.link_url] if raw.link_url else []) + raw.links:
        full = github_repo_from_url(url)
        if full:
            names.setdefault(full, None)
    if not names:
        return
    allow_new = len(names) <= 8  # awesome-lists and link dumps may only link to already-known repos
    created = 0
    for full in names:
        repo = repo_svc.get_repository(session, full)
        if repo is None:
            if not allow_new or created >= MAX_NEW_REPOS_PER_ITEM:
                continue
            repo = repo_svc.upsert_repository(session, full, None, via=f"mention:{item.content_type}")
            session.flush()
            created += 1
        repo_svc.ensure_repo_item(session, repo, via=f"mention:{item.content_type}")
        relation = "links_to" if raw.link_url and github_repo_from_url(raw.link_url) == full else "mentions"
        repo_svc.add_mention(session, repo, item, relation=relation)
        stats.repos_linked += 1


def _store_links(session: Session, item: Item, raw: RawItem) -> None:
    urls: dict[str, str] = {}
    for url in ([raw.link_url] if raw.link_url else []) + raw.links:
        if not url or not url.startswith("http"):
            continue
        canon = canonicalize(url)
        h = url_hash(canon)
        if canon != item.canonical_url:
            urls.setdefault(h, canon)
        if len(urls) >= MAX_LINKS_PER_ITEM:
            break
    for h, canon in urls.items():
        session.execute(sqlite_insert(ItemLink).values(
            item_id=item.id, url=canon[:1500], url_hash=h, domain=domain_of(canon), kind=classify_link(canon)
        ).on_conflict_do_nothing(index_elements=["item_id", "url_hash"]))


def ingest_items(session: Session, source: Source | None, raws: list[RawItem]) -> IngestStats:
    stats = IngestStats(seen=len(raws))
    norms: dict[str, Normalized] = {}
    for raw in raws:
        try:
            n = normalize(raw, source.id if source else None)
        except Exception as exc:  # noqa: BLE001 - malformed plugin/collector input is isolated per item
            log.warning("normalize failed for %s: %s", raw.url[:120], exc)
            continue
        if n.fields["canonical_url"] and n.fields["title"]:
            norms.setdefault(n.url_hash, n)
    if not norms:
        return stats
    existing = {it.url_hash: it for it in session.scalars(select(Item).where(Item.url_hash.in_(list(norms))))}
    pending: list[tuple[Item, Normalized, bool]] = []
    for h, norm in norms.items():
        item = existing.get(h)
        if item is not None:
            changed = _update_existing(item, norm, source)
            if source is not None and item.source_id not in (None, source.id):
                stats.cross_source_duplicates += 1
            if changed:
                stats.updated += 1
            pending.append((item, norm, False))
        else:
            item = Item(**norm.fields)
            _initial_states(item, norm)
            session.add(item)
            pending.append((item, norm, True))
    session.flush()
    for item, norm, is_new in pending:
        raw = norm.raw
        _record_sighting(session, item, source, raw.url, raw.source_item_id)
        _upsert_community(session, item, raw)
        if is_new:
            stats.new += 1
            stats.new_item_ids.append(item.id)
            _store_links(session, item, raw)
        _link_repositories(session, item, raw, stats)
        stats.touched_item_ids.append(item.id)
    if source is not None:
        source.item_count = (source.item_count or 0) + stats.new
        source.duplicate_count = (source.duplicate_count or 0) + stats.cross_source_duplicates
    session.flush()
    index_items(session, [i.id for i, _, _ in pending][:2000])
    return stats
