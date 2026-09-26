"""Reddit collector.

Access order for every listing:

1. OAuth (oauth.reddit.com) when a client id/secret is configured: full JSON with votes,
   comment counts and flair.
2. Anonymous old.reddit.com JSON, unless it was blocked for this source recently. Many
   networks now get a login redirect (or 403) here; the block is remembered in
   `Source.state` for `JSON_BLOCK_HOURS` so we do not burn a paced request every cycle.
3. The public Atom feed on www.reddit.com with a descriptive User-Agent. old.reddit.com
   RSS redirects to login just like its JSON, but www.reddit.com/r/<sub>/<sort>/.rss keeps
   answering. Atom has no vote counts, so the post's position in the hot/top listing is kept
   (`reddit_rank`) and used by the ranker as the engagement signal.

Anonymous access has a small per-IP budget. A "too many requests" answer starts a cooldown that
every Radar process shares (a timestamp file in the runtime directory): while it lasts no Reddit
request is sent, background work defers instead of failing, and the reader shows what is cached.

The identifier is a subreddit (`LocalLLaMA`, `r/LocalLLaMA`, or a `rust+golang` multireddit)
or a Reddit feed URL, e.g. the private front-page feed from https://www.reddit.com/prefs/feeds
which follows the user's own subscriptions. The old.reddit fallback idea comes from Horizon
(MIT); see THIRD_PARTY_NOTICES.md.
"""
from __future__ import annotations

import base64
import html
import os
import re
import time
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from radar import paths
from radar.collectors.base import Collector, CollectorError, Deferred, FetchResult, SourceSpec
from radar.collectors.feedutil import entry_html, entry_published, parse_feed
from radar.config import Config
from radar.extraction.canonical_url import extract_urls
from radar.net import BROWSER_UA, Http, HttpError
from radar.normalization.models import CommunityInfo, RawItem
from radar.util import html_to_text, iso, parse_datetime, redact_feed_tokens, strip_html, utcnow

OLD = "https://old.reddit.com"
WWW = "https://www.reddit.com"
OAUTH = "https://oauth.reddit.com"
# Reddit asks API clients for a unique, descriptive agent; browser-like agents get 429/403.
REDDIT_UA = "linux:nyra-radar:0.2 (self-hosted personal tech radar)"
MEDIA_HOSTS = ("i.redd.it", "v.redd.it", "preview.redd.it", "i.imgur.com", "reddit.com/gallery")
RANKED_SORTS = ("hot", "top", "best", "rising", "front")
JSON_BLOCK_HOURS = 6

_SELF_BLOCK = re.compile(r"<!--\s*SC_OFF\s*-->(.*?)<!--\s*SC_ON\s*-->", re.DOTALL)
_LINK_ANCHOR = re.compile(r"<a href=\"([^\"]+)\">\s*\[link\]\s*</a>")
_POST_PATH = re.compile(r"/r/([^/]+)/comments/([a-z0-9]+)", re.IGNORECASE)
_HREF = re.compile(r"href=\"(https?://[^\"]+)\"")
_ANCHOR = re.compile(r"<a\s[^>]*href=\"(https?://[^\"]+)\"[^>]*>.*?</a>", re.DOTALL)
_MD_IMAGE_URL = re.compile(r"https://(?:preview\.redd\.it|i\.redd\.it|i\.imgur\.com)/[^\s)\]]+")
INLINE_IMAGE_HOSTS = ("preview.redd.it", "i.redd.it", "i.imgur.com")
MAX_INLINE_IMAGES = 12
# Atom has no `stickied` flag; pinned community threads are recognisable by title.
_PINNED_TITLE = re.compile(r"\b(mega ?thread|(daily|weekly|bi-weekly|monthly) (discussion|thread))\b",
                           re.IGNORECASE)


DEFAULT_COOLDOWN = 60.0
INTERACTIVE_TIMEOUT = 10.0


