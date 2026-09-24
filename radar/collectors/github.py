"""GitHub discovery collectors and the shared GitHub API client.

kinds:
  github_search   - REST search for recently created/pushed repos matching a query
  github_trending - github.com/trending HTML (daily/weekly, optional language)
  ossinsight      - api.ossinsight.io trending repos with stars gained in the period
                    (endpoint and field mapping follow Horizon, MIT - THIRD_PARTY_NOTICES.md)
  github_org      - recent repositories of an organisation/user (added by source discovery)

GitHub releases live in github_releases.py. Repository metadata hydration (GraphQL) is in
radar.services.projects and uses GitHubAPI from here.
"""
from __future__ import annotations

import logging
import re
from datetime import timedelta
from typing import Any

from bs4 import BeautifulSoup

from radar.collectors.base import Collector, CollectorError, FetchResult, SourceSpec
from radar.config import Config
from radar.net import BROWSER_UA, Http, HttpError
from radar.normalization.models import RawItem, RepoInfo
from radar.util import parse_datetime, safe_int, utcnow

log = logging.getLogger(__name__)
API = "https://api.github.com"


class GitHubAPI:
    def __init__(self, http: Http, config: Config):
        self.http = http
        self.config = config
        self._token: str | None = None
        self.remaining: int | None = None
        self.graphql_remaining: int | None = None

    @property
    def token(self) -> str:
        if self._token is None:
            self._token = self.config.secret(self.config.github.token, self.config.github.token_command)
        return self._token

    def headers(self) -> dict[str, str]:
        h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    async def rest(self, path: str, params: dict[str, Any] | None = None, accept: str | None = None,
                   raw: bool = False) -> Any:
        headers = self.headers()
        if accept:
            headers["Accept"] = accept
        try:
            resp = await self.http.get(API + path, headers=headers, params=params)
        except HttpError as exc:
            if exc.status_code in (403, 429):
                raise CollectorError("GitHub rate limit or access denied", exc.status_code) from exc
            raise CollectorError(str(exc), exc.status_code, permanent=exc.status_code == 404) from exc
        self.remaining = safe_int(resp.headers.get("x-ratelimit-remaining"), self.remaining)
        if raw:
            return resp
        return resp.json()

    async def graphql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.token:
            raise CollectorError("GraphQL requires a GitHub token", permanent=True)
        try:
            resp = await self.http.request("POST", API + "/graphql", headers=self.headers(),
                                           json={"query": query, "variables": variables or {}}, retries=1,
                                           timeout=40)
        except HttpError as exc:
            raise CollectorError(str(exc), exc.status_code) from exc
        data = resp.json()
        rate = (data.get("data") or {}).get("rateLimit") or {}
        self.graphql_remaining = rate.get("remaining", self.graphql_remaining)
        if data.get("errors") and not data.get("data"):
            raise CollectorError(str(data["errors"])[:300])
        return data


def repo_raw_item(full_name: str, *, description: str | None = None, stars: int | None = None,
                  forks: int | None = None, language: str | None = None, topics: list[str] | None = None,
                  license_name: str | None = None, created_at: Any = None, pushed_at: Any = None,
                  homepage: str | None = None, external_delta: dict[str, Any] | None = None,
                  via: str = "github") -> RawItem:
    owner, _name = full_name.split("/", 1)
    item = RawItem(
        url=f"https://github.com/{full_name}",
        title=full_name,
        content_type="github_repository",
        source_item_id=full_name.lower(),
        excerpt=(description or "").strip() or None,
        text=(description or "").strip() or None,
        author=owner,
        published_at=parse_datetime(created_at) or None,
        external_score=stars,
        repo=RepoInfo(full_name=full_name, description=description, stars=stars, forks=forks, language=language,
                      topics=list(topics or []), license=license_name, created_at=parse_datetime(created_at),
                      pushed_at=parse_datetime(pushed_at), homepage=homepage,
                      external_stars_delta=dict(external_delta or {})),
        meta={"via": via},
    )
    item.add_image("github_card", f"https://opengraph.githubassets.com/radar/{full_name}")
    return item


def _repo_from_rest(repo: dict[str, Any], via: str) -> RawItem:
    lic = (repo.get("license") or {}).get("spdx_id")
    return repo_raw_item(
        repo["full_name"], description=repo.get("description"), stars=repo.get("stargazers_count"),
        forks=repo.get("forks_count"), language=repo.get("language"), topics=repo.get("topics"),
        license_name=None if lic in (None, "NOASSERTION") else lic, created_at=repo.get("created_at"),
        pushed_at=repo.get("pushed_at"), homepage=repo.get("homepage"), via=via)


class GitHubSearchCollector(Collector):
    kind = "github_search"
    content_type = "github_repository"

    def __init__(self, http: Http, config: Config):
        super().__init__(http, config)
        self.api = GitHubAPI(http, config)

    async def fetch(self, source: SourceSpec) -> FetchResult:
        days = int(source.config.get("created_within_days", 10))
        min_stars = int(source.config.get("min_stars", 25))
        field = source.config.get("date_field", "created")
        since = (utcnow() - timedelta(days=days)).date().isoformat()
        q = f"{source.identifier} {field}:>{since} stars:>={min_stars} archived:false fork:false".strip()
        data = await self.api.rest("/search/repositories", params={
            "q": q, "sort": source.config.get("sort", "stars"), "order": "desc",
            "per_page": source.limit(30)})
        items = [_repo_from_rest(r, via=f"github_search:{source.identifier}") for r in data.get("items", [])]
        return FetchResult(items=items, status_code=200)


