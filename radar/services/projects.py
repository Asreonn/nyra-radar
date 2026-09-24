"""GitHub repository hydration and locally measured momentum."""
from __future__ import annotations

import base64
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from radar.collectors.github import GitHubAPI
from radar.db.models import Repository, RepositoryMention, RepositorySnapshot
from radar.util import parse_datetime, utcnow


def _snapshot_before(session: Session, repo_id: int, when) -> RepositorySnapshot | None:
    return session.scalar(select(RepositorySnapshot).where(
        RepositorySnapshot.repository_id == repo_id,
        RepositorySnapshot.observed_at <= when,
    ).order_by(RepositorySnapshot.observed_at.desc()).limit(1))


def update_momentum(session: Session, repo: Repository) -> None:
    now = utcnow()
    day = _snapshot_before(session, repo.id, now - timedelta(hours=23))
    week = _snapshot_before(session, repo.id, now - timedelta(days=6, hours=12))
    repo.stars_delta_24h = max(0, repo.stars - day.stars) if day and day.stars is not None and repo.stars else None
    repo.stars_delta_7d = max(0, repo.stars - week.stars) if week and week.stars is not None and repo.stars else None
    repo.mentions_7d = session.scalar(select(func.count(RepositoryMention.id)).where(
        RepositoryMention.repository_id == repo.id,
        RepositoryMention.created_at >= now - timedelta(days=7))) or 0
    growth = repo.stars_delta_7d or repo.stars_delta_24h or 0
    repo.momentum = min(1.0, growth / 250 + repo.mentions_7d / 10)


async def hydrate_repository(session: Session, repo: Repository, api: GitHubAPI) -> bool:
    try:
        data = await api.rest(f"/repos/{repo.display_name}")
        readme = None
        try:
            rd = await api.rest(f"/repos/{repo.display_name}/readme")
            readme = base64.b64decode(rd.get("content", "")).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001, S110 - README is optional; repository metadata remains useful
            pass
        repo.description = data.get("description")
        repo.homepage = data.get("homepage")
        repo.stars = data.get("stargazers_count")
        repo.forks = data.get("forks_count")
        repo.watchers = data.get("subscribers_count")
        repo.open_issues = data.get("open_issues_count")
        repo.language = data.get("language")
        repo.license = (data.get("license") or {}).get("spdx_id")
        repo.topics = data.get("topics") or []
        repo.created_at_gh = parse_datetime(data.get("created_at"))
        repo.pushed_at = parse_datetime(data.get("pushed_at"))
        repo.archived = bool(data.get("archived"))
        repo.is_fork = bool(data.get("fork"))
        repo.default_branch = data.get("default_branch")
        repo.social_image_url = data.get("open_graph_image_url") or f"https://opengraph.githubassets.com/radar/{repo.display_name}"
        repo.readme_excerpt = (readme or "")[:12000] or None
        repo.last_hydrated_at = utcnow()
        repo.hydrate_state = "done"
        repo.hydrate_error = None
        session.add(RepositorySnapshot(repository_id=repo.id, stars=repo.stars, forks=repo.forks,
                                       watchers=repo.watchers, open_issues=repo.open_issues,
                                       latest_release_tag=repo.latest_release_tag, pushed_at=repo.pushed_at))
        update_momentum(session, repo)
        return True
    except Exception as exc:  # noqa: BLE001 - worker boundary stores failure state for retry
        repo.hydrate_state = "failed"
        repo.hydrate_error = str(exc)[:1000]
        return False


async def hydrate_due(session: Session, api: GitHubAPI, limit: int = 8) -> dict[str, int]:
    cutoff = utcnow() - timedelta(hours=24)
    repos = session.scalars(select(Repository).where(
        Repository.hydrate_state.in_(("pending", "failed")) |
        (Repository.last_hydrated_at.is_(None)) |
        (Repository.last_hydrated_at < cutoff)
    ).order_by(Repository.momentum.desc(), Repository.first_seen_at.desc()).limit(limit)).all()
    ok = 0
    for repo in repos:
        ok += int(await hydrate_repository(session, repo, api))
        session.commit()
    return {"attempted": len(repos), "ok": ok, "failed": len(repos) - ok}
