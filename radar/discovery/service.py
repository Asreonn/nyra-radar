"""Auditable source discovery from repeatedly observed outbound links."""
from __future__ import annotations

from collections import Counter
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from radar.db.models import DiscoveryAudit, DiscoveryCandidate, Item, ItemLink, Source
from radar.services.settings import get as get_setting
from radar.util import utcnow

KNOWN_FEEDS = ("/feed", "/rss", "/atom.xml", "/feed.xml", "/index.xml")
IGNORED = {"github.com", "youtube.com", "www.youtube.com", "reddit.com", "www.reddit.com",
           "news.ycombinator.com", "x.com", "twitter.com"}


def mine_candidates(session: Session, minimum_evidence: int = 2) -> int:
    cutoff = utcnow() - timedelta(days=14)
    links = session.scalars(
        select(ItemLink).join_from(ItemLink, Item).where(Item.first_seen_at >= cutoff)
    ).all()
    items = session.scalars(select(Item).where(Item.first_seen_at >= cutoff)).all()
    exact_feeds: dict[str, list[str]] = {}
    for item in items:
        for feed in (item.meta or {}).get("page_feeds", []):
            exact_feeds.setdefault(feed, []).append(item.canonical_url)
    counts = Counter(link.domain for link in links if link.domain and link.domain not in IGNORED)
    created = 0
    possible = [(feed, len(evidence), feed.split("/")[2], "exact_match", evidence)
                for feed, evidence in exact_feeds.items()]
    possible += [(f"https://{domain}/feed", count, domain, "link_mining",
                  sorted({link.url for link in links if link.domain == domain})[:8])
                 for domain, count in counts.most_common(50) if count >= minimum_evidence]
    for identifier, count, domain, method, evidence in possible:
        candidate = session.scalar(select(DiscoveryCandidate).where(
            DiscoveryCandidate.kind == "rss", DiscoveryCandidate.identifier == identifier))
        if candidate is None:
            candidate = DiscoveryCandidate(kind="rss", identifier=identifier, name=domain, url=f"https://{domain}",
                                           method=method, confidence=(0.9 if method == "exact_match" else
                                                                     min(0.85, 0.35 + count * 0.08)),
                                           evidence=evidence, evidence_count=count)
            session.add(candidate)
            session.flush()
            session.add(DiscoveryAudit(candidate_id=candidate.id, action="discovered",
                                       reason=f"{count} recent independent links to {domain}",
                                       metrics={"link_count": count}))
            created += 1
        else:
            candidate.evidence = evidence
            candidate.evidence_count = count
            candidate.confidence = min(0.9, 0.35 + count * 0.08)
    return created


def auto_start_trials(session: Session) -> int:
    if not get_setting(session, "discovery.auto_trial", True):
        return 0
    max_trials = int(get_setting(session, "discovery.max_active_trials", 6))
    active = session.scalar(select(func.count(Source.id)).where(Source.status == "trial")) or 0
    candidates = session.scalars(select(DiscoveryCandidate).where(
        DiscoveryCandidate.status == "candidate", DiscoveryCandidate.confidence >= 0.72
    ).order_by(DiscoveryCandidate.confidence.desc()).limit(max(0, max_trials - active))).all()
    for candidate in candidates:
        start_trial(session, candidate)
    return len(candidates)


def review_trials(session: Session) -> dict[str, int]:
    """Promote useful trials and reject duplicate/noisy ones with an auditable reason."""
    days = int(get_setting(session, "discovery.trial_days", 3))
    minimum = int(get_setting(session, "discovery.trial_min_items", 8))
    auto_promote = bool(get_setting(session, "discovery.auto_promote", True))
    reviewed = promoted = rejected = 0
    candidates = session.scalars(select(DiscoveryCandidate).where(
        DiscoveryCandidate.status == "trial", DiscoveryCandidate.trial_started_at <= utcnow() - timedelta(days=days)
    )).all()
    for candidate in candidates:
        source = session.get(Source, candidate.source_id) if candidate.source_id else None
        if source is None:
            decide_trial(session, candidate, False, "trial source disappeared")
            rejected += 1
        elif source.item_count < minimum:
            decide_trial(session, candidate, False, f"only {source.item_count} items during {days}-day trial")
            rejected += 1
        elif source.duplicate_rate > 0.8 or source.consecutive_failures >= 4:
            decide_trial(session, candidate, False,
                         f"duplicate rate {source.duplicate_rate:.0%}; failures {source.consecutive_failures}")
            rejected += 1
        elif auto_promote:
            decide_trial(session, candidate, True,
                         f"{source.item_count} items; quality {source.quality:.2f}; duplicate rate {source.duplicate_rate:.0%}")
            promoted += 1
        reviewed += 1
    return {"reviewed": reviewed, "promoted": promoted, "rejected": rejected}


def start_trial(session: Session, candidate: DiscoveryCandidate) -> Source:
    source = session.scalar(select(Source).where(Source.kind == candidate.kind,
                                                 Source.identifier == candidate.identifier))
    if source is None:
        source = Source(kind=candidate.kind, identifier=candidate.identifier, name=candidate.name,
                        url=candidate.url, status="trial", origin="discovered", config={"max_items": 15})
        session.add(source)
        session.flush()
    candidate.status = "trial"
    candidate.source_id = source.id
    candidate.trial_started_at = utcnow()
    session.add(DiscoveryAudit(candidate_id=candidate.id, action="trial",
                               reason="user or confidence threshold started a bounded trial", metrics={}))
    return source


def decide_trial(session: Session, candidate: DiscoveryCandidate, promote: bool, reason: str) -> None:
    source = session.get(Source, candidate.source_id) if candidate.source_id else None
    candidate.status = "promoted" if promote else "rejected"
    candidate.decided_at = utcnow()
    candidate.decision_reason = reason
    if source:
        source.status = "active" if promote else "rejected"
        source.decision_reason = reason
    session.add(DiscoveryAudit(candidate_id=candidate.id, action=candidate.status, reason=reason,
                               metrics=candidate.metrics or {}))
