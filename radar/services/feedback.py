"""Explicit user feedback and transparent, decayed interest learning."""
from __future__ import annotations

import math

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from radar.db.models import (
    CommunityPost,
    Interest,
    Item,
    ItemTopic,
    SavedItem,
    Source,
    SourcePreference,
    Topic,
    UserEvent,
)
from radar.services.settings import get as get_setting
from radar.util import utcnow

EVENT_WEIGHTS = {"like": 1.0, "dislike": -1.2, "save": 1.4, "unsave": -0.5,
                 "open": 0.12, "dismiss": -0.45, "hide": -0.8, "skip": 0.0, "read": 0.08,
                 "impression": 0.0}

# What a "less like this" teaches depends on why: (learn topics, learn source, weight, taste example).
# `taste example` makes the item a negative example for the embedding taste profile.
DISLIKE_REASONS: dict[str, tuple[bool, bool, float, bool]] = {
    "topic": (True, False, -1.2, True),      # not interested in this kind of thing
    "source": (False, True, -2.5, False),    # less from this subreddit / site
    "quality": (False, True, -1.0, True),    # clickbait, low effort
    "seen": (False, False, 0.0, False),      # already seen elsewhere: just hide
}
UNDOABLE = ("like", "dislike", "save", "dismiss", "hide", "skip")
SOURCE_ATTRS = {"open": "opens", "save": "saves", "like": "likes", "dislike": "dislikes",
                "dismiss": "dismisses", "hide": "dismisses"}


def _feedback_source(session: Session, item: Item, reason: str | None) -> int | None:
    """Reddit feedback about "the source" means the subreddit, whichever feed delivered the post."""
    if reason == "source" and item.content_type == "reddit_post":
        post = session.get(CommunityPost, item.id)
        if post and post.community:
            sub = session.scalar(select(Source.id).where(
                Source.kind == "reddit", func.lower(Source.identifier) == post.community.lower()))
            if sub:
                return sub
    return item.source_id


def record(session: Session, item: Item, event_type: str, context: dict | None = None) -> UserEvent:
    if event_type not in EVENT_WEIGHTS:
        raise ValueError(f"unsupported feedback: {event_type}")
    context = dict(context or {})
    reason = context.get("reason") if event_type == "dislike" else None
    if reason is not None and reason not in DISLIKE_REASONS:
        raise ValueError(f"unsupported reason: {reason}")
    topic_rows = session.execute(
        select(ItemTopic.topic_id, ItemTopic.weight).where(ItemTopic.item_id == item.id)
    ).all()
    topics = [[a, b] for a, b in topic_rows]
    source_id = _feedback_source(session, item, reason)
    weight = EVENT_WEIGHTS[event_type]
    taste = event_type == "dislike"
    if reason:
        learn_topics, learn_source, weight, taste = DISLIKE_REASONS[reason]
        topics = topics if learn_topics else []
        source_id = source_id if learn_source else None
    event = UserEvent(item_id=item.id, event_type=event_type, source_id=source_id,
                      content_type=item.content_type, topics=topics, weight=weight, context=context)
    session.add(event)
    if event_type == "like":
        item.user_vote = 1
        item.is_hidden = False
    elif event_type == "dislike":
        item.user_vote = -1 if taste else 0
        item.is_hidden = True
    elif event_type in ("dismiss", "hide", "skip"):
        item.is_hidden = True
    elif event_type in ("read", "open"):
        item.is_read = True
        item.last_opened_at = utcnow()
    elif event_type == "impression":
        item.impressions += 1
    return event


