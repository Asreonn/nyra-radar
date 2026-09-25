"""Plain-language taste surfaces: why an item is shown, what Radar has learned, and a
diverse sample of posts to rate (the Tune page).

Everything here reads stored evidence (`item_scores`, interests, source preferences); nothing is
inferred that the ranker did not actually use.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from radar.collectors.reddit import RANKED_SORTS
from radar.db.models import (
    CommunityPost,
    Interest,
    Item,
    ItemScore,
    ItemTopic,
    SavedItem,
    Source,
    SourcePreference,
    StoryCluster,
    Topic,
    UserEvent,
)
from radar.util import utcnow


def _source_name(source: Source | None, community: CommunityPost | None) -> str | None:
    if community and community.platform == "reddit" and community.community:
        return f"r/{community.community}"
    if community and community.platform == "hackernews":
        return "Hacker News"
    return source.name if source else None


def why(session: Session, item: Item) -> list[str]:
    """Up to four short reasons, strongest first, for showing this item."""
    score = session.get(ItemScore, item.id)
    if score is None:
        return []
    components, evidence = score.components or {}, score.evidence or {}
    community = session.get(CommunityPost, item.id)
    reasons: list[tuple[float, str]] = []
    taste = evidence.get("taste_profile")
    semantic = components.get("semantic_preference", 0.5)
    if taste and semantic >= 0.7:
        reasons.append((semantic, "Similar to posts you liked"))
    elif taste and semantic <= 0.3 and taste.get("disliked"):
        reasons.append((1 - semantic, "Similar to things you asked to see less of"))
    topics = session.execute(
        select(Topic.name, Interest.weight).join(ItemTopic, ItemTopic.topic_id == Topic.id)
        .join(Interest, Interest.topic_id == Topic.id).where(ItemTopic.item_id == item.id)
        .order_by(Interest.weight.desc())
    ).all()
    liked = [name for name, weight in topics if weight > 0.1]
    if liked:
        reasons.append((0.6, "Matches your interests: " + ", ".join(liked[:3])))
    affinity = components.get("source_affinity", 0.5)
    name = _source_name(item.source, community)
    if name and affinity >= 0.6:
        reasons.append((affinity, f"You often like {name}"))
    elif name and affinity <= 0.4:
        reasons.append((0.5, f"You asked for less from {name}"))
    comm = evidence.get("community") or {}
    if comm.get("listing") in RANKED_SORTS and comm.get("rank"):
        label = "on the front page" if comm["listing"] == "front" else f"in {name} {comm['listing']}"
        reasons.append((components.get("community_signal", 0), f"#{comm['rank']} {label}"))
    elif comm.get("score"):
        noun = "points" if community and community.platform == "hackernews" else "upvotes"
        reasons.append((components.get("community_signal", 0), f"{comm['score']:,} {noun} · {comm.get('comments', 0):,} comments"))
    cluster = session.get(StoryCluster, item.cluster_id) if item.cluster_id else None
    if cluster and cluster.source_count > 1:
        reasons.append((components.get("coverage", 0), f"Covered by {cluster.source_count} sources"))
    if components.get("freshness", 0) >= 0.8:
        reasons.append((0.3, "New in the last few hours"))
    reasons.sort(key=lambda r: r[0], reverse=True)
    return [text for _, text in reasons[:4]]


def profile(session: Session) -> dict:
    """What the ranker has learned, in terms a person can check and correct."""
    likes = session.scalar(select(func.count(Item.id)).where(Item.user_vote == 1)) or 0
    dislikes = session.scalar(select(func.count(Item.id)).where(Item.user_vote == -1)) or 0
    saved = session.scalar(select(func.count(SavedItem.id))) or 0
    hidden = session.scalar(select(func.count(Item.id)).where(Item.is_hidden.is_(True))) or 0
    reactions = session.scalar(select(func.count(UserEvent.id)).where(
        UserEvent.event_type.in_(("like", "dislike", "save", "skip", "dismiss", "hide")))) or 0
    undone = session.scalar(select(func.count(UserEvent.id)).where(UserEvent.event_type == "undo")) or 0
    ratings = max(0, reactions - undone)
    prefs = session.execute(select(Source, SourcePreference)
                            .join(SourcePreference, SourcePreference.source_id == Source.id)).all()
    scored = sorted(((max(0.0, min(1.0, (p.affinity - 0.2) / 1.6)), s) for s, p in prefs),
                    key=lambda r: r[0], reverse=True)

    def label(source: Source) -> str:
        if source.kind == "reddit" and not source.identifier.startswith("http"):
            return f"r/{source.identifier}"
        return source.name

    topics = session.execute(select(Topic.name, Interest.weight, Interest.positive_evidence,
                                    Interest.negative_evidence).join(Interest, Interest.topic_id == Topic.id)
                             .order_by(Interest.weight.desc())).all()
    learned = [t for t in topics if t.positive_evidence or t.negative_evidence]
    return {
        "counts": {"likes": likes, "dislikes": dislikes, "saved": saved, "hidden": hidden, "ratings": ratings},
        "more_from": [label(s) for a, s in scored if a >= 0.55][:8],
        "less_from": [label(s) for a, s in reversed(scored) if a <= 0.45][:8],
        "topics_up": [t.name for t in learned if t.weight > 0.05][:8],
        "topics_down": [t.name for t in reversed(learned) if t.weight < -0.05][:8],
    }


def tune_candidates(session: Session, limit: int = 12, days: int = 4) -> list[Item]:
    """Recent unrated posts spread over as many subreddits/sources as possible, best first in
    each, so a dozen ratings cover the whole feed rather than one subreddit."""
    rows = session.execute(
        select(Item, ItemScore, CommunityPost.community)
        .join(ItemScore, ItemScore.item_id == Item.id)
        .outerjoin(CommunityPost, CommunityPost.item_id == Item.id)
        .where(Item.first_seen_at >= utcnow() - timedelta(days=days), Item.is_hidden.is_(False),
               Item.user_vote == 0, Item.duplicate_of.is_(None),
               Item.content_type.not_in(("github_release", "website_change")))
        .order_by(ItemScore.final_score.desc()).limit(1500)
    ).all()
    saved = set(session.scalars(select(SavedItem.item_id)))
    groups: dict[str, list[Item]] = {}
    for item, _score, community in rows:
        if item.id in saved or (item.meta or {}).get("reddit_account"):
            continue
        key = f"r/{community.lower()}" if community else f"s{item.source_id}"
        groups.setdefault(key, []).append(item)
    picked: list[Item] = []
    depth = 0
    while len(picked) < limit and any(len(g) > depth for g in groups.values()):
        for group in groups.values():
            if len(group) > depth and len(picked) < limit:
                picked.append(group[depth])
        depth += 1
    return picked