class RedditRateLimited(HttpError):
    """Reddit answered 429, or a shared cooldown from an earlier 429 is still running."""

    def __init__(self, seconds: float):
        super().__init__(f"Reddit asked Radar to slow down; retrying in {max(1, round(seconds))}s",
                         429, seconds)


def _stamp(name: str):
    return paths.runtime_dir() / name


def _read_stamp(name: str) -> float:
    try:
        return float(_stamp(name).read_text().strip() or 0)
    except (OSError, ValueError):
        return 0.0


def _write_stamp(name: str, value: float) -> None:
    path = _stamp(name)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{os.getpid()}")
        tmp.write_text(f"{value:.3f}")
        tmp.replace(path)
    except OSError:
        pass


def cooldown_remaining() -> float:
    """Seconds left on the shared Reddit cooldown (0 when requests may go out)."""
    return max(0.0, _read_stamp("reddit-cooldown") - time.time())


def start_cooldown(seconds: float | None) -> float:
    wait = max(15.0, min(float(seconds or DEFAULT_COOLDOWN), 600.0))
    until = time.time() + wait
    if until > _read_stamp("reddit-cooldown"):
        _write_stamp("reddit-cooldown", until)
    return wait


def note_interactive() -> None:
    """The reader just asked Reddit for something; background prefetching steps aside for a while."""
    _write_stamp("reddit-interactive", time.time())


def interactive_recent(seconds: float = 120.0) -> bool:
    return time.time() - _read_stamp("reddit-interactive") < seconds


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
                headers={"Authorization": f"Basic {basic}", "User-Agent": REDDIT_UA},
                data={"grant_type": "client_credentials"})
            data = resp.json()
            self.token = data["access_token"]
            self.expires = time.time() + float(data.get("expires_in", 3600))
        return {"Authorization": f"Bearer {self.token}", "User-Agent": REDDIT_UA}


async def reddit_json(http: Http, auth: RedditAuth | None, path: str, params: dict[str, Any]) -> Any:
    """GET a Reddit listing path (e.g. '/r/rust/hot') as JSON via OAuth or old.reddit."""
    params = {**params, "raw_json": 1}
    if auth and auth.enabled:
        headers = await auth.headers()
        return (await http.get(OAUTH + path, headers=headers, params=params)).json()
    if (wait := cooldown_remaining()) > 0:
        raise RedditRateLimited(wait)
    try:
        resp = await http.get(OLD + path.rstrip("/") + ".json", headers={"User-Agent": BROWSER_UA},
                              params=params, retries=0, retry_on_429=False)
    except HttpError as exc:
        if exc.status_code == 429:
            raise RedditRateLimited(start_cooldown(exc.retry_after)) from exc
        raise
    if "/login" in urlsplit(resp.url).path:
        raise ValueError("anonymous JSON redirected to login")
    return resp.json()


async def reddit_rss(http: Http, url: str, params: dict[str, Any] | None = None, *,
                     interactive: bool = False) -> Any:
    """GET a www.reddit.com Atom feed with the descriptive agent Reddit expects.

    Honours the shared cooldown and never sleeps on 429 (it starts the cooldown and raises
    RedditRateLimited). `interactive` requests (someone is waiting) fail fast: no retry, short timeout.
    """
    parts = urlsplit(url)
    if parts.hostname in ("reddit.com", "old.reddit.com", "new.reddit.com"):
        url = urlunsplit(("https", "www.reddit.com", parts.path, parts.query, ""))
    if (wait := cooldown_remaining()) > 0:
        raise RedditRateLimited(wait)
    if interactive:
        note_interactive()
    try:
        resp = await http.get(url, headers={"User-Agent": REDDIT_UA, "Accept": "application/atom+xml"},
                              params=params, retries=0 if interactive else 1, retry_on_429=False,
                              timeout=INTERACTIVE_TIMEOUT if interactive else None)
    except HttpError as exc:
        if exc.status_code == 429:
            raise RedditRateLimited(start_cooldown(exc.retry_after)) from exc
        raise
    if "/login" in urlsplit(resp.url).path:
        raise HttpError("Reddit feed redirected to login", 403)
    return parse_feed(resp.content)


