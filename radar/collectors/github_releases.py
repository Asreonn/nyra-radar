"""GitHub release tracking via the public releases Atom feed (no API budget used)."""
from __future__ import annotations

from radar.collectors.base import Collector, CollectorError, FetchResult, SourceSpec
from radar.collectors.feedutil import entry_html, entry_published, parse_feed
from radar.net import HttpError
from radar.normalization.models import RawItem, RepoInfo
from radar.util import normalize_ws, strip_html


class GitHubReleasesCollector(Collector):
    kind = "github_releases"
    content_type = "github_release"

    async def fetch(self, source: SourceSpec) -> FetchResult:
        full = source.identifier.strip("/")
        headers = {}
        if source.etag:
            headers["If-None-Match"] = source.etag
        try:
            resp = await self.http.get(f"https://github.com/{full}/releases.atom", headers=headers)
        except HttpError as exc:
            raise CollectorError(str(exc), exc.status_code, permanent=exc.status_code == 404) from exc
        if resp.status_code == 304:
            return FetchResult(not_modified=True, status_code=304, etag=source.etag)
        try:
            parsed = parse_feed(resp.content)
        except ValueError as exc:
            raise CollectorError(str(exc), resp.status_code) from exc
        include_pre = bool(source.config.get("include_prereleases", False))
        items = []
        for entry in parsed.entries[: source.limit(5)]:
            title = normalize_ws(entry.get("title", ""))
            link = entry.get("link")
            if not link or not title:
                continue
            tag = link.rstrip("/").rsplit("/", 1)[-1]
            if not include_pre and any(x in tag.lower() for x in ("-rc", "alpha", "beta", "nightly", "-pre")):
                continue
            body = entry_html(entry)
            text = strip_html(body)
            item = RawItem(
                url=link,
                title=f"{full} {title}" if full.split('/')[-1].lower() not in title.lower() else title,
                content_type="github_release",
                source_item_id=entry.get("id") or link,
                text=body or None,
                text_is_html=True,
                excerpt=text[:600] or None,
                author=(entry.get("author") or full.split("/")[0]),
                published_at=entry_published(entry),
                repo=RepoInfo(full_name=full, release_tag=tag, release_published_at=entry_published(entry)),
                meta={"release_tag": tag, "repo": full},
            )
            item.add_image("github_card", f"https://opengraph.githubassets.com/radar/{full}")
            items.append(item)
        return FetchResult(items=items, etag=resp.headers.get("etag"), status_code=resp.status_code)
