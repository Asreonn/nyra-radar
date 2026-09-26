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
    KV,
    CommunityPost,
    Embedding,
    Interest,
    Item,
    ItemScore,
    ItemTopic,
    Repository,
    SavedItem,
    Source,
    SourcePreference,
    StoryCluster,
)
from radar.services.community import feed_excluded
from radar.services.settings import get as get_setting
from radar.util import iso, parse_datetime, utcnow


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


MIN_PROFILE_ITEMS = 2
NEGATIVE_PULL = 0.8  # how strongly similarity to disliked items pulls semantic_preference down


@dataclass
class Taste:
    """One side of the taste profile: centroid of example embeddings plus the sorted similarities
    of every *other* item, so an item's similarity becomes a percentile."""
    centroid: np.ndarray
    reference: np.ndarray
    size: int

    def percentile(self, vector: np.ndarray) -> float:
        # The epsilon absorbs float32 rounding so an item always counts its own reference entry.
        similarity = float(vector @ self.centroid) + 1e-6
        return float(np.searchsorted(self.reference, similarity, side="right")) / len(self.reference)


@dataclass
class RankContext:
    """Per-rescore facts shared by every item: community scales, taste profile, subreddit sources."""
    scales: dict[tuple[str, str], tuple[float, float]]
    index: VectorIndex | None = None
    liked: Taste | None = None
    disliked: Taste | None = None
    subreddit_sources: dict[str, int] | None = None

    @property
    def profile_size(self) -> int:
        return (self.liked.size if self.liked else 0) + (self.disliked.size if self.disliked else 0)


def _taste(index: VectorIndex, ids: list[int], exclude: np.ndarray) -> Taste | None:
    if len(ids) < MIN_PROFILE_ITEMS:
        return None
    _, vectors = index.vectors(ids)
    if vectors.shape[0] < MIN_PROFILE_ITEMS:
        return None
    centroid = vectors.mean(axis=0)
    norm = float(np.linalg.norm(centroid))
    others = index.matrix[~np.isin(index.ids, exclude)]
    if norm == 0 or not len(others):
        return None
    centroid = centroid / norm
    return Taste(centroid, np.sort(others @ centroid), int(vectors.shape[0]))


def taste_profile(session: Session) -> tuple[VectorIndex | None, Taste | None, Taste | None]:
    """Liked and saved items (including Reddit upvotes/saves imported from a connected account)
    pull `semantic_preference` up; items marked "less like this" push similar ones down."""
    model = session.scalar(select(Embedding.model).group_by(Embedding.model)
                           .order_by(func.count(Embedding.item_id).desc()).limit(1))
    if not model:
        return None, None, None
    liked = list(session.scalars(
        select(Item.id).outerjoin(SavedItem, SavedItem.item_id == Item.id)
        .where(or_(Item.user_vote == 1, SavedItem.id.is_not(None)))
        .order_by(Item.last_opened_at.desc(), Item.id.desc()).limit(500)))
    disliked = list(session.scalars(select(Item.id).where(Item.user_vote == -1)
                                    .order_by(Item.id.desc()).limit(500)))
    if len(liked) < MIN_PROFILE_ITEMS and len(disliked) < MIN_PROFILE_ITEMS:
        return None, None, None
    index = get_index(session, model)
    examples = np.asarray(liked + disliked, dtype=np.int64)
    return index, _taste(index, liked, examples), _taste(index, disliked, examples)


def rank_context(session: Session) -> RankContext:
    index, liked, disliked = taste_profile(session)
    subs = {ident.lower(): sid for sid, ident in session.execute(
        select(Source.id, Source.identifier).where(Source.kind == "reddit")) if not ident.startswith("http")}
    return RankContext(community_scales(session), index, liked, disliked, subs)


def _semantic(ctx: RankContext, item: Item) -> float:
    if ctx.index is None or (ctx.liked is None and ctx.disliked is None):
        return 0.5
    vector = ctx.index.vector(item.id)
    taste = ctx.liked or ctx.disliked
    if vector is None or vector.shape[-1] != taste.centroid.shape[-1]:
        return 0.5
    value = ctx.liked.percentile(vector) if ctx.liked else 0.5
    if ctx.disliked:
        value -= NEGATIVE_PULL * (ctx.disliked.percentile(vector) - 0.5)
    return max(0.0, min(1.0, value))


def _affinity(session: Session, ctx: RankContext, item: Item, community: CommunityPost | None) -> float:
    """Learned source affinity (0.2..1.8, neutral 1.0) mapped to 0..1 with neutral 0.5. Reddit posts
    use their subreddit's preference, whichever feed (front page, subreddit) delivered them."""
    source_id = item.source_id
    if community and community.platform == "reddit" and community.community and ctx.subreddit_sources:
        source_id = ctx.subreddit_sources.get(community.community.lower(), source_id)
    pref = session.get(SourcePreference, source_id) if source_id else None
    return 0.5 if pref is None else max(0.0, min(1.0, (float(pref.affinity) - 0.2) / 1.6))


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
    cluster = session.get(StoryCluster, item.cluster_id) if item.cluster_id else None

    freshness = _freshness(item, half_lives)
    interest, interest_notes = _interest(session, item.id)
    source_quality = float(item.source.quality if item.source else 0.5)
    source_affinity = _affinity(session, ctx, item, community)
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
        evidence["taste_profile"] = {"liked": ctx.liked.size if ctx.liked else 0,
                                     "disliked": ctx.disliked.size if ctx.disliked else 0,
                                     "value": round(semantic, 3)}
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


RESCORED_KEY = "ranking.rescored_at"


def rescore(session: Session, *, days: int = 14, limit: int = 3000, background: bool = False) -> int:
    """Re-rank recent items. `background` computes every score first and writes them in one short
    flush at the end, so a pass running beside the API holds SQLite's write lock for milliseconds
    instead of seconds (page loads and clicks never queue behind it)."""
    cutoff = utcnow() - timedelta(days=days)
    items = session.scalars(
        select(Item).where(Item.first_seen_at >= cutoff).order_by(Item.first_seen_at.desc()).limit(limit)
    ).all()
    ctx = rank_context(session)
    if background:
        with session.no_autoflush:
            for item in items:
                score_item(session, item, ctx)
    else:
        for item in items:
            score_item(session, item, ctx)
    row = session.get(KV, RESCORED_KEY) or KV(key=RESCORED_KEY)
    session.add(row)
    row.value = {"at": iso(utcnow()), "items": len(items)}
    session.flush()
    return len(items)


def last_rescored(session: Session):
    row = session.get(KV, RESCORED_KEY)
    return parse_datetime((row.value or {}).get("at")) if row and row.value else None


def score_unscored(session: Session, *, days: int = 14, limit: int = 500) -> int:
    """Score only items that have no score yet (fresh arrivals); cheap enough for a request."""
    cutoff = utcnow() - timedelta(days=days)
    items = session.scalars(
        select(Item).outerjoin(ItemScore, ItemScore.item_id == Item.id)
        .where(Item.first_seen_at >= cutoff, ItemScore.item_id.is_(None)).limit(limit)
    ).all()
    if items:
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

    rows = [r for r in rows if not feed_excluded(r[0])]
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
