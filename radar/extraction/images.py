"""Image acquisition and local caching.

Priority for choosing an item's visual (see normalization.models.IMAGE_PRIORITY):
custom GitHub social preview → README hero → GitHub card → og:image → twitter:image →
Reddit preview → YouTube thumbnail → feed image → site icon → (frontend) neutral card.

Cached images are stored once per content hash as downscaled WebP. The DB records
the original URL, MIME, original dimensions and fetch time. Rows that are already
cached (or known failures) are never downloaded again.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image as PILImage
from PIL import UnidentifiedImageError

from radar.net import BROWSER_UA, Http, HttpError
from radar.normalization.models import IMAGE_PRIORITY

log = logging.getLogger(__name__)
PILImage.MAX_IMAGE_PIXELS = 40_000_000

MIN_DIMS = {"site_icon": (48, 48), "reddit_thumb": (96, 64)}
DEFAULT_MIN = (160, 80)
BADGE_HOSTS = ("shields.io", "badgen.net", "badge.fury.io", "travis-ci", "codecov.io", "circleci.com",
               "github.com/.*/workflows", "img.shields", "star-history.com", "api.star-history",
               "contrib.rocks", "readme-typing-svg", "komarev.com", "visitor-badge", "hits.seeyoufarm",
               "deepwiki.com/badge", "discord.com/api/guilds", "trendshift.io/api/badge")


class ImageRejected(Exception):
    pass


@dataclass
class CachedImage:
    content_hash: str
    mime: str
    width: int
    height: int
    bytes: int
    rel_path: str


def priority(kind: str) -> int:
    try:
        return IMAGE_PRIORITY.index(kind)
    except ValueError:
        return len(IMAGE_PRIORITY)


def looks_like_badge(url: str) -> bool:
    low = url.lower()
    return any(b in low for b in BADGE_HOSTS) or low.endswith(".svg") or "badge" in low.split("?")[0][-40:]


def order_candidates(cands: list[tuple[str, str]]) -> list[tuple[str, str]]:
    seen: set[str] = set()
    out = []
    for kind, url in sorted(cands, key=lambda c: priority(c[0])):
        if url in seen or looks_like_badge(url):
            continue
        seen.add(url)
        out.append((kind, url))
    return out


def reopen_image_stage(item: Any, before: list[tuple[str, str]], after: list[tuple[str, str]]) -> None:
    """New image candidates (og:image from the article, inline Reddit images) re-open the image
    stage when nothing is cached yet or when a candidate outranks the cached one."""
    known = {tuple(c) for c in before}
    fresh = [tuple(c) for c in after if tuple(c) not in known]
    if not fresh:
        return
    if item.image_state in ("none", "failed"):
        item.image_state = "pending"
    elif item.image_state == "done":
        current = next((kind for kind, url in before if url == item.image_url), None)
        if current is None or min(priority(kind) for kind, _ in fresh) < priority(current):
            item.image_state = "pending"


def _process(data: bytes, kind: str, max_width: int, root: Path) -> CachedImage:
    content_hash = hashlib.sha256(data).hexdigest()
    try:
        with PILImage.open(io.BytesIO(data)) as im:
            fmt = (im.format or "").upper()
            width, height = im.size
            min_w, min_h = MIN_DIMS.get(kind, DEFAULT_MIN)
            if width < min_w or height < min_h:
                raise ImageRejected(f"too small {width}x{height}")
            ratio = width / max(1, height)
            if kind != "site_icon" and (ratio > 4.5 or ratio < 0.3):
                raise ImageRejected(f"extreme aspect {width}x{height}")
            rel = f"{content_hash[:2]}/{content_hash}.webp"
            dest = root / rel
            if not dest.exists():
                dest.parent.mkdir(parents=True, exist_ok=True)
                frame = im.convert("RGBA") if im.mode in ("P", "LA", "RGBA") else im.convert("RGB")
                if frame.width > max_width:
                    frame = frame.resize((max_width, int(frame.height * max_width / frame.width)),
                                         PILImage.LANCZOS)
                tmp = dest.with_suffix(".tmp")
                frame.save(tmp, "WEBP", quality=80, method=4)
                tmp.replace(dest)
            mime = PILImage.MIME.get(fmt, f"image/{fmt.lower()}" if fmt else "application/octet-stream")
            return CachedImage(content_hash, mime, width, height, len(data), rel)
    except (UnidentifiedImageError, OSError, ValueError, PILImage.DecompressionBombError) as exc:
        raise ImageRejected(f"not a decodable image: {type(exc).__name__}") from exc


class ImageCache:
    def __init__(self, http: Http, root: Path, max_width: int = 960):
        self.http = http
        self.root = root
        self.max_width = max_width
        root.mkdir(parents=True, exist_ok=True)

    async def download(self, url: str, kind: str, etag: str | None = None) -> tuple[CachedImage | None, str | None]:
        """Returns (image, etag). (None, etag) means 304 Not Modified."""
        headers = {"User-Agent": BROWSER_UA, "Accept": "image/avif,image/webp,image/png,image/jpeg,image/*;q=0.8"}
        if etag:
            headers["If-None-Match"] = etag
        try:
            resp = await self.http.get(url, headers=headers, max_bytes=8_000_000, retries=1, timeout=20)
        except HttpError as exc:
            raise ImageRejected(str(exc)) from exc
        if resp.status_code == 304:
            return None, etag
        ctype = resp.headers.get("content-type", "")
        if ctype and not ctype.startswith("image/") and "octet-stream" not in ctype:
            raise ImageRejected(f"content-type {ctype[:40]}")
        if "svg" in ctype:
            raise ImageRejected("svg not cached")
        img = await asyncio.to_thread(_process, resp.content, kind, self.max_width, self.root)
        return img, resp.headers.get("etag")

    def path_for(self, rel: str) -> Path:
        return self.root / rel
