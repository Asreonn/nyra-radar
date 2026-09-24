"""Reddit collector.

Access order: OAuth (oauth.reddit.com) when a client id/secret is configured, then
old.reddit.com JSON (works unauthenticated from this machine), then the subreddit RSS
feed. Per-host pacing lives in radar.net. The old.reddit fallback idea comes from
Horizon (MIT); see THIRD_PARTY_NOTICES.md.
"""
from __future__ import annotations

import base64
import html
import time
from typing import Any

from radar.collectors.base import Collector, CollectorError, FetchResult, SourceSpec
from radar.collectors.feedutil import entry_to_raw, parse_feed
from radar.config import Config
from radar.extraction.canonical_url import extract_urls
from radar.net import BROWSER_UA, Http, HttpError
from radar.normalization.models import CommunityInfo, RawItem
from radar.util import parse_datetime, strip_html

OLD = "https://old.reddit.com"
OAUTH = "https://oauth.reddit.com"
MEDIA_HOSTS = ("i.redd.it", "v.redd.it", "preview.redd.it", "i.imgur.com", "reddit.com/gallery")


class RedditAuth:
    """App-only OAuth token cache (client_credentials grant)."""

    def __init__(self, http: Http, config: Config):
        self.http = http
        self.client_id = config.reddit.client_id
        self.secret = config.secret(config.reddit.client_secret, config.reddit.client_secret_command)
        self.token: str | None = None
        self.expires = 0.0

    @property
    def enabled(self) -> bool:
        return bool(self.client_id and self.secret)

    async def headers(self) -> dict[str, str] | None:
        if not self.enabled:
            return None
        if not self.token or time.time() > self.expires - 60:
            basic = base64.b64encode(f"{self.client_id}:{self.secret}".encode()).decode()
            resp = await self.http.request(
                "POST", "https://www.reddit.com/api/v1/access_token",
                headers={"Authorization": f"Basic {basic}", "User-Agent": "linux:nyra-radar:0.1"},
                data={"grant_type": "client_credentials"})
            data = resp.json()
            self.token = data["access_token"]
            self.expires = time.time() + float(data.get("expires_in", 3600))
        return {"Authorization": f"Bearer {self.token}", "User-Agent": "linux:nyra-radar:0.1"}


async def reddit_json(http: Http, auth: RedditAuth | None, path: str, params: dict[str, Any]) -> Any:
    """GET a Reddit listing path (e.g. '/r/rust/hot') as JSON via OAuth or old.reddit."""
    params = {**params, "raw_json": 1}
    if auth and auth.enabled:
        headers = await auth.headers()
        return (await http.get(OAUTH + path, headers=headers, params=params)).json()
    return (await http.get(OLD + path.rstrip("/") + ".json", headers={"User-Agent": BROWSER_UA},
                           params=params)).json()


def post_to_raw(post: dict[str, Any]) -> RawItem | None:
    title = html.unescape((post.get("title") or "").strip())
    permalink = post.get("permalink") or ""
    if not title or not permalink:
        return None
    discussion = "https://www.reddit.com" + permalink
    is_self = bool(post.get("is_self"))
    target = post.get("url_overridden_by_dest") or post.get("url") or ""
    is_media = any(h in target for h in MEDIA_HOSTS)
    link_url = None if (is_self or not target or is_media or "reddit.com/r/" in target) else target
    selftext = post.get("selftext") or ""
    sub = post.get("subreddit") or ""
    item = RawItem(
        url=discussion,
        title=title,
        content_type="reddit_post",
        source_item_id=post.get("name") or post.get("id"),
        text=selftext or None,
        excerpt=strip_html(selftext)[:600] or None,
        author=post.get("author"),
        published_at=parse_datetime(post.get("created_utc")),
        link_url=link_url,
        external_score=int(post.get("score") or 0),
        external_comment_count=int(post.get("num_comments") or 0),
        community=CommunityInfo(
            platform="reddit", post_id=post.get("id") or "", discussion_url=discussion, community=sub,
            score=int(post.get("score") or 0), num_comments=int(post.get("num_comments") or 0),
            upvote_ratio=post.get("upvote_ratio"), flair=post.get("link_flair_text"), is_self=is_self,
            post_kind="self" if is_self else ("media" if is_media else "link")),
        meta={"subreddit": sub, "domain": post.get("domain"), "stickied": bool(post.get("stickied")),
              "crosspost_from": [c.get("subreddit") for c in post.get("crosspost_parent_list") or []
                                 if c.get("subreddit")]},
    )
    preview = (post.get("preview") or {}).get("images") or []
    if preview:
        src = preview[0].get("source", {}).get("url")
        item.add_image("reddit_preview", html.unescape(src) if src else None)
    if is_media and target.startswith("https://i.redd.it"):
        item.add_image("reddit_preview", target)
    thumb = post.get("thumbnail")
    if thumb and thumb.startswith("http"):
        item.add_image("reddit_preview", thumb)
    if link_url:
        item.links.append(link_url)
    item.links += extract_urls(selftext, 30)
    return item


