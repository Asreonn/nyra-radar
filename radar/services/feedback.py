"""Explicit user feedback and transparent, decayed interest learning."""
from __future__ import annotations

import math

from sqlalchemy import select
from sqlalchemy.orm import Session

from radar.db.models import Interest, Item, ItemTopic, SourcePreference, Topic, UserEvent
from radar.services.settings import get as get_setting
from radar.util import utcnow

EVENT_WEIGHTS = {"like": 1.0, "dislike": -1.2, "save": 1.4, "unsave": -0.5,
                 "open": 0.12, "dismiss": -0.45, "hide": -0.8, "read": 0.08,
                 "impression": 0.0}


def record(session: Session, item: Item, event_type: str, context: dict | None = None) -> UserEvent:
    if event_type not in EVENT_WEIGHTS:
        raise ValueError(f"unsupported feedback: {event_type}")
    topic_rows = session.execute(
        select(ItemTopic.topic_id, ItemTopic.weight).where(ItemTopic.item_id == item.id)
    ).all()
    event = UserEvent(item_id=item.id, event_type=event_type, source_id=item.source_id,
                      content_type=item.content_type, topics=[[a, b] for a, b in topic_rows],
                      weight=EVENT_WEIGHTS[event_type], context=context or {})
    session.add(event)
    if event_type == "like":
        item.user_vote = 1
    elif event_type == "dislike":
        item.user_vote = -1
    elif event_type in ("dismiss", "hide"):
        item.is_hidden = True
    elif event_type in ("read", "open"):
        item.is_read = True
        item.last_opened_at = utcnow()
    elif event_type == "impression":
        item.impressions += 1
    return event


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
            row.positive_evidence += int(delta > 0)
            row.negative_evidence += int(delta < 0)
            row.last_evidence_at = event.ts
        if event.source_id:
            pref = session.get(SourcePreference, event.source_id)
            if pref is None:
                pref = SourcePreference(source_id=event.source_id)
                session.add(pref)
            attr = {"open": "opens", "save": "saves", "like": "likes", "dislike": "dislikes",
                    "dismiss": "dismisses", "hide": "dismisses"}.get(event.event_type)
            if attr:
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
