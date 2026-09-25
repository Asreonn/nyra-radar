"""Explainable cheap ranking and diversity-aware feed construction."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import timedelta

import numpy as np
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from radar.clustering.vector_index import VectorIndex, get_index
from radar.collectors.reddit import RANKED_SORTS
from radar.db.models import (
    CommunityPost,
    Embedding,
    Interest,
    Item,
    ItemScore,
    ItemTopic,
    Repository,
    SavedItem,
    SourcePreference,
    StoryCluster,
)
from radar.services.settings import get as get_setting
from radar.util import utcnow


def _sat(value: float, scale: float) -> float:
    return 1.0 - math.exp(-max(0.0, value) / scale)


def _freshness(item: Item, half_lives: dict[str, float]) -> float:
    stamp = item.published_at or item.first_seen_at
    hours = max(0.0, (utcnow() - stamp).total_seconds() / 3600)
    half = float(half_lives.get(item.content_type, half_lives.get("default", 30)))
    return 2 ** (-hours / max(1.0, half))


def _interest(session: Session, item_id: int) -> tuple[float, list[str]]:
    rows = session.execute(
        select(ItemTopic.weight, Interest.weight, Interest.muted)
        .join(Interest, Interest.topic_id == ItemTopic.topic_id)
        .where(ItemTopic.item_id == item_id)
    ).all()
    if not rows:
        return 0.5, []
    values = [float(item_weight) * float(weight) for item_weight, weight, muted in rows if not muted]
    muted = ["muted topic"] if any(r[2] for r in rows) else []
    return max(0.0, min(1.0, 0.5 + sum(values) / max(2, len(values)) / 2)), muted


# Default saturation scales (points, comments) where a community has too little local history.
DEFAULT_ENGAGEMENT_SCALE = (120.0, 45.0)
MIN_SCALE_SAMPLES = 8


def community_scales(session: Session, days: int = 7) -> dict[tuple[str, str], tuple[float, float]]:
    """Per-community (platform, name) saturation scales: the 75th percentile of observed votes
    and comments. 150 points is a big post in r/opensource and an ordinary one in r/LocalLLaMA,
    so engagement is judged against the community it was posted in."""
    rows = session.execute(
        select(CommunityPost.platform, CommunityPost.community, CommunityPost.score, CommunityPost.num_comments)
        .join(Item, Item.id == CommunityPost.item_id)
        .where(Item.first_seen_at >= utcnow() - timedelta(days=days), CommunityPost.score > 0)
    ).all()
    samples: dict[tuple[str, str], list[tuple[int, int]]] = {}
    for platform, community, score, comments in rows:
        samples.setdefault((platform, (community or "").lower()), []).append((score, comments))
    scales = {}
    for key, values in samples.items():
        if len(values) < MIN_SCALE_SAMPLES:
            continue
        scores = sorted(v[0] for v in values)
        comments = sorted(v[1] for v in values)
        p75 = int(len(values) * 0.75)
        scales[key] = (max(10.0, float(scores[p75])), max(5.0, float(comments[p75])))
    return scales


MIN_PROFILE_ITEMS = 3


@dataclass
class RankContext:
    """Per-rescore facts shared by every item: community scales and the taste profile."""
    scales: dict[tuple[str, str], tuple[float, float]]
    index: VectorIndex | None = None
    centroid: np.ndarray | None = None
    reference: np.ndarray | None = None  # sorted similarities of all non-example items, for percentiles
    profile_size: int = 0


def taste_profile(session: Session) -> tuple[VectorIndex | None, np.ndarray | None, np.ndarray | None, int]:
    """Centroid of the embeddings of liked and saved items (including Reddit upvotes/saves
    imported from a connected account). `semantic_preference` is an item's similarity
    percentile against it; with fewer than MIN_PROFILE_ITEMS examples it stays neutral."""
    model = session.scalar(select(Embedding.model).group_by(Embedding.model)
                           .order_by(func.count(Embedding.item_id).desc()).limit(1))
    if not model:
        return None, None, None, 0
    liked = session.scalars(
        select(Item.id).outerjoin(SavedItem, SavedItem.item_id == Item.id)
        .where(or_(Item.user_vote == 1, SavedItem.id.is_not(None)))
        .order_by(Item.last_opened_at.desc(), Item.id.desc()).limit(500)
    ).all()
    if len(liked) < MIN_PROFILE_ITEMS:
        return None, None, None, 0
    index = get_index(session, model)
    _, vectors = index.vectors(list(liked))
    if vectors.shape[0] < MIN_PROFILE_ITEMS:
        return None, None, None, 0
    centroid = vectors.mean(axis=0)
    norm = float(np.linalg.norm(centroid))
    if norm == 0:
        return None, None, None, 0
    centroid = centroid / norm
    # Percentiles are taken among everything *except* the examples themselves.
    others = index.matrix[~np.isin(index.ids, np.asarray(liked, dtype=np.int64))]
    if not len(others):
        return None, None, None, 0
    return index, centroid, np.sort(others @ centroid), int(vectors.shape[0])


def rank_context(session: Session) -> RankContext:
    index, centroid, reference, size = taste_profile(session)
    return RankContext(community_scales(session), index, centroid, reference, size)


def _semantic(ctx: RankContext, item: Item) -> float:
    if ctx.index is None or ctx.centroid is None or ctx.reference is None or not len(ctx.reference):
        return 0.5
    vector = ctx.index.vector(item.id)
    if vector is None or vector.shape[-1] != ctx.centroid.shape[-1]:
        return 0.5
    similarity = float(vector @ ctx.centroid)
    return float(np.searchsorted(ctx.reference, similarity, side="right")) / len(ctx.reference)


def _engagement(community: CommunityPost, item: Item,
                scales: dict[tuple[str, str], tuple[float, float]]) -> tuple[float, dict[str, object]]:
    meta = item.meta or {}
    rank = meta.get("reddit_rank")
    if community.platform == "reddit" and not community.score and not community.num_comments and rank is not None:
        # Atom feeds carry no votes: Reddit's own hot/top position is the engagement evidence.
        sort = meta.get("reddit_sort") or "hot"
        if sort not in RANKED_SORTS:
            return 0.25, {"listing": sort, "rank": int(rank) + 1}
        size = max(1, int(meta.get("reddit_listing_size") or 25))
        position = 1.0 - min(1.0, int(rank) / size)
        # Convex: the head of a hot listing is real heat, its tail is merely "recent".
        value = 0.1 + 0.55 * position ** 1.5 + (0.1 if sort == "top" else 0.0)
        return min(1.0, value), {"listing": sort, "rank": int(rank) + 1, "of": size}
    score_scale, comment_scale = scales.get((community.platform, (community.community or "").lower()),
                                            DEFAULT_ENGAGEMENT_SCALE)
    value = 0.65 * _sat(community.score, score_scale) + 0.35 * _sat(community.num_comments, comment_scale)
    return value, {"score": community.score, "comments": community.num_comments,
                   "scale": [round(score_scale), round(comment_scale)]}


def score_item(session: Session, item: Item, ctx: RankContext | None = None) -> ItemScore:
    """Compute deterministic components. Unknown signals remain neutral, not invented."""
    weights = get_setting(session, "ranking.weights", {})
    half_lives = get_setting(session, "feed.freshness_half_life_hours", {"default": 30})
    type_prefs = get_setting(session, "ranking.content_type_preference", {"default": 0.5})
    if ctx is None:
        ctx = rank_context(session)
    community = session.get(CommunityPost, item.id)
    repo = session.get(Repository, item.repository_id) if item.repository_id else None
    pref = session.get(SourcePreference, item.source_id) if item.source_id else None
    cluster = session.get(StoryCluster, item.cluster_id) if item.cluster_id else None

    freshness = _freshness(item, half_lives)
    interest, interest_notes = _interest(session, item.id)
    source_quality = float(item.source.quality if item.source else 0.5)
    source_affinity = min(1.0, max(0.0, float(pref.affinity if pref else 0.5)))
    engagement = 0.0
    momentum = 0.0
    evidence: dict[str, object] = {}
    if community:
        engagement, evidence["community"] = _engagement(community, item, ctx.scales)
    if repo:
        growth = repo.stars_delta_7d
        if growth is None:
            for key in ("ossinsight_7d", "ossinsight_24h"):
                raw = (repo.external_stars_delta or {}).get(key)
                if raw is not None:
                    growth = raw.get("value") if isinstance(raw, dict) else raw
                    break
        momentum = 0.55 * _sat(float(growth or 0), 180) + 0.25 * _sat(repo.mentions_7d, 4)
        if repo.pushed_at:
            momentum += 0.2 * 2 ** (-max(0, (utcnow() - repo.pushed_at).days) / 30)
        evidence["repository"] = {"stars": repo.stars, "growth_7d": growth,
                                  "mentions_7d": repo.mentions_7d}
    semantic = _semantic(ctx, item)
    if ctx.profile_size:
        evidence["taste_profile"] = {"examples": ctx.profile_size, "percentile": round(semantic, 3)}
    coverage = _sat((cluster.source_count if cluster else 1) - 1, 2)
    novelty = max(0.05, 1.0 - coverage * 0.35)
    spam = 0.0
    low_title = (item.title or "").lower()
    if len(item.title or "") < 12 or sum(x in low_title for x in ("sponsored", "giveaway", "crypto airdrop")):
        spam = 0.7
    duplicate = 1.0 if item.duplicate_of else 0.0
    ignored = 1.0 if item.is_hidden else min(1.0, item.impressions / 8)
    components = {
        "personal_interest": interest,
        "semantic_preference": semantic,
        "freshness": freshness,
        "novelty": novelty,
        "source_quality": source_quality,
        "source_affinity": source_affinity,
        "content_type_preference": float(type_prefs.get(item.content_type, type_prefs.get("default", 0.5))),
        "community_signal": engagement,
        "project_momentum": momentum,
        "coverage": coverage,
        "llm_quality": float((item.meta or {}).get("llm_quality", 0.5)),
        "duplicate_penalty": duplicate,
        "spam_penalty": spam,
        "ignored_penalty": ignored,
    }
    positive = sum(float(weights.get(k, 0)) * v for k, v in components.items() if "penalty" not in k)
    negative = sum(float(weights.get(k, 0)) * v for k, v in components.items() if "penalty" in k)
    total_weight = sum(float(v) for k, v in weights.items() if "penalty" not in k) or 1
    final = max(0.0, min(1.0, positive / total_weight - negative / total_weight))
    row = session.get(ItemScore, item.id) or ItemScore(item_id=item.id)
    session.add(row)
    row.cheap_score = round(final, 6)
    row.final_score = round(final, 6)
    row.importance = round(0.45 * engagement + 0.35 * coverage + 0.2 * source_quality, 6)
    row.novelty = round(novelty, 6)
    row.interest = round(interest, 6)
    row.community = round(engagement, 6)
    row.momentum = round(momentum, 6)
    row.spam = round(spam, 6)
    row.is_candidate = final >= 0.28 and not item.is_hidden and not item.duplicate_of
    row.components = {k: round(v, 5) for k, v in components.items()}
    row.evidence = evidence | ({"notes": interest_notes} if interest_notes else {})
    return row


def rescore(session: Session, *, days: int = 14, limit: int = 3000) -> int:
    cutoff = utcnow() - timedelta(days=days)
    items = session.scalars(
        select(Item).where(Item.first_seen_at >= cutoff).order_by(Item.first_seen_at.desc()).limit(limit)
    ).all()
    ctx = rank_context(session)
    for item in items:
        score_item(session, item, ctx)
    session.flush()
    return len(items)


@dataclass
class FeedEntry:
    item: Item
    section: str
    score: float
    reason: str


def build_feed(session: Session, *, limit: int = 40, period_hours: int = 168,
               mode: str = "mixed") -> list[FeedEntry]:
    """Build a stable mixed feed with explicit exploration capacity and source diversity."""
    cutoff = utcnow() - timedelta(hours=period_hours)
    rows = session.execute(
        select(Item, ItemScore).join(ItemScore, ItemScore.item_id == Item.id)
        .where(Item.first_seen_at >= cutoff, Item.is_hidden.is_(False), Item.duplicate_of.is_(None))
        .order_by(ItemScore.final_score.desc(), Item.first_seen_at.desc()).limit(max(limit * 8, 100))
    ).all()
    if mode == "newest":
        rows.sort(key=lambda x: x[0].published_at or x[0].first_seen_at, reverse=True)
    elif mode == "novelty":
        rows.sort(key=lambda x: (x[1].novelty, x[1].final_score), reverse=True)
    elif mode == "projects":
        rows = [r for r in rows if r[0].repository_id]

    budget = get_setting(session, "feed.budget", {})
    quotas = {k: max(1, round(limit * float(v))) for k, v in budget.items()}
    initial = dict(quotas)
    reddit_elsewhere: dict[str, int] = {}
    picked: list[FeedEntry] = []
    per_domain: dict[str, int] = {}
    clusters: set[int] = set()
    communities = dict(session.execute(
        select(CommunityPost.item_id, CommunityPost.community)
        .where(CommunityPost.platform == "reddit", CommunityPost.item_id.in_([r[0].id for r in rows]))
    ).all()) if rows else {}
    per_community_cap = max(2, round(limit * float(get_setting(session, "feed.max_per_community", 0.08))))

    def diversity_key(item: Item) -> tuple[str, int]:
        # Reddit posts are diversified per subreddit, everything else per domain.
        if item.id in communities:
            return f"r/{(communities[item.id] or '').lower()}", per_community_cap
        return item.domain or "unknown", max(2, limit // 8)

    def section_for(item: Item, score: ItemScore, index: int) -> str:
        if item.repository_id and quotas.get("oss", 0) > 0:
            return "oss"
        if item.content_type == "reddit_post" and quotas.get("reddit", 0) > 0:
            return "reddit"
        if score.importance >= 0.55 and quotas.get("important", 0) > 0:
            return "important"
        if (score.interest < 0.52 or score.novelty > 0.82) and quotas.get("adjacent", 0) > 0:
            return "wildcard" if index % 5 == 0 and quotas.get("wildcard", 0) > 0 else "adjacent"
        return "for_you"

    rows = [r for r in rows if not (r[0].meta or {}).get("reddit_account")]  # already upvoted/saved there
    for index, (item, score) in enumerate(rows):
        if len(picked) >= limit:
            break
        if item.cluster_id and item.cluster_id in clusters:
            continue
        domain, cap = diversity_key(item)
        if per_domain.get(domain, 0) >= cap:
            continue
        section = section_for(item, score, index)
        if quotas.get(section, 0) <= 0:
            continue
        if item.content_type == "reddit_post" and section != "reddit":
            # Reddit leads with its own section; elsewhere it may fill at most half the slots.
            if reddit_elsewhere.get(section, 0) >= math.ceil(initial[section] / 2):
                continue
            reddit_elsewhere[section] = reddit_elsewhere.get(section, 0) + 1
        quotas[section] -= 1
        if item.cluster_id:
            clusters.add(item.cluster_id)
        per_domain[domain] = per_domain.get(domain, 0) + 1
        top = sorted(score.components.items(), key=lambda kv: kv[1], reverse=True)[:2]
        reason = " · ".join(k.replace("_", " ") for k, _ in top)
        picked.append(FeedEntry(item, section, score.final_score, reason))

    if len(picked) < limit:
        used = {p.item.id for p in picked}
        used_clusters = {p.item.cluster_id for p in picked if p.item.cluster_id}
        for item, score in rows:
            if item.id not in used and (not item.cluster_id or item.cluster_id not in used_clusters):
                picked.append(FeedEntry(item, "for_you", score.final_score, "fresh discovery"))
                used.add(item.id)
                if item.cluster_id:
                    used_clusters.add(item.cluster_id)
                if len(picked) >= limit:
                    break
    return picked
