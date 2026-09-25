"""Embedding persistence, layered duplicate detection, and story clustering."""
from __future__ import annotations

from datetime import timedelta

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from radar.clustering.embeddings import Embedder, to_blob
from radar.clustering.fingerprint import hamming
from radar.db.models import Embedding, Item, StoryCluster
from radar.normalization.normalize import embed_text
from radar.services.settings import get as get_setting
from radar.util import sha1_hex, utcnow


async def embed_pending(session: Session, embedder: Embedder, limit: int = 64) -> int:
    items = session.scalars(
        select(Item).where(Item.embedding_state.in_(("pending", "stale")))
        .order_by(Item.first_seen_at.desc()).limit(limit)
    ).all()
    if not items:
        return 0
    texts = [embed_text(i.title, i.clean_text or i.excerpt) for i in items]
    vectors = await embedder.embed(texts, kind="document")
    now = utcnow()
    for item, text, vector in zip(items, texts, vectors, strict=True):
        row = session.get(Embedding, item.id)
        if row is None:
            row = Embedding(item_id=item.id, model=embedder.model_id, dim=len(vector), vector=b"",
                            input_hash="")
            session.add(row)
        row.model = embedder.model_id
        row.dim = len(vector)
        row.vector = to_blob(vector)
        row.input_hash = sha1_hex(text)
        row.created_at = now
        item.embedding_state = "done"
    session.flush()
    return len(items)


def _vector(row: Embedding | None) -> np.ndarray | None:
    return np.frombuffer(row.vector, dtype=np.float32) if row and row.vector else None


def _similarity(a: Embedding | None, b: Embedding | None) -> float:
    va, vb = _vector(a), _vector(b)
    if va is None or vb is None or va.shape != vb.shape:
        return 0.0
    return float(np.dot(va, vb))


def cluster_pending(session: Session, limit: int = 300) -> int:
    """Cluster recent stories. Exact target/title matches work without an embedding model."""
    window = int(get_setting(session, "clustering.window_hours", 96))
    threshold = float(get_setting(session, "clustering.story_threshold", 0.84))
    cutoff = utcnow() - timedelta(hours=window)
    pending = session.scalars(
        select(Item).where(Item.cluster_state == "pending", Item.first_seen_at >= cutoff)
        .order_by(Item.first_seen_at).limit(limit)
    ).all()
    if not pending:
        return 0
    recent = session.scalars(
        select(Item).where(Item.first_seen_at >= cutoff, Item.cluster_state == "done")
        .order_by(Item.first_seen_at.desc()).limit(2500)
    ).all()
    embeddings = {e.item_id: e for e in session.scalars(
        select(Embedding).where(Embedding.item_id.in_([x.id for x in pending + recent]))
    )}
    clusters = {c.id: c for c in session.scalars(select(StoryCluster))}

    for item in pending:
        best: Item | None = None
        best_sim = 0.0
        for other in recent:
            if other.id == item.id:
                continue
            exact_target = bool(item.link_hash and item.link_hash in (other.link_hash, other.url_hash)) or bool(
                other.link_hash and other.link_hash == item.url_hash
            )
            exact_title = bool(item.title_hash and item.title_hash == other.title_hash)
            if item.source_id and item.source_id == other.source_id and not (exact_target or exact_title):
                continue  # a story is coverage across sources; one channel's videos share boilerplate text
            simhash_close = bool(item.simhash is not None and other.simhash is not None and
                                 hamming(item.simhash, other.simhash) <= 5)
            sim = _similarity(embeddings.get(item.id), embeddings.get(other.id))
            if exact_target or exact_title:
                sim = max(sim, 0.99)
            elif simhash_close:
                sim = max(sim, 0.94)
            if sim > best_sim and (sim >= threshold or exact_target or exact_title):
                best, best_sim = other, sim
        if best and best.cluster_id:
            cluster = clusters[best.cluster_id]
        elif best:
            cluster = StoryCluster(title=best.title, representative_item_id=best.id,
                                   first_seen_at=min(best.first_seen_at, item.first_seen_at))
            session.add(cluster)
            session.flush()
            clusters[cluster.id] = cluster
            best.cluster_id = cluster.id
        else:
            cluster = StoryCluster(title=item.title, representative_item_id=item.id,
                                   first_seen_at=item.first_seen_at)
            session.add(cluster)
            session.flush()
            clusters[cluster.id] = cluster
        item.cluster_id = cluster.id
        item.cluster_state = "done"
        recent.append(item)

    touched = {i.cluster_id for i in pending if i.cluster_id}
    for cid in touched:
        members = session.scalars(select(Item).where(Item.cluster_id == cid)).all()
        cluster = clusters[cid]
        cluster.item_count = len(members)
        cluster.source_count = len({i.source_id for i in members if i.source_id})
        cluster.platforms = sorted({i.content_type for i in members})
        cluster.last_updated_at = max(i.first_seen_at for i in members)
        representative = max(
            members,
            key=lambda x: ((x.external_score or 0) + 3 * (x.external_comment_count or 0),
                           len(x.clean_text or "")),
        )
        cluster.representative_item_id = representative.id
        cluster.title = representative.title
    session.flush()
    return len(pending)


def related_items(session: Session, item: Item, limit: int = 10) -> list[tuple[Item, float]]:
    base = session.get(Embedding, item.id)
    candidates = session.scalars(
        select(Item).where(Item.id != item.id, Item.first_seen_at >= utcnow() - timedelta(days=30))
        .order_by(Item.first_seen_at.desc()).limit(1200)
    ).all()
    scored: list[tuple[Item, float]] = []
    for other in candidates:
        score = _similarity(base, session.get(Embedding, other.id))
        if item.cluster_id and item.cluster_id == other.cluster_id:
            score = max(score, 0.98)
        if item.repository_id and item.repository_id == other.repository_id:
            score = max(score, 0.96)
        if score >= 0.35:
            scored.append((other, score))
    return sorted(scored, key=lambda x: x[1], reverse=True)[:limit]