def full_size_image(url: str | None) -> str | None:
    """preview.redd.it/<id>.<ext>?width=140&… -> i.redd.it/<id>.<ext>, the original upload.

    Reddit-hosted images (single images and gallery items) share their media id between the
    signed, resized preview and the original, so Atom's 140 px thumbnail leads to the full image.
    external-preview.redd.it (link previews of other sites) has no such original.
    """
    if not url:
        return None
    parts = urlsplit(html.unescape(url))
    if parts.hostname != "preview.redd.it" or not re.fullmatch(r"/[A-Za-z0-9]+\.(png|jpe?g|gif|webp)", parts.path):
        return None
    return f"https://i.redd.it{parts.path}"


def inline_image(url: str) -> str | None:
    """Full-size URL for a Reddit-hosted (or imgur) image linked inside a post, else None."""
    url = html.unescape(url)
    host = urlsplit(url).hostname or ""
    if host not in INLINE_IMAGE_HOSTS:
        return None
    return full_size_image(url) or (url if re.search(r"\.(png|jpe?g|gif|webp)$", urlsplit(url).path) else None)


def selftext_with_images(fragment: str) -> tuple[str, list[str]]:
    """Reddit post HTML -> readable text in which every inline image link becomes a line holding
    the image's full-size URL (the reader renders those lines as pictures, in place)."""
    images: list[str] = []

    def swap(match: re.Match) -> str:
        full = inline_image(match.group(1))
        if not full:
            return match.group(0)
        if full not in images:
            images.append(full)
        return f"<p>{full}</p>"

    text = html_to_text(_ANCHOR.sub(swap, fragment))
    return text, images[:MAX_INLINE_IMAGES]


def media_type(target: str | None, post: dict[str, Any] | None = None) -> str | None:
    """image | gallery | video for Reddit-hosted media posts, else None."""
    post = post or {}
    if post.get("is_gallery") or (target and "reddit.com/gallery" in target):
        return "gallery"
    if post.get("is_video") or (target and "v.redd.it" in target):
        return "video"
    if target and any(h in target for h in ("i.redd.it", "preview.redd.it", "i.imgur.com")):
        return "image"
    return None


def _listing_meta(sort: str, rank: int, size: int) -> dict[str, Any]:
    return {"reddit_sort": sort, "reddit_rank": rank, "reddit_listing_size": size,
            "reddit_seen_at": iso(utcnow())}


def post_to_raw(post: dict[str, Any]) -> RawItem | None:
    title = html.unescape((post.get("title") or "").strip())
    permalink = post.get("permalink") or ""
    if not title or not permalink:
        return None
    discussion = "https://www.reddit.com" + permalink
    is_self = bool(post.get("is_self"))
    target = post.get("url_overridden_by_dest") or post.get("url") or ""
    is_media = any(h in target for h in MEDIA_HOSTS) or bool(post.get("is_gallery") or post.get("is_video"))
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
              "via": "json", "media_url": target if is_media else None,
              "media_type": media_type(target, post) if is_media else None,
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
        item.add_image("reddit_thumb", thumb)
    for media in list((post.get("media_metadata") or {}).values())[:1]:  # first gallery image
        source = (media.get("s") or {}).get("u") or (media.get("s") or {}).get("gif")
        item.add_image("reddit_preview", html.unescape(source) if source else None)
    inline = []
    for url in _MD_IMAGE_URL.findall(selftext):
        full = inline_image(url)
        if full and full not in inline:
            inline.append(full)
    if inline:
        item.meta["inline_images"] = inline[:MAX_INLINE_IMAGES]
        item.add_image("reddit_preview", inline[0])
    if link_url:
        item.links.append(link_url)
    item.links += extract_urls(selftext, 30)
    return item