def undo_last(session: Session, item: Item) -> UserEvent | None:
    """Reverse the latest reaction to an item with an inverse event (the log stays append-only)."""
    events = session.scalars(select(UserEvent).where(UserEvent.item_id == item.id,
                                                     UserEvent.event_type.in_(UNDOABLE))
                             .order_by(UserEvent.ts.desc(), UserEvent.id.desc())).all()
    last = next((e for e in events if not (e.context or {}).get("undone")), None)
    if last is None:
        return None
    last.context = {**(last.context or {}), "undone": True}
    inverse = UserEvent(item_id=item.id, event_type="undo", source_id=last.source_id,
                        content_type=last.content_type, topics=last.topics, weight=-float(last.weight),
                        context={"undoes": last.id, "undoes_type": last.event_type})
    session.add(inverse)
    if last.event_type in ("like", "dislike"):
        item.user_vote = 0
    if last.event_type in ("dislike", "dismiss", "hide", "skip"):
        item.is_hidden = False
    if last.event_type == "save":
        saved = session.scalar(select(SavedItem).where(SavedItem.item_id == item.id))
        if saved:
            session.delete(saved)
    return inverse


def fold_events(session: Session) -> int:
    """Fold unapplied events into topic/source preferences; idempotent via applied flag."""
    events = session.scalars(select(UserEvent).where(UserEvent.applied.is_(False)).order_by(UserEvent.ts)).all()
    for event in events:
        for topic_id, item_weight in event.topics or []:
            row = session.scalar(select(Interest).where(Interest.topic_id == int(topic_id)))
            if row is None:
                row = Interest(topic_id=int(topic_id))
                session.add(row)
            delta = float(event.weight) * float(item_weight)
            row.learned += delta
            if event.event_type == "undo":  # retract the evidence the undone event added
                row.positive_evidence = max(0, row.positive_evidence - int(delta < 0))
                row.negative_evidence = max(0, row.negative_evidence - int(delta > 0))
            else:
                row.positive_evidence += int(delta > 0)
                row.negative_evidence += int(delta < 0)
            row.last_evidence_at = event.ts
        if event.source_id:
            pref = session.get(SourcePreference, event.source_id)
            if pref is None:
                pref = SourcePreference(source_id=event.source_id)
                session.add(pref)
            if event.event_type == "undo":
                attr = SOURCE_ATTRS.get((event.context or {}).get("undoes_type"))
                if attr:
                    setattr(pref, attr, max(0.0, (getattr(pref, attr) or 0.0) - abs(event.weight)))
            elif attr := SOURCE_ATTRS.get(event.event_type):
                setattr(pref, attr, (getattr(pref, attr) or 0.0) + abs(event.weight))
        event.applied = True
    refresh_interests(session)
    return len(events)


def refresh_interests(session: Session) -> None:
    now = utcnow()
    half_life = float(get_setting(session, "interests.decay_half_life_days", 30))
    for row in session.scalars(select(Interest)):
        if row.last_evidence_at:
            days = max(0.0, (now - row.last_evidence_at).total_seconds() / 86400)
            decayed = row.learned * 2 ** (-days / max(1.0, half_life))
        else:
            decayed = row.learned
        row.weight = -1.0 if row.muted else max(-1.0, min(1.0,
            row.seed_weight + math.tanh(decayed / 3) * 0.7 + row.inferred + row.manual_adjust))
        row.updated_at = now
    for pref in session.scalars(select(SourcePreference)):
        positive = pref.likes * 1.2 + pref.saves + pref.opens * 0.08
        negative = pref.dislikes * 1.2 + pref.dismisses * 0.5
        pref.affinity = max(0.2, min(1.8, 1.0 + math.tanh((positive - negative) / 8) * 0.6))


def explain_interests(session: Session) -> list[dict]:
    rows = session.execute(
        select(Interest, Topic).join(Topic, Topic.id == Interest.topic_id)
        .order_by(Interest.weight.desc())
    ).all()
    return [{"topic_id": topic.id, "topic": topic.name, "weight": round(interest.weight, 4),
             "seed": interest.seed_weight, "learned": round(interest.learned, 4),
             "manual": interest.manual_adjust, "pinned": interest.pinned, "muted": interest.muted,
             "positive_evidence": interest.positive_evidence,
             "negative_evidence": interest.negative_evidence} for interest, topic in rows]
