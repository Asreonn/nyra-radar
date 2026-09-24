"""Collector output model (RawItem) and supporting structures.

Collectors produce RawItems; `normalize.py` turns them into Item rows. Keeping this
boundary explicit means collectors never touch the DB and are trivially testable
from fixtures.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

CONTENT_TYPES = (
    "article",
    "reddit_post",
    "hackernews_post",
    "github_repository",
    "github_release",
    "youtube_video",
    "rss_entry",
    "website_change",
    "telegram_post",
)

# Image candidate kinds in priority order (lower index wins).
IMAGE_PRIORITY = (
    "github_social",  # custom social preview uploaded by the maintainers
    "readme_hero",
    "github_card",  # auto-generated opengraph card
    "og",
    "twitter",
    "reddit_preview",
    "youtube_thumb",
    "feed",
    "site_icon",
)


@dataclass
class CommunityInfo:
    platform: str  # reddit | hackernews | telegram
    post_id: str
    discussion_url: str
    community: str | None = None
    score: int = 0
    num_comments: int = 0
    upvote_ratio: float | None = None
    flair: str | None = None
    is_self: bool = False
    post_kind: str | None = None  # story | show | ask | link | self | job


@dataclass
class RepoInfo:
    full_name: str  # owner/name (original case)
    description: str | None = None
    stars: int | None = None
    forks: int | None = None
    language: str | None = None
    topics: list[str] = field(default_factory=list)
    license: str | None = None
    created_at: datetime | None = None
    pushed_at: datetime | None = None
    homepage: str | None = None
    external_stars_delta: dict[str, Any] = field(default_factory=dict)  # {"ossinsight_24h": 812}
    release_tag: str | None = None
    release_published_at: datetime | None = None


@dataclass
class RawItem:
    url: str
    title: str
    content_type: str
    source_item_id: str | None = None
    text: str | None = None
    text_is_html: bool = False
    excerpt: str | None = None
    author: str | None = None
    published_at: datetime | None = None
    link_url: str | None = None
    image_candidates: list[tuple[str, str]] = field(default_factory=list)  # (kind, url)
    external_score: int | None = None
    external_comment_count: int | None = None
    language: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    community: CommunityInfo | None = None
    repo: RepoInfo | None = None
    links: list[str] = field(default_factory=list)

    def add_image(self, kind: str, url: str | None) -> None:
        if url and url.startswith(("http://", "https://")) and (kind, url) not in self.image_candidates:
            self.image_candidates.append((kind, url))
