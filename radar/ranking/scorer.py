"""Explainable cheap ranking and diversity-aware feed construction."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from radar.db.models import (
    CommunityPost,
    Interest,
    Item,
    ItemScore,
    ItemTopic,
    Repository,
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


def score_item(session: Session, item: Item) -> ItemScore:
    """Compute deterministic components. Unknown signals remain neutral, not invented."""
    weights = get_setting(session, "ranking.weights", {})
    half_lives = get_setting(session, "feed.freshness_half_life_hours", {"default": 30})
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
        engagement = 0.65 * _sat(community.score, 120) + 0.35 * _sat(community.num_comments, 45)
        evidence["community"] = {"score": community.score, "comments": community.num_comments}
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
        "semantic_preference": 0.5,
        "freshness": freshness,
        "novelty": novelty,
        "source_quality": source_quality,
        "source_affinity": source_affinity,
        "content_type_preference": 0.6 if item.content_type.startswith("github") else 0.5,
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
    for item in items:
        score_item(session, item)
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
    picked: list[FeedEntry] = []
    per_domain: dict[str, int] = {}
    clusters: set[int] = set()

    def section_for(item: Item, score: ItemScore, index: int) -> str:
        if item.repository_id and quotas.get("oss", 0) > 0:
            return "oss"
        if score.importance >= 0.55 and quotas.get("important", 0) > 0:
            return "important"
        if (score.interest < 0.52 or score.novelty > 0.82) and quotas.get("adjacent", 0) > 0:
            return "wildcard" if index % 5 == 0 and quotas.get("wildcard", 0) > 0 else "adjacent"
        return "for_you"

    for index, (item, score) in enumerate(rows):
        if len(picked) >= limit:
            break
        if item.cluster_id and item.cluster_id in clusters:
            continue
        domain = item.domain or "unknown"
        if per_domain.get(domain, 0) >= max(2, limit // 8):
            continue
        section = section_for(item, score, index)
        if quotas.get(section, 0) <= 0:
            continue
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