def rss_entry_to_raw(entry: Any) -> RawItem | None:
    """One www.reddit.com Atom entry -> RawItem (no votes; the caller adds listing rank)."""
    discussion = entry.get("link") or ""
    title = html.unescape(strip_html(entry.get("title") or ""))
    match = _POST_PATH.search(discussion)
    if not title or not match or (entry.get("id") or "").startswith("t1_"):  # t1_ = saved comment
        return None
    sub = next((t.get("term") for t in entry.get("tags") or [] if t.get("term")), None) or match.group(1)
    post_id = (entry.get("id") or "").removeprefix("t3_") or match.group(2)
    body = entry_html(entry)
    self_block = _SELF_BLOCK.search(body)
    selftext, inline = selftext_with_images(self_block.group(1)) if self_block else ("", [])
    anchor = _LINK_ANCHOR.search(body)
    target = html.unescape(anchor.group(1)) if anchor else ""
    points_home = not target or _POST_PATH.search(target) is not None
    is_media = bool(target) and any(h in target for h in MEDIA_HOSTS)
    is_self = points_home and not is_media
    link_url = None if (is_self or is_media) else target
    author = (entry.get("author") or "").removeprefix("/u/").strip() or None
    item = RawItem(
        url=discussion,
        title=title,
        content_type="reddit_post",
        source_item_id=f"t3_{post_id}",
        text=selftext or None,
        excerpt=selftext[:600] or None,
        author=author,
        published_at=entry_published(entry),
        link_url=link_url,
        community=CommunityInfo(platform="reddit", post_id=post_id, discussion_url=discussion, community=sub,
                                score=None, num_comments=None, is_self=is_self,
                                post_kind="self" if is_self else ("media" if is_media else "link")),
        meta={"subreddit": sub, "via": "rss", "media_url": target if is_media else None,
              "media_type": media_type(target) if is_media else None, "inline_images": inline},
    )
    if is_media and target.startswith(("https://i.redd.it", "https://i.imgur.com")):
        item.add_image("reddit_preview", target)
    for thumb in entry.get("media_thumbnail") or []:
        url = html.unescape(thumb.get("url") or "")
        item.add_image("reddit_preview", full_size_image(url))
        item.add_image("reddit_thumb", url)
    for url in inline[:1]:  # a text post with pictures leads with its first one
        item.add_image("reddit_preview", url)
    if link_url:
        item.links.append(link_url)
    if self_block:
        item.links += [html.unescape(u) for u in _HREF.findall(self_block.group(1))[:30]]
    return item


def _feed_url_parts(identifier: str) -> tuple[str, str]:
    """Split a Reddit feed URL into (json_url, rss_url); `.rss` and `.json` are interchangeable."""
    parts = urlsplit(identifier)
    base = parts.path or "/"
    for suffix in (".rss", ".json"):
        base = base.removesuffix(suffix)
    base = base.rstrip("/")
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query) if k != "limit"])
    json_url = urlunsplit(("https", "www.reddit.com", f"{base}/.json", query, ""))
    rss_url = urlunsplit(("https", "www.reddit.com", f"{base}/.rss", query, ""))
    return json_url, rss_url


def _json_blocked(state: dict[str, Any]) -> bool:
    until = parse_datetime(state.get("json_blocked_until"))
    return bool(until and until > utcnow())


