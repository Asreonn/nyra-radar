"""URL canonicalization and link classification.

Canonical URLs are the first dedup layer: different raw URLs pointing at the same
resource (tracking params, mobile/AMP hosts, youtu.be, reddit mirrors, trailing
slashes, fragments) collapse to one identity. The tracking-parameter idea follows
Horizon's URL dedup key (MIT); see THIRD_PARTY_NOTICES.md.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

from radar.util import sha1_hex

TRACKING_PARAMS = {
    "fbclid", "gclid", "dclid", "gbraid", "wbraid", "msclkid", "mc_cid", "mc_eid", "igshid", "yclid",
    "ref", "ref_src", "ref_url", "referrer", "source", "src", "cmpid", "campaign", "via", "share",
    "s_kwcid", "spm", "_hsenc", "_hsmi", "mkt_tok", "trk", "trkcampaign", "si", "feature",
    "sc_channel", "sr_share", "rss", "rss_source", "guccounter", "ncid", "ocid",
    "smid", "at_medium", "at_campaign", "utm", "__twitter_impression",
}
# Parameters that identify content on specific hosts and must never be stripped.
KEEP_PARAMS = {
    "news.ycombinator.com": {"id"},
    "www.youtube.com": {"v", "list"},
    "youtube.com": {"v", "list"},
}
REDDIT_HOSTS = {"reddit.com", "www.reddit.com", "old.reddit.com", "np.reddit.com", "new.reddit.com",
                "m.reddit.com", "i.reddit.com", "amp.reddit.com"}
GITHUB_RESERVED = {
    "about", "features", "topics", "trending", "collections", "explore", "marketplace", "pricing", "orgs",
    "sponsors", "settings", "login", "join", "notifications", "search", "apps", "enterprise", "security",
    "site", "readme", "customer-stories", "events", "team", "new", "organizations", "codespaces", "issues",
    "pulls", "users", "resources", "solutions", "github-copilot", "copilot", "contact",
}
_GH_REPO = re.compile(r"^/([A-Za-z0-9][A-Za-z0-9-]{0,38})/([A-Za-z0-9._-]{1,100})(?:/|$)")
_URL_IN_TEXT = re.compile(r"https?://[^\s<>\"'\]\)\}|,]+", re.IGNORECASE)
_SUBREDDIT_IN_TEXT = re.compile(r"(?<![A-Za-z0-9/])/?r/([A-Za-z0-9][A-Za-z0-9_]{2,20})\b")


def canonicalize(url: str) -> str:
    """Return the canonical form of a URL (idempotent)."""
    url = (url or "").strip()
    if not url:
        return ""
    if url.startswith("//"):
        url = "https:" + url
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url):
        url = "https://" + url
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        return url
    host = (parts.hostname or "").lower().rstrip(".")
    port = parts.port
    path = parts.path or "/"
    query = parts.query

    # Host normalisation (mobile / AMP / mirrors).
    if host.startswith("m.") and host not in ("m.youtube.com",):
        host = host[2:]
    if host in ("m.youtube.com", "youtube.com", "music.youtube.com"):
        host = "www.youtube.com"
    if host == "youtu.be":
        vid = path.strip("/").split("/")[0]
        host, path, query = "www.youtube.com", "/watch", urlencode({"v": vid})
    if host in REDDIT_HOSTS or host.endswith(".reddit.com"):
        host = "www.reddit.com"
        # /r/sub/comments/<id>/<slug>/ -> /r/sub/comments/<id>/
        m = re.match(r"^(/r/[^/]+/comments/[a-z0-9]+)", path, re.IGNORECASE)
        if m:
            path = m.group(1) + "/"
    if host == "redd.it":
        host, path = "www.reddit.com", "/comments" + path
    if host in ("www.github.com",):
        host = "github.com"
    if host == "github.com":
        repo = _GH_REPO.match(path)
        if repo and repo.group(1).lower() not in GITHUB_RESERVED:
            # keep deeper paths (issues, releases), but canonical repo roots drop .git / trailing slash
            name = repo.group(2)
            name = name.removesuffix(".git")
            rest = path[repo.end():] if path[repo.end() - 1:repo.end()] == "/" else ""
            path = f"/{repo.group(1)}/{name}" + (("/" + rest) if rest else "")
    if "/amp/" in path or path.endswith("/amp"):
        path = path.replace("/amp/", "/").removesuffix("/amp") or "/"
    host = host.removeprefix("amp.")

    # Query cleanup.
    keep = KEEP_PARAMS.get(host, set())
    pairs = []
    for k, v in parse_qsl(query, keep_blank_values=False):
        kl = k.lower()
        if kl in keep:
            pairs.append((k, v))
            continue
        if kl.startswith(("utm_", "utm-")) or kl in TRACKING_PARAMS:
            continue
        pairs.append((k, v))
    pairs.sort()
    query = urlencode(pairs, doseq=True)

    # Path cleanup.
    path = quote(unquote(path), safe="/:@!$&'()*+,;=-._~%")
    path = re.sub(r"/{2,}", "/", path)
    if path != "/" and path.endswith("/") and host != "www.reddit.com":
        path = path.rstrip("/")
    for suffix in ("/index.html", "/index.htm", "/index.php"):
        if path.endswith(suffix):
            path = path[: -len(suffix)] or "/"
    if (scheme, port) in (("http", 80), ("https", 443)):
        port = None
    netloc = host + (f":{port}" if port else "")
    scheme = "https" if scheme == "http" and host.endswith(("github.com", "reddit.com", "youtube.com",
                                                             "ycombinator.com")) else scheme
    return urlunsplit((scheme, netloc, path, query, ""))


def url_hash(url: str) -> str:
    """Identity hash; scheme-insensitive and ignores a leading `www.`."""
    canon = canonicalize(url)
    parts = urlsplit(canon)
    host = parts.netloc.removeprefix("www.")
    return sha1_hex(f"{host}{parts.path}?{parts.query}")


def domain_of(url: str | None) -> str | None:
    if not url:
        return None
    host = (urlsplit(url if "://" in url else "https://" + url).hostname or "").lower()
    return host.removeprefix("www.") or None


def github_repo_from_url(url: str | None) -> str | None:
    """Return 'owner/name' if the URL points into a GitHub repository."""
    if not url:
        return None
    parts = urlsplit(url if "://" in url else "https://" + url)
    host = (parts.hostname or "").lower()
    if host not in ("github.com", "www.github.com"):
        return None
    m = _GH_REPO.match(parts.path or "")
    if not m:
        return None
    owner, name = m.group(1), m.group(2)
    if owner.lower() in GITHUB_RESERVED or name.lower() in {"followers", "following", "stars", "repositories"}:
        return None
    name = name.removesuffix(".git")
    if not name or name.startswith("."):
        return None
    return f"{owner}/{name}"


def youtube_video_id(url: str | None) -> str | None:
    if not url:
        return None
    canon = canonicalize(url)
    parts = urlsplit(canon)
    if parts.netloc == "www.youtube.com":
        if parts.path == "/watch":
            return dict(parse_qsl(parts.query)).get("v")
        m = re.match(r"^/(?:shorts|embed|live)/([A-Za-z0-9_-]{6,})", parts.path)
        if m:
            return m.group(1)
    return None


def extract_urls(text: str | None, limit: int = 60) -> list[str]:
    if not text:
        return []
    seen: dict[str, None] = {}
    for m in _URL_IN_TEXT.finditer(text):
        url = m.group(0).rstrip(".,;:!?*_)")
        if len(url) > 12:
            seen.setdefault(url, None)
        if len(seen) >= limit:
            break
    return list(seen)


def extract_subreddits(text: str | None) -> list[str]:
    if not text:
        return []
    return sorted({m.group(1) for m in _SUBREDDIT_IN_TEXT.finditer(text)})


def classify_link(url: str) -> str:
    if github_repo_from_url(url):
        return "github_repo"
    dom = domain_of(url) or ""
    if dom.endswith("reddit.com") and "/r/" in url:
        return "subreddit"
    if dom in ("youtube.com", "youtu.be"):
        return "youtube"
    if url.endswith((".xml", ".rss", ".atom")) or "/feed" in url:
        return "feed"
    return "link"
