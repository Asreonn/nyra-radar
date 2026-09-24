"""Website change monitoring: emits a `website_change` item when the main text changes."""
from __future__ import annotations

import difflib

from radar.collectors.base import Collector, CollectorError, FetchResult, SourceSpec
from radar.extraction.article import extract_main_text
from radar.extraction.metadata import parse_metadata
from radar.net import BROWSER_UA, HttpError
from radar.normalization.models import RawItem
from radar.util import sha1_hex, utcnow


def summarize_diff(old: str, new: str, max_lines: int = 12) -> tuple[list[str], list[str]]:
    old_lines = [ln.strip() for ln in old.splitlines() if ln.strip()]
    new_lines = [ln.strip() for ln in new.splitlines() if ln.strip()]
    added, removed = [], []
    for line in difflib.unified_diff(old_lines, new_lines, lineterm="", n=0):
        if line.startswith("+") and not line.startswith("+++"):
            added.append(line[1:])
        elif line.startswith("-") and not line.startswith("---"):
            removed.append(line[1:])
    return added[:max_lines], removed[:max_lines]


class WebsiteWatchCollector(Collector):
    kind = "website_watch"
    content_type = "website_change"

    async def fetch(self, source: SourceSpec) -> FetchResult:
        url = source.identifier
        try:
            resp = await self.http.get(url, headers={"User-Agent": BROWSER_UA})
        except HttpError as exc:
            raise CollectorError(str(exc), exc.status_code, permanent=exc.status_code in (404, 410)) from exc
        text = extract_main_text(resp.text, url) or ""
        digest = sha1_hex(text)
        state = dict(source.state or {})
        previous_hash = state.get("hash")
        previous_text = state.get("text", "")
        new_state = {"hash": digest, "text": text[:20000], "checked_at": utcnow().isoformat()}
        if previous_hash is None or previous_hash == digest:
            return FetchResult(items=[], state=new_state, status_code=resp.status_code)
        added, removed = summarize_diff(previous_text, text)
        meta = parse_metadata(resp.text, url)
        min_changed = int(source.config.get("min_changed_lines", 1))
        if len(added) + len(removed) < min_changed:
            return FetchResult(items=[], state=new_state, status_code=resp.status_code)
        body = "\n".join(["Added:"] + [f"+ {a}" for a in added] + ["", "Removed:"] + [f"- {r}" for r in removed])
        item = RawItem(
            url=f"{url}#change-{digest[:10]}",
            title=f"{source.name} changed: {added[0][:90] if added else 'content updated'}",
            content_type="website_change",
            source_item_id=digest,
            text=body,
            excerpt=(" · ".join(added[:3]) or "Content changed")[:600],
            published_at=utcnow(),
            link_url=url,
            meta={"watched_url": url, "added": added, "removed": removed, "previous_hash": previous_hash},
        )
        for kind, img in meta.image_candidates():
            item.add_image(kind, img)
        return FetchResult(items=[item], state=new_state, status_code=resp.status_code)
