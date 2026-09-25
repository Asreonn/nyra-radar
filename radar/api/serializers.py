"""Stable public JSON shapes shared by the UI and Nyra client."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from radar.db.models import (
    CommunityPost,
    Enrichment,
    Image,
    Item,
    ItemScore,
    Repository,
    SavedItem,
    StoryCluster,
)
from radar.services.community import community_info
from radar.util import html_to_text, iso

HTML_BODY_TYPES = {"hackernews_post", "telegram_post"}


def readable_body(item: Item) -> str | None:
    """Post text with paragraphs intact (clean_text is whitespace-collapsed for indexing)."""
    raw = item.raw_text
    if not raw:
        return None
    if item.content_type in HTML_BODY_TYPES or raw.lstrip().startswith("<"):
        return html_to_text(raw) or None
    return raw.strip() or None


def item_dict(session: Session, item: Item, *, detail: bool = False) -> dict:
    score = session.get(ItemScore, item.id)
    repo = session.get(Repository, item.repository_id) if item.repository_id else None
    out = {
        "id": item.id, "type": item.content_type, "title": item.title,
        "url": item.canonical_url, "link_url": item.link_url,
        "domain": item.domain, "author": item.author, "excerpt": item.excerpt,
        "summary": item.summary, "image_url": f"/media/{item.image_id}" if item.image_id else None,
        "published_at": iso(item.published_at), "first_seen_at": iso(item.first_seen_at),
        "source": ({"id": item.source.id, "name": item.source.name, "kind": item.source.kind}
                   if item.source else None),
        "cluster_id": item.cluster_id, "repository_id": item.repository_id,
        "external": {"score": item.external_score, "comments": item.external_comment_count},
        "user": {"vote": item.user_vote, "read": item.is_read, "hidden": item.is_hidden,
                 "saved": session.scalar(select(SavedItem.id).where(SavedItem.item_id == item.id)) is not None},
        "score": ({"total": score.final_score, "importance": score.importance, "novelty": score.novelty,
                   "interest": score.interest, "community": score.community, "momentum": score.momentum,
                   "components": score.components, "evidence": score.evidence} if score else None),
        "why_it_matters": (item.meta or {}).get("why_it_matters"),
    }
    image = session.get(Image, item.image_id) if item.image_id else None
    if image is not None:
        out["image"] = {"url": out["image_url"], "width": image.width, "height": image.height}
    community = session.get(CommunityPost, item.id)
    if community:
        out["community"] = community_info(community, item)
    if repo:
        out["repository"] = {
            "id": repo.id, "full_name": repo.display_name, "description": repo.description,
            "stars": repo.stars, "forks": repo.forks, "language": repo.language, "license": repo.license,
            "topics": repo.topics, "pushed_at": iso(repo.pushed_at), "stars_delta_24h": repo.stars_delta_24h,
            "stars_delta_7d": repo.stars_delta_7d, "mentions_7d": repo.mentions_7d,
            "momentum": repo.momentum, "homepage": repo.homepage,
        }
    if detail:
        out["content"] = item.clean_text
        out["body"] = readable_body(item) if community else None
        out["metadata"] = item.meta
        out["processing"] = {"extraction": item.extraction_state, "image": item.image_state,
                             "embedding": item.embedding_state, "cluster": item.cluster_state,
                             "summary": item.summary_state}
        out["analysis"] = [e.data for e in session.scalars(
            select(Enrichment).where(Enrichment.item_id == item.id).order_by(Enrichment.created_at.desc()))]
    return out


def cluster_dict(session: Session, cluster: StoryCluster) -> dict:
    members = session.scalars(select(Item).where(Item.cluster_id == cluster.id)
                              .order_by(Item.first_seen_at.desc())).all()
    return {"id": cluster.id, "title": cluster.title, "summary": cluster.summary,
            "item_count": cluster.item_count, "source_count": cluster.source_count,
            "platforms": cluster.platforms, "first_seen_at": iso(cluster.first_seen_at),
            "last_updated_at": iso(cluster.last_updated_at),
            "representative_item_id": cluster.representative_item_id,
            "sources": [item_dict(session, m) for m in members]}
