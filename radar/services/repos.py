"""Repository records and cross-links (shared by ingest, hydration and discovery)."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from radar.db.models import Item, Repository, RepositoryMention
from radar.extraction.canonical_url import url_hash
from radar.normalization.models import RepoInfo
from radar.util import utcnow

PLATFORM_BY_TYPE = {
    "reddit_post": "reddit",
    "hackernews_post": "hackernews",
    "youtube_video": "youtube",
    "telegram_post": "telegram",
    "github_release": "release",
    "github_repository": "github",
}


def get_repository(session: Session, full_name: str) -> Repository | None:
    return session.scalar(select(Repository).where(Repository.full_name == full_name.lower()))


def upsert_repository(session: Session, full_name: str, info: RepoInfo | None = None,
                      via: str | None = None) -> Repository:
    key = full_name.lower().strip("/")
    repo = session.scalar(select(Repository).where(Repository.full_name == key))
    owner, name = full_name.split("/", 1)
    if repo is None:
        repo = Repository(full_name=key, display_name=full_name, owner=owner.lower(), name=name,
                          discovered_via=via, first_seen_at=utcnow(), topics=[], readme_images=[],
                          readme_links=[], external_stars_delta={})
        session.add(repo)
    if info:
        for attr, value in (("description", info.description), ("stars", info.stars), ("forks", info.forks),
                            ("language", info.language), ("license", info.license),
                            ("created_at_gh", info.created_at), ("pushed_at", info.pushed_at),
                            ("homepage", info.homepage)):
            if value not in (None, ""):
                setattr(repo, attr, value)
        if info.topics:
            repo.topics = list(info.topics)
        if info.external_stars_delta:
            merged = dict(repo.external_stars_delta or {})
            for k, v in info.external_stars_delta.items():
                merged[k] = {"value": v, "observed_at": utcnow().isoformat()}
            repo.external_stars_delta = merged
        if info.release_tag and (repo.latest_release_at is None or (
                info.release_published_at and info.release_published_at >= repo.latest_release_at)):
            repo.latest_release_tag = info.release_tag
            repo.latest_release_at = info.release_published_at
    return repo


def ensure_repo_item(session: Session, repo: Repository, via: str) -> Item:
    """Every repository has a github_repository Item so it is browsable and rankable."""
    if repo.item_id:
        item = session.get(Item, repo.item_id)
        if item is not None:
            return item
    canonical = f"https://github.com/{repo.display_name}"
    h = url_hash(canonical)
    item = session.scalar(select(Item).where(Item.url_hash == h))
    if item is None:
        item = Item(canonical_url=canonical, url_hash=h, content_type="github_repository",
                    title=repo.display_name, excerpt=repo.description, clean_text=repo.description,
                    author=repo.owner, published_at=repo.created_at_gh or utcnow(), domain="github.com",
                    source_item_id=repo.full_name, meta={"via": via}, external_score=repo.stars,
                    processing_state="normalized", extraction_state="skipped", image_state="pending",
                    embedding_state="pending", cluster_state="pending")
        session.add(item)
        session.flush()
    repo.item_id = item.id
    item.repository_id = repo.id
    return item


def add_mention(session: Session, repo: Repository, item: Item, relation: str = "mentions") -> None:
    if repo.item_id == item.id:
        return
    platform = PLATFORM_BY_TYPE.get(item.content_type, "article")
    stmt = sqlite_insert(RepositoryMention).values(
        repository_id=repo.id, item_id=item.id, platform=platform, relation=relation, created_at=utcnow()
    ).on_conflict_do_nothing(index_elements=["repository_id", "item_id"])
    session.execute(stmt)