class RedditCollector(Collector):
    kind = "reddit"
    content_type = "reddit_post"

    def __init__(self, http: Http, config: Config):
        super().__init__(http, config)
        self.auth = RedditAuth(http, config)

    async def fetch(self, source: SourceSpec) -> FetchResult:
        state = dict(source.state or {})
        limit = source.limit(25)
        min_score = int(source.config.get("min_score", 3))
        feed_url = source.identifier if source.identifier.startswith("http") else None
        sub = None if feed_url else source.identifier.removeprefix("r/").strip("/")
        # Feed URLs: the private front page ranks like `hot`; upvoted/saved listings are labelled
        # by their account role (see radar.services.reddit_account) and never count as heat.
        sorts = ([source.config.get("account") or "front"] if feed_url
                 else (source.config.get("sorts") or [source.config.get("sort", "hot")]))
        items: dict[str, RawItem] = {}
        errors: list[str] = []
        via = set()
        rate_limited: RedditRateLimited | None = None
        for sort in sorts:
            params: dict[str, Any] = {"limit": min(100, limit + 5)}
            if sort in ("top", "controversial"):
                params["t"] = source.config.get("t", "day")
            listing: list[RawItem] = []
            if self.auth.enabled or not _json_blocked(state):
                try:
                    listing = await self._json_listing(sub, feed_url, sort, params, min_score)
                    via.add("oauth" if self.auth.enabled else "json")
                    state.pop("json_blocked_until", None)
                except RedditRateLimited as exc:
                    rate_limited = exc
                except (HttpError, ValueError, KeyError) as exc:
                    if getattr(exc, "status_code", None) == 404:
                        raise CollectorError(f"r/{sub} not found", 404, permanent=True) from exc
                    errors.append(f"{sort} json: {exc}")
                    if not self.auth.enabled:
                        state["json_blocked_until"] = iso(utcnow() + timedelta(hours=JSON_BLOCK_HOURS))
            if not listing and rate_limited is None:
                try:
                    listing = await self._rss_listing(sub, feed_url, sort, params)
                    via.add("rss")
                except RedditRateLimited as exc:
                    rate_limited = exc
                except (HttpError, ValueError) as exc:
                    code = getattr(exc, "status_code", None)
                    if code == 404:
                        raise CollectorError(f"r/{sub} not found", 404, permanent=True) from exc
                    errors.append(f"{sort} rss: {exc}")
            for raw in listing:
                items.setdefault(raw.source_item_id or raw.url, raw)
        state["last_access"] = "+".join(sorted(via)) or ("deferred" if rate_limited else "failed")
        if not items and rate_limited is not None:
            raise Deferred(str(rate_limited), rate_limited.retry_after or DEFAULT_COOLDOWN)
        if not items and errors:
            raise CollectorError(redact_feed_tokens("; ".join(errors)[:500]))
        return FetchResult(items=list(items.values())[: limit * len(sorts)], status_code=200, state=state)

    async def _json_listing(self, sub: str | None, feed_url: str | None, sort: str,
                            params: dict[str, Any], min_score: int) -> list[RawItem]:
        if feed_url:
            json_url, _ = _feed_url_parts(feed_url)
            resp = await self.http.get(json_url, headers={"User-Agent": REDDIT_UA}, params=params, retries=0)
            data = resp.json()
        else:
            data = await reddit_json(self.http, self.auth, f"/r/{sub}/{sort}", params)
        children = (data.get("data") or {}).get("children") or []
        out: list[RawItem] = []
        for child in children:
            post = child.get("data") or {}
            if post.get("stickied") or post.get("over_18"):
                continue
            if int(post.get("score") or 0) < min_score and sort != "new":
                continue
            raw = post_to_raw(post)
            if raw:
                out.append(raw)
        for rank, raw in enumerate(out):
            raw.meta.update(_listing_meta(sort, rank, len(out)))
        return out

    async def _rss_listing(self, sub: str | None, feed_url: str | None, sort: str,
                           params: dict[str, Any]) -> list[RawItem]:
        if feed_url:
            _, url = _feed_url_parts(feed_url)
        else:
            url = f"{WWW}/r/{sub}/{sort}/.rss"
        parsed = await reddit_rss(self.http, url, params)
        out: list[RawItem] = []
        for entry in parsed.entries:
            raw = rss_entry_to_raw(entry)
            if raw and not _PINNED_TITLE.search(raw.title):
                out.append(raw)
        for rank, raw in enumerate(out):
            raw.meta.update(_listing_meta(sort, rank, len(out)))
        return out