class GitHubOrgCollector(Collector):
    kind = "github_org"
    content_type = "github_repository"

    def __init__(self, http: Http, config: Config):
        super().__init__(http, config)
        self.api = GitHubAPI(http, config)

    async def fetch(self, source: SourceSpec) -> FetchResult:
        login = source.identifier
        max_age = int(source.config.get("max_age_days", 60))
        path = f"/orgs/{login}/repos" if source.config.get("type", "org") == "org" else f"/users/{login}/repos"
        try:
            repos = await self.api.rest(path, params={"sort": "pushed", "per_page": 30, "type": "public"})
        except CollectorError as exc:
            if exc.status_code == 404 and path.startswith("/orgs"):
                repos = await self.api.rest(f"/users/{login}/repos", params={"sort": "pushed", "per_page": 30})
            else:
                raise
        cutoff = utcnow() - timedelta(days=max_age)
        items = []
        for repo in repos:
            if repo.get("fork") or repo.get("archived"):
                continue
            pushed = parse_datetime(repo.get("pushed_at"))
            if pushed and pushed < cutoff:
                continue
            items.append(_repo_from_rest(repo, via=f"github_org:{login}"))
        return FetchResult(items=items[: source.limit(20)], status_code=200)


_STARS_TODAY = re.compile(r"([\d,]+)\s+stars?\s+(today|this week|this month)", re.IGNORECASE)


def parse_trending(html: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "lxml")
    rows = []
    for art in soup.select("article.Box-row"):
        link = art.select_one("h2 a")
        if not link or not link.get("href"):
            continue
        full = link["href"].strip("/")
        if full.count("/") != 1:
            continue
        desc_el = art.select_one("p")
        lang_el = art.select_one("[itemprop=programmingLanguage]")
        stars_el = art.select_one("a[href$='/stargazers']")
        forks_el = art.select_one("a[href$='/forks']")
        gained = _STARS_TODAY.search(art.get_text(" ", strip=True))
        rows.append({
            "full_name": full,
            "description": desc_el.get_text(" ", strip=True) if desc_el else None,
            "language": lang_el.get_text(strip=True) if lang_el else None,
            "stars": safe_int(stars_el.get_text(strip=True) if stars_el else None),
            "forks": safe_int(forks_el.get_text(strip=True) if forks_el else None),
            "stars_gained": safe_int(gained.group(1)) if gained else None,
            "period_label": gained.group(2).lower() if gained else None,
        })
    return rows


class GitHubTrendingCollector(Collector):
    kind = "github_trending"
    content_type = "github_repository"

    async def fetch(self, source: SourceSpec) -> FetchResult:
        since = source.identifier if source.identifier in ("daily", "weekly", "monthly") else "daily"
        lang = source.config.get("language", "")
        url = f"https://github.com/trending/{lang}" if lang else "https://github.com/trending"
        try:
            resp = await self.http.get(url, params={"since": since}, headers={"User-Agent": BROWSER_UA})
        except HttpError as exc:
            raise CollectorError(str(exc), exc.status_code) from exc
        items = []
        for row in parse_trending(resp.text)[: source.limit(25)]:
            key = f"github_trending_{since}"
            delta = {key: row["stars_gained"]} if row["stars_gained"] is not None else {}
            items.append(repo_raw_item(row["full_name"], description=row["description"], stars=row["stars"],
                                       forks=row["forks"], language=row["language"], external_delta=delta,
                                       via=f"github_trending:{since}"))
        return FetchResult(items=items, status_code=resp.status_code)


class OSSInsightCollector(Collector):
    kind = "ossinsight"
    content_type = "github_repository"
    URL = "https://api.ossinsight.io/v1/trends/repos/"

    async def fetch(self, source: SourceSpec) -> FetchResult:
        period = source.identifier or "past_24_hours"
        languages = source.config.get("languages") or ["All"]
        items: dict[str, RawItem] = {}
        for lang in languages:
            try:
                resp = await self.http.get(self.URL, params={"period": period, "language": lang},
                                           headers={"Accept": "application/json"})
            except HttpError as exc:
                raise CollectorError(str(exc), exc.status_code) from exc
            rows = ((resp.json().get("data") or {}).get("rows")) or []
            for row in rows:
                name = row.get("repo_name")
                if not name or "/" not in name or name.lower() in items:
                    continue
                gained = safe_int(row.get("stars"))
                items[name.lower()] = repo_raw_item(
                    name, description=row.get("description"), language=row.get("primary_language"),
                    external_delta={f"ossinsight_{period}": gained} if gained is not None else {},
                    via=f"ossinsight:{period}")
                items[name.lower()].meta["ossinsight"] = {
                    "stars_gained": gained, "forks_gained": safe_int(row.get("forks")),
                    "pushes": safe_int(row.get("pushes")), "pull_requests": safe_int(row.get("pull_requests")),
                    "collections": row.get("collection_names")}
        ranked = sorted(items.values(),
                        key=lambda r: -(next(iter(r.repo.external_stars_delta.values()), 0) or 0) if r.repo else 0)
        return FetchResult(items=ranked[: source.limit(40)], status_code=200)
