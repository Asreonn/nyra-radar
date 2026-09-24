"""Normalized Radar data model (SQLite is the source of truth).

JSON columns hold only flexible metadata. All state that the pipeline, ranker or UI
branches on is a real column.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from radar.util import utcnow


class Base(DeclarativeBase):
    type_annotation_map: ClassVar = {dict[str, Any]: JSON, list[Any]: JSON}


# ---------------------------------------------------------------- sources
class Source(Base):
    __tablename__ = "sources"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)  # rss, reddit, hackernews, github_search, ...
    name: Mapped[str] = mapped_column(String(200))
    identifier: Mapped[str] = mapped_column(String(500))  # feed URL, subreddit, query, repo, ...
    url: Mapped[str | None] = mapped_column(String(1000))  # human-facing homepage
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # active | trial | muted | disabled | rejected  (promoted sources are `active` with origin=discovered)
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    origin: Mapped[str] = mapped_column(String(16), default="seed")  # seed | manual | discovered
    category: Mapped[str | None] = mapped_column(String(64))
    weight: Mapped[float] = mapped_column(Float, default=1.0)  # manual weight multiplier
    quality: Mapped[float] = mapped_column(Float, default=0.6)  # learned 0..1
    novelty_contribution: Mapped[float] = mapped_column(Float, default=0.0)
    duplicate_rate: Mapped[float] = mapped_column(Float, default=0.0)
    interval_minutes: Mapped[int | None] = mapped_column(Integer)  # override of the per-kind default
    last_fetch_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime)
    next_fetch_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    total_fetches: Mapped[int] = mapped_column(Integer, default=0)
    successful_fetches: Mapped[int] = mapped_column(Integer, default=0)
    item_count: Mapped[int] = mapped_column(Integer, default=0)
    duplicate_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    etag: Mapped[str | None] = mapped_column(String(300))
    last_modified: Mapped[str | None] = mapped_column(String(100))
    state: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # collector cursor / website hash
    muted_until: Mapped[datetime | None] = mapped_column(DateTime)
    trial_started_at: Mapped[datetime | None] = mapped_column(DateTime)
    decision_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    __table_args__ = (UniqueConstraint("kind", "identifier", name="uq_source_kind_identifier"),)


class FetchLog(Base):
    """One row per collector run (health, success rate, observability)."""
    __tablename__ = "fetch_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    status_code: Mapped[int | None] = mapped_column(Integer)
    items_seen: Mapped[int] = mapped_column(Integer, default=0)
    items_new: Mapped[int] = mapped_column(Integer, default=0)
    items_updated: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)


# ---------------------------------------------------------------- items
class Item(Base):
    """Shared base representation of every collected thing."""
    __tablename__ = "items"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    canonical_url: Mapped[str] = mapped_column(String(1500))
    url_hash: Mapped[str] = mapped_column(String(40), unique=True)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id", ondelete="SET NULL"), index=True)
    source_item_id: Mapped[str | None] = mapped_column(String(300))
    content_type: Mapped[str] = mapped_column(String(32), index=True)
    title: Mapped[str] = mapped_column(Text)
    raw_text: Mapped[str | None] = mapped_column(Text)
    clean_text: Mapped[str | None] = mapped_column(Text)
    excerpt: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)  # LLM summary once enriched
    author: Mapped[str | None] = mapped_column(String(300))
    published_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    language: Mapped[str | None] = mapped_column(String(12))
    domain: Mapped[str | None] = mapped_column(String(255), index=True)
    link_url: Mapped[str | None] = mapped_column(String(1500))  # target of a link post (canonical)
    link_hash: Mapped[str | None] = mapped_column(String(40), index=True)
    image_url: Mapped[str | None] = mapped_column(String(1500))
    image_id: Mapped[int | None] = mapped_column(ForeignKey("images.id", ondelete="SET NULL"))
    image_cache_path: Mapped[str | None] = mapped_column(String(500))
    external_score: Mapped[int | None] = mapped_column(Integer)
    external_comment_count: Mapped[int | None] = mapped_column(Integer)
    meta: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    # dedup / fingerprints
    title_hash: Mapped[str | None] = mapped_column(String(40), index=True)
    content_hash: Mapped[str | None] = mapped_column(String(40))
    simhash: Mapped[int | None] = mapped_column(BigInteger)
    duplicate_of: Mapped[int | None] = mapped_column(ForeignKey("items.id", ondelete="SET NULL"), index=True)
    dup_reason: Mapped[str | None] = mapped_column(String(32))
    dup_similarity: Mapped[float | None] = mapped_column(Float)
    cluster_id: Mapped[int | None] = mapped_column(ForeignKey("story_clusters.id", ondelete="SET NULL"),
                                                   index=True)
    repository_id: Mapped[int | None] = mapped_column(ForeignKey("repositories.id", ondelete="SET NULL"),
                                                      index=True)
    # explicit per-stage state machine (all retryable)
    processing_state: Mapped[str] = mapped_column(String(16), default="new", index=True)
    extraction_state: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    image_state: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    embedding_state: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    cluster_state: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    summary_state: Mapped[str] = mapped_column(String(16), default="none", index=True)
    comments_state: Mapped[str] = mapped_column(String(16), default="none")
    stage_attempts: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # denormalised user state for fast browsing filters (source of truth: user_events)
    user_vote: Mapped[int] = mapped_column(SmallInteger, default=0)  # -1 / 0 / 1
    is_read: Mapped[bool] = mapped_column(Boolean, default=False)
    is_hidden: Mapped[bool] = mapped_column(Boolean, default=False)
    impressions: Mapped[int] = mapped_column(Integer, default=0)
    last_opened_at: Mapped[datetime | None] = mapped_column(DateTime)

    source: Mapped[Source | None] = relationship(lazy="joined")
    score: Mapped[ItemScore | None] = relationship(back_populates="item", uselist=False, lazy="select")

    __table_args__ = (
        Index("ix_items_source_item", "source_id", "source_item_id"),
        Index("ix_items_type_published", "content_type", "published_at"),
    )


class ItemSighting(Base):
    """Every raw observation of an item (raw URLs and multi-source coverage are never lost)."""
    __tablename__ = "item_sightings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), index=True)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id", ondelete="SET NULL"), index=True)
    source_item_id: Mapped[str | None] = mapped_column(String(300))
    raw_url: Mapped[str] = mapped_column(String(2000))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    seen_count: Mapped[int] = mapped_column(Integer, default=1)
    __table_args__ = (UniqueConstraint("item_id", "source_id", "raw_url", name="uq_sighting"),)


class ItemLink(Base):
    """Outbound links found in an item (citations, repos, domains) - feeds discovery."""
    __tablename__ = "item_links"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), index=True)
    url: Mapped[str] = mapped_column(String(1500))
    url_hash: Mapped[str] = mapped_column(String(40), index=True)
    domain: Mapped[str | None] = mapped_column(String(255), index=True)
    kind: Mapped[str] = mapped_column(String(24), default="link")  # link | github_repo | subreddit | youtube | feed
    __table_args__ = (UniqueConstraint("item_id", "url_hash", name="uq_item_link"),)


class ItemScore(Base):
    """Latest ranking components for an item. Explanations derive only from these."""
    __tablename__ = "item_scores"
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), primary_key=True)
    computed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    cheap_score: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    final_score: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    importance: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    novelty: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    interest: Mapped[float] = mapped_column(Float, default=0.0)
    community: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    momentum: Mapped[float] = mapped_column(Float, default=0.0)
    spam: Mapped[float] = mapped_column(Float, default=0.0)
    is_candidate: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    components: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # facts behind components
    item: Mapped[Item] = relationship(back_populates="score")


class Embedding(Base):
    __tablename__ = "embeddings"
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), primary_key=True)
    model: Mapped[str] = mapped_column(String(100))
    dim: Mapped[int] = mapped_column(Integer)
    vector: Mapped[bytes] = mapped_column(LargeBinary)  # float32, L2-normalised
    input_hash: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Article(Base):
    __tablename__ = "articles"
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), primary_key=True)
    site_name: Mapped[str | None] = mapped_column(String(200))
    raw_extraction: Mapped[str | None] = mapped_column(Text)  # extractor output before cleanup
    extraction_method: Mapped[str | None] = mapped_column(String(32))
    word_count: Mapped[int | None] = mapped_column(Integer)
    reading_minutes: Mapped[float | None] = mapped_column(Float)
    page_canonical_url: Mapped[str | None] = mapped_column(String(1500))
    og: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    extracted_at: Mapped[datetime | None] = mapped_column(DateTime)
    http_status: Mapped[int | None] = mapped_column(Integer)


class Enrichment(Base):
    """Structured LLM output per item and kind (article | repository | community | story)."""
    __tablename__ = "enrichments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_id: Mapped[int | None] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(24))
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    model: Mapped[str | None] = mapped_column(String(120))
    input_hash: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    __table_args__ = (UniqueConstraint("item_id", "kind", name="uq_enrichment_item_kind"),)


# ---------------------------------------------------------------- community
class CommunityPost(Base):
    __tablename__ = "community_posts"
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), primary_key=True)
    platform: Mapped[str] = mapped_column(String(16), index=True)  # reddit | hackernews | telegram
    community: Mapped[str | None] = mapped_column(String(120), index=True)  # subreddit / "hn"
    post_id: Mapped[str] = mapped_column(String(64))
    score: Mapped[int] = mapped_column(Integer, default=0)
    num_comments: Mapped[int] = mapped_column(Integer, default=0)
    upvote_ratio: Mapped[float | None] = mapped_column(Float)
    discussion_url: Mapped[str] = mapped_column(String(1000))
    linked_url: Mapped[str | None] = mapped_column(String(1500))
    flair: Mapped[str | None] = mapped_column(String(120))
    is_self: Mapped[bool] = mapped_column(Boolean, default=False)
    post_kind: Mapped[str | None] = mapped_column(String(24))  # story | show | ask | link | self
    score_history: Mapped[list[Any]] = mapped_column(JSON, default=list)  # [[iso, score, comments], ...]
    __table_args__ = (UniqueConstraint("platform", "post_id", name="uq_community_post"),)


class CommentSummary(Base):
    __tablename__ = "comment_summaries"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), unique=True)
    platform: Mapped[str] = mapped_column(String(16))
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    sampled: Mapped[int] = mapped_column(Integer, default=0)
    comments: Mapped[list[Any]] = mapped_column(JSON, default=list)  # [{author, score, text, depth}]
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    state: Mapped[str] = mapped_column(String(16), default="fetched")  # fetched | summarized | failed
    input_hash: Mapped[str | None] = mapped_column(String(40))


# ---------------------------------------------------------------- stories
class StoryCluster(Base):
    __tablename__ = "story_clusters"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(Text)
    representative_item_id: Mapped[int | None] = mapped_column(Integer, index=True)
    item_count: Mapped[int] = mapped_column(Integer, default=1)
    source_count: Mapped[int] = mapped_column(Integer, default=1)
    platforms: Mapped[list[Any]] = mapped_column(JSON, default=list)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    centroid: Mapped[bytes | None] = mapped_column(LargeBinary)
    summary: Mapped[str | None] = mapped_column(Text)
    differences: Mapped[list[Any]] = mapped_column(JSON, default=list)
    summary_state: Mapped[str] = mapped_column(String(16), default="none")
    summary_input_hash: Mapped[str | None] = mapped_column(String(40))
    score: Mapped[float] = mapped_column(Float, default=0.0)


# ---------------------------------------------------------------- repositories
class Repository(Base):
    __tablename__ = "repositories"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    full_name: Mapped[str] = mapped_column(String(200), unique=True)  # lower-case owner/name
    display_name: Mapped[str] = mapped_column(String(200))
    owner: Mapped[str] = mapped_column(String(100), index=True)
    name: Mapped[str] = mapped_column(String(100))
    item_id: Mapped[int | None] = mapped_column(Integer, index=True)
    description: Mapped[str | None] = mapped_column(Text)
    homepage: Mapped[str | None] = mapped_column(String(500))
    stars: Mapped[int | None] = mapped_column(Integer)
    forks: Mapped[int | None] = mapped_column(Integer)
    watchers: Mapped[int | None] = mapped_column(Integer)
    open_issues: Mapped[int | None] = mapped_column(Integer)
    language: Mapped[str | None] = mapped_column(String(64), index=True)
    license: Mapped[str | None] = mapped_column(String(64))
    topics: Mapped[list[Any]] = mapped_column(JSON, default=list)
    created_at_gh: Mapped[datetime | None] = mapped_column(DateTime)
    pushed_at: Mapped[datetime | None] = mapped_column(DateTime)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    is_fork: Mapped[bool] = mapped_column(Boolean, default=False)
    default_branch: Mapped[str | None] = mapped_column(String(100))
    latest_release_tag: Mapped[str | None] = mapped_column(String(120))
    latest_release_at: Mapped[datetime | None] = mapped_column(DateTime)
    releases_90d: Mapped[int | None] = mapped_column(Integer)
    release_interval_days: Mapped[float | None] = mapped_column(Float)
    contributors: Mapped[int | None] = mapped_column(Integer)
    commits_30d: Mapped[int | None] = mapped_column(Integer)
    social_image_url: Mapped[str | None] = mapped_column(String(1000))
    uses_custom_social_image: Mapped[bool] = mapped_column(Boolean, default=False)
    readme_excerpt: Mapped[str | None] = mapped_column(Text)
    readme_hash: Mapped[str | None] = mapped_column(String(40))
    readme_images: Mapped[list[Any]] = mapped_column(JSON, default=list)
    readme_links: Mapped[list[Any]] = mapped_column(JSON, default=list)
    # derived momentum (recomputed from snapshots/mentions; null = unknown, never fabricated)
    stars_delta_24h: Mapped[int | None] = mapped_column(Integer)
    stars_delta_7d: Mapped[int | None] = mapped_column(Integer)
    external_stars_delta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # e.g. ossinsight
    activity_delta: Mapped[float | None] = mapped_column(Float)
    mentions_7d: Mapped[int] = mapped_column(Integer, default=0)
    mention_velocity: Mapped[float | None] = mapped_column(Float)
    momentum: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    discovered_via: Mapped[str | None] = mapped_column(String(64))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    last_hydrated_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    hydrate_state: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    hydrate_error: Mapped[str | None] = mapped_column(Text)
    watch_releases: Mapped[bool] = mapped_column(Boolean, default=False)


class RepositorySnapshot(Base):
    """Append-only historical observations used to compute growth locally."""
    __tablename__ = "repository_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    repository_id: Mapped[int] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"))
    observed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    stars: Mapped[int | None] = mapped_column(Integer)
    forks: Mapped[int | None] = mapped_column(Integer)
    watchers: Mapped[int | None] = mapped_column(Integer)
    open_issues: Mapped[int | None] = mapped_column(Integer)
    commits_30d: Mapped[int | None] = mapped_column(Integer)
    contributors: Mapped[int | None] = mapped_column(Integer)
    latest_release_tag: Mapped[str | None] = mapped_column(String(120))
    pushed_at: Mapped[datetime | None] = mapped_column(DateTime)
    source: Mapped[str] = mapped_column(String(24), default="github")
    __table_args__ = (Index("ix_repo_snap_repo_time", "repository_id", "observed_at"),)


class RepositoryMention(Base):
    """Links a repository to an item that mentions it (Reddit thread, HN post, article, release)."""
    __tablename__ = "repository_mentions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    repository_id: Mapped[int] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"), index=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), index=True)
    platform: Mapped[str] = mapped_column(String(24))  # reddit | hackernews | article | release | youtube ...
    relation: Mapped[str] = mapped_column(String(24), default="mentions")  # mentions | links_to | release_of
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    __table_args__ = (UniqueConstraint("repository_id", "item_id", name="uq_repo_mention"),)


# ---------------------------------------------------------------- topics / interests
class Topic(Base):
    __tablename__ = "topics"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(80), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str | None] = mapped_column(Text)
    aliases: Mapped[list[Any]] = mapped_column(JSON, default=list)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("topics.id", ondelete="SET NULL"))
    kind: Mapped[str] = mapped_column(String(16), default="seed")  # seed | extracted | manual
    embedding: Mapped[bytes | None] = mapped_column(LargeBinary)
    embedding_model: Mapped[str | None] = mapped_column(String(100))
    item_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class TopicRelation(Base):
    __tablename__ = "topic_relations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    topic_a: Mapped[int] = mapped_column(ForeignKey("topics.id", ondelete="CASCADE"), index=True)
    topic_b: Mapped[int] = mapped_column(ForeignKey("topics.id", ondelete="CASCADE"), index=True)
    weight: Mapped[float] = mapped_column(Float, default=0.5)
    kind: Mapped[str] = mapped_column(String(16), default="related")  # related | parent | cooccur
    __table_args__ = (UniqueConstraint("topic_a", "topic_b", name="uq_topic_relation"),)


class ItemTopic(Base):
    __tablename__ = "item_topics"
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), primary_key=True)
    topic_id: Mapped[int] = mapped_column(ForeignKey("topics.id", ondelete="CASCADE"), primary_key=True,
                                          index=True)
    weight: Mapped[float] = mapped_column(Float, default=0.5)
    method: Mapped[str] = mapped_column(String(16), default="embedding")  # embedding | keyword | llm


class Interest(Base):
    __tablename__ = "interests"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    topic_id: Mapped[int] = mapped_column(ForeignKey("topics.id", ondelete="CASCADE"), unique=True)
    weight: Mapped[float] = mapped_column(Float, default=0.0)  # effective weight in [-1, 1]
    seed_weight: Mapped[float] = mapped_column(Float, default=0.0)
    learned: Mapped[float] = mapped_column(Float, default=0.0)  # decayed evidence sum
    inferred: Mapped[float] = mapped_column(Float, default=0.0)  # propagated from related topics (capped)
    manual_adjust: Mapped[float] = mapped_column(Float, default=0.0)
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    muted: Mapped[bool] = mapped_column(Boolean, default=False)
    state: Mapped[str] = mapped_column(String(16), default="seed")  # seed | learned | inferred | manual
    positive_evidence: Mapped[int] = mapped_column(Integer, default=0)
    negative_evidence: Mapped[int] = mapped_column(Integer, default=0)
    last_evidence_at: Mapped[datetime | None] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    topic: Mapped[Topic] = relationship(lazy="joined")


class SourcePreference(Base):
    __tablename__ = "source_preferences"
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"), primary_key=True)
    opens: Mapped[float] = mapped_column(Float, default=0.0)
    saves: Mapped[float] = mapped_column(Float, default=0.0)
    likes: Mapped[float] = mapped_column(Float, default=0.0)
    dislikes: Mapped[float] = mapped_column(Float, default=0.0)
    dismisses: Mapped[float] = mapped_column(Float, default=0.0)
    affinity: Mapped[float] = mapped_column(Float, default=1.0)
    decayed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class UserEvent(Base):
    __tablename__ = "user_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user: Mapped[str] = mapped_column(String(64), default="local")
    item_id: Mapped[int | None] = mapped_column(ForeignKey("items.id", ondelete="SET NULL"), index=True)
    event_type: Mapped[str] = mapped_column(String(24), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    session_id: Mapped[str | None] = mapped_column(String(64))
    context: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # view, position, digest_id, client
    source_id: Mapped[int | None] = mapped_column(Integer)
    content_type: Mapped[str | None] = mapped_column(String(32))
    topics: Mapped[list[Any]] = mapped_column(JSON, default=list)  # [[topic_id, weight], ...] at event time
    weight: Mapped[float] = mapped_column(Float, default=0.0)
    applied: Mapped[bool] = mapped_column(Boolean, default=False, index=True)  # folded into interests


class Collection(Base):
    __tablename__ = "collections"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SavedItem(Base):
    __tablename__ = "saved_items"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), unique=True)
    collection_id: Mapped[int | None] = mapped_column(ForeignKey("collections.id", ondelete="SET NULL"))
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


# ---------------------------------------------------------------- digests
class DigestSession(Base):
    __tablename__ = "digest_sessions"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)  # stable, e.g. dg_20260925_0712_ab12
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    period: Mapped[str] = mapped_column(String(16), default="24h")
    mode: Mapped[str] = mapped_column(String(16), default="mixed")
    requested_by: Mapped[str] = mapped_column(String(24), default="ui")
    params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    sections: Mapped[list[Any]] = mapped_column(JSON, default=list)  # [{key, title, item_ids}]


class DigestItem(Base):
    __tablename__ = "digest_items"
    digest_id: Mapped[str] = mapped_column(ForeignKey("digest_sessions.id", ondelete="CASCADE"), primary_key=True)
    position: Mapped[int] = mapped_column(Integer, primary_key=True)  # 1-based, stable for "the second one"
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), index=True)
    section: Mapped[str] = mapped_column(String(24))
    score: Mapped[float] = mapped_column(Float, default=0.0)
    reason: Mapped[str | None] = mapped_column(Text)


# ---------------------------------------------------------------- discovery
class DiscoveryCandidate(Base):
    __tablename__ = "discovery_candidates"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))  # same vocabulary as Source.kind
    identifier: Mapped[str] = mapped_column(String(500))
    name: Mapped[str] = mapped_column(String(200))
    url: Mapped[str | None] = mapped_column(String(1000))
    status: Mapped[str] = mapped_column(String(16), default="candidate", index=True)  # candidate|trial|promoted|rejected
    method: Mapped[str] = mapped_column(String(32))  # known_pattern | link_mining | llm | exact_match | ...
    topic: Mapped[str | None] = mapped_column(String(120))
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    evidence: Mapped[list[Any]] = mapped_column(JSON, default=list)
    evidence_count: Mapped[int] = mapped_column(Integer, default=1)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    trial_started_at: Mapped[datetime | None] = mapped_column(DateTime)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime)
    decision_reason: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (UniqueConstraint("kind", "identifier", name="uq_candidate_kind_identifier"),)


class DiscoveryAudit(Base):
    __tablename__ = "discovery_audit"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    candidate_id: Mapped[int] = mapped_column(ForeignKey("discovery_candidates.id", ondelete="CASCADE"),
                                              index=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    action: Mapped[str] = mapped_column(String(24))  # discovered | evidence | trial | promoted | rejected | manual
    reason: Mapped[str] = mapped_column(Text)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


# ---------------------------------------------------------------- images
class Image(Base):
    __tablename__ = "images"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    original_url: Mapped[str] = mapped_column(String(1500))
    url_hash: Mapped[str] = mapped_column(String(40), unique=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    mime: Mapped[str | None] = mapped_column(String(64))
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    bytes: Mapped[int | None] = mapped_column(Integer)
    cache_path: Mapped[str | None] = mapped_column(String(500))
    kind: Mapped[str | None] = mapped_column(String(32))  # github_social | readme | og | twitter | ...
    status: Mapped[str] = mapped_column(String(16), default="pending")  # ok | failed | rejected
    etag: Mapped[str | None] = mapped_column(String(300))
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime)
    fail_count: Mapped[int] = mapped_column(Integer, default=0)
    last_used_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# ---------------------------------------------------------------- operations
class ProcessingJob(Base):
    __tablename__ = "processing_jobs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)  # enrich_article | enrich_repo | ...
    target_type: Mapped[str | None] = mapped_column(String(24))
    target_id: Mapped[int | None] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    priority: Mapped[float] = mapped_column(Float, default=0.0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime)
    input_hash: Mapped[str | None] = mapped_column(String(40))
    error: Mapped[str | None] = mapped_column(Text)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    requested_by: Mapped[str] = mapped_column(String(24), default="pipeline")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    __table_args__ = (
        Index("ix_jobs_queue", "state", "priority"),
        UniqueConstraint("kind", "target_id", "input_hash", name="uq_job_identity"),
    )


class ModelUsage(Base):
    __tablename__ = "model_usage"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    provider: Mapped[str] = mapped_column(String(24))
    model: Mapped[str] = mapped_column(String(120))
    purpose: Mapped[str] = mapped_column(String(32))
    job_id: Mapped[int | None] = mapped_column(Integer)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    error: Mapped[str | None] = mapped_column(Text)


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class KV(Base):
    """Small operational state: scheduler last-runs, worker heartbeat, preference vectors."""
    __tablename__ = "kv"
    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON, nullable=True)
    blob: Mapped[bytes | None] = mapped_column(LargeBinary)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
