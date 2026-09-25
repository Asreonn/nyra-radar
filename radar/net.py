"""Shared async HTTP client with per-host pacing, bounded retries and size limits."""
from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

log = logging.getLogger(__name__)

BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0 NyraRadar/0.1"
BOT_UA = "NyraRadar/0.1 (+personal self-hosted tech radar; contact: local user)"

# Minimum seconds between requests to the same host (politeness / rate limits).
HOST_INTERVALS: dict[str, float] = {
    "old.reddit.com": 3.0,
    "www.reddit.com": 3.0,
    "oauth.reddit.com": 1.0,
    "api.github.com": 0.35,
    "github.com": 1.0,
    "hacker-news.firebaseio.com": 0.05,
    "hn.algolia.com": 0.25,
    "news.google.com": 2.0,
    "api.ossinsight.io": 1.5,
    "www.youtube.com": 1.0,
    "t.me": 2.0,
}
DEFAULT_HOST_INTERVAL = 0.5
RETRY_STATUSES = {429, 500, 502, 503, 504}


class HttpError(Exception):
    def __init__(self, message: str, status_code: int | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


@dataclass
class Response:
    url: str
    status_code: int
    headers: dict[str, str]
    content: bytes

    @property
    def text(self) -> str:
        ctype = self.headers.get("content-type", "")
        charset = "utf-8"
        if "charset=" in ctype:
            charset = ctype.split("charset=")[-1].split(";")[0].strip() or "utf-8"
        try:
            return self.content.decode(charset, errors="replace")
        except LookupError:
            return self.content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        import json

        return json.loads(self.content)


class HostPacer:
    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._next: dict[str, float] = {}

    async def wait(self, host: str) -> None:
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            ready = self._next.get(host, 0.0)
            if ready > now:
                await asyncio.sleep(ready - now)
            self._next[host] = time.monotonic() + HOST_INTERVALS.get(host, DEFAULT_HOST_INTERVAL)

    def penalize(self, host: str, seconds: float) -> None:
        self._next[host] = max(self._next.get(host, 0.0), time.monotonic() + seconds)


class Http:
    """Thin wrapper around one shared httpx.AsyncClient."""

    def __init__(self, client: httpx.AsyncClient | None = None, max_concurrency: int = 8):
        self.client = client or httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(20.0, connect=10.0),
            limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
            headers={"User-Agent": BOT_UA, "Accept-Language": "en-US,en;q=0.8"},
        )
        self.pacer = HostPacer()
        self.sem = asyncio.Semaphore(max_concurrency)
        self.requests = 0

    async def close(self) -> None:
        await self.client.aclose()

    async def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        retries: int = 2,
        max_bytes: int = 6_000_000,
        timeout: float | None = None,
        accept_statuses: tuple[int, ...] = (200,),
    ) -> Response:
        return await self.request("GET", url, headers=headers, params=params, retries=retries,
                                  max_bytes=max_bytes, timeout=timeout, accept_statuses=accept_statuses)

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json: Any = None,
        data: Any = None,
        retries: int = 2,
        max_bytes: int = 6_000_000,
        timeout: float | None = None,
        accept_statuses: tuple[int, ...] = (200,),
    ) -> Response:
        # httpx replaces a URL's query string with `params` instead of merging them; that silently
        # dropped the private ?feed=…&user=… token from Reddit account feeds.
        url, params = merge_query(url, params)
        host = urlsplit(url).hostname or ""
        attempt = 0
        while True:
            attempt += 1
            await self.pacer.wait(host)
            try:
                async with self.sem:
                    self.requests += 1
                    kwargs: dict[str, Any] = {"headers": headers, "params": params}
                    if json is not None:
                        kwargs["json"] = json
                    if data is not None:
                        kwargs["data"] = data
                    if timeout is not None:
                        kwargs["timeout"] = timeout
                    async with self.client.stream(method, url, **kwargs) as resp:
                        chunks: list[bytes] = []
                        size = 0
                        async for chunk in resp.aiter_bytes():
                            size += len(chunk)
                            if size > max_bytes:
                                break
                            chunks.append(chunk)
                        result = Response(str(resp.url), resp.status_code,
                                          {k.lower(): v for k, v in resp.headers.items()}, b"".join(chunks))
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt > retries:
                    raise HttpError(f"{type(exc).__name__}: {exc}") from exc
                await asyncio.sleep(min(30.0, (2 ** attempt) + random.random()))
                continue
            if result.status_code in accept_statuses or result.status_code == 304:
                return result
            retry_after = _retry_after(result.headers.get("retry-after"))
            if result.status_code in RETRY_STATUSES and attempt <= retries:
                delay = retry_after if retry_after is not None else (2 ** attempt) * 1.5 + random.random()
                if result.status_code == 429:
                    self.pacer.penalize(host, min(delay, 120.0))
                await asyncio.sleep(min(delay, 60.0))
                continue
            raise HttpError(f"HTTP {result.status_code} for {url[:200]}", result.status_code, retry_after)


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def merge_query(url: str, params: dict[str, Any] | None) -> tuple[str, dict[str, Any] | None]:
    """Fold `params` into a URL that already carries a query string (params win on conflicts)."""
    parts = urlsplit(url)
    if not params or not parts.query:
        return url, params
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.update({k: str(v) for k, v in params.items() if v is not None})
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)), None
