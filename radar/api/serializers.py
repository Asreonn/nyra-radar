"""Stable public JSON shapes shared by the UI and Nyra client."""
from __future__ import annotations

import re
from urllib.parse import quote

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
from radar.extraction.canonical_url import youtube_video_id
from radar.services.community import community_info
from radar.util import html_to_text, iso

HTML_BODY_TYPES = {"hackernews_post", "telegram_post"}
# Site-wide descriptions some pages return instead of one for the linked thing.
BOILERPLATE = ("Enjoy the videos and music you love",)
_VREDDIT = re.compile(r"^https?://v\.redd\.it/([A-Za-z0-9]+)")


def community_text(item: Item) -> bool:
    """Whether the post itself has text (as opposed to text extracted from the page it links)."""
    return bool((item.raw_text or "").strip())


def media_info(item: Item) -> dict | None:
    """What the post shows: a YouTube video (its own or a linked one), a Reddit-hosted video with
    its stream URLs, or a Reddit image/gallery. The browser plays videos directly from the source."""
    meta = item.meta or {}
    if item.content_type == "youtube_video":
        vid = meta.get("video_id") or youtube_video_id(item.canonical_url)
    else:
        vid = youtube_video_id(item.link_url) or youtube_video_id(meta.get("media_url"))
    if vid:
        return {"kind": "youtube", "id": vid}
    match = _VREDDIT.match(meta.get("media_url") or "")
    if match:
        base = f"https://v.redd.it/{match.group(1)}"
        return {"kind": "reddit_video", "hls": f"{base}/HLSPlaylist.m3u8", "dash": f"{base}/DASHPlaylist.mpd"}
    if meta.get("media_type") in ("image", "gallery"):
        return {"kind": meta["media_type"]}
    return None


def remote_image(url: str) -> str:
    return f"/media/remote?u={quote(url, safe='')}"


def image_info(session: Session, item: Item, media: dict | None) -> dict | None:
    """The picture to show now. Cached pictures come with their size; a picture the worker has not
    cached yet is fetched on first view through /media/item/<id>, so feeds never wait for it."""
    image = session.get(Image, item.image_id) if item.image_id else None
    cached = ({"url": f"/media/{item.image_id}", "width": image.width, "height": image.height}
              if image is not None and image.status == "ok" else None)
    if media and media["kind"] == "youtube" and (cached is None or (cached["width"] or 0) < 480):
        return {"url": remote_image(f"https://i.ytimg.com/vi/{media['id']}/hqdefault.jpg"), "width": 16,
                "height": 9}
    if cached:
        return cached
    if item.image_state == "pending" and (item.meta or {}).get("image_candidates"):
        return {"url": f"/media/item/{item.id}", "width": None, "height": None}
    return None


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
        "domain": item.domain, "author": item.author,
        "excerpt": None if (item.excerpt or "").startswith(BOILERPLATE) else item.excerpt,
        "summary": item.summary, "image_url": None,
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
    media = media_info(item)
    image = image_info(session, item, media)
    if image is not None:
        out["image"], out["image_url"] = image, image["url"]
    if media:
        out["media"] = media
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
        linked_video = media is not None and media["kind"] == "youtube" and item.content_type != "youtube_video"
        out["content"] = None if linked_video and not community_text(item) else item.clean_text
        out["body"] = readable_body(item) if community or item.content_type == "youtube_video" else None
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