class RedditCollector(Collector):
    kind = "reddit"
    content_type = "reddit_post"

    def __init__(self, http: Http, config: Config):
        super().__init__(http, config)
        self.auth = RedditAuth(http, config)

    async def fetch(self, source: SourceSpec) -> FetchResult:
        sub = source.identifier.removeprefix("r/").strip("/")
        sorts = source.config.get("sorts") or [source.config.get("sort", "hot")]
        limit = source.limit(25)
        min_score = int(source.config.get("min_score", 3))
        items: dict[str, RawItem] = {}
        errors: list[str] = []
        for sort in sorts:
            params: dict[str, Any] = {"limit": min(100, limit + 5)}
            if sort in ("top", "controversial"):
                params["t"] = source.config.get("t", "day")
            try:
                data = await reddit_json(self.http, self.auth, f"/r/{sub}/{sort}", params)
                children = (data.get("data") or {}).get("children") or []
                for child in children:
                    post = child.get("data") or {}
                    if post.get("stickied") or post.get("over_18"):
                        continue
                    if int(post.get("score") or 0) < min_score and sort != "new":
                        continue
                    raw = post_to_raw(post)
                    if raw and raw.source_item_id not in items:
                        raw.meta["reddit_sort"] = sort
                        items[raw.source_item_id or raw.url] = raw
            except (HttpError, ValueError, KeyError) as exc:
                errors.append(f"{sort}: {exc}")
                code = getattr(exc, "status_code", None)
                if code == 404:
                    raise CollectorError(f"r/{sub} not found", 404, permanent=True) from exc
                # Reddit often redirects anonymous JSON requests to an HTML login page
                # (a JSON ValueError, not a 403). RSS remains the credential-free fallback.
                if not items:
                    rss = await self._rss_fallback(sub, sort, limit)
                    for raw in rss:
                        items.setdefault(raw.source_item_id or raw.url, raw)
        if not items and errors:
            raise CollectorError("; ".join(errors)[:500])
        return FetchResult(items=list(items.values())[: limit * len(sorts)], status_code=200)

    async def _rss_fallback(self, sub: str, sort: str, limit: int) -> list[RawItem]:
        try:
            resp = await self.http.get(f"{OLD}/r/{sub}/{sort}/.rss", headers={"User-Agent": BROWSER_UA},
                                       retries=0)
            parsed = parse_feed(resp.content)
        except (HttpError, ValueError):
            return []
        out = []
        for entry in parsed.entries[:limit]:
            raw = entry_to_raw(entry, "reddit_post")
            if raw:
                post_id = (entry.get("id") or "").removeprefix("t3_")
                raw.community = CommunityInfo(platform="reddit", post_id=post_id, discussion_url=raw.url,
                                              community=sub)
                raw.meta["subreddit"] = sub
                raw.meta["via"] = "rss"
                out.append(raw)
        return out
