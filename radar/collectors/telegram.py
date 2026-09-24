"""Public Telegram channels via the t.me/s/<channel> web preview (no credentials needed).

The web-preview parsing approach follows Horizon (MIT); see THIRD_PARTY_NOTICES.md.
Private channels would need MTProto credentials and are intentionally unsupported.
"""
from __future__ import annotations

import re

from bs4 import BeautifulSoup

from radar.collectors.base import Collector, CollectorError, FetchResult, SourceSpec
from radar.net import BROWSER_UA, HttpError
from radar.normalization.models import RawItem
from radar.util import normalize_ws, parse_datetime, safe_int

_BG = re.compile(r"background-image:url\('([^']+)'\)")


def parse_channel(html: str, channel: str) -> list[RawItem]:
    soup = BeautifulSoup(html, "lxml")
    items = []
    for msg in soup.select("div.tgme_widget_message[data-post]"):
        post = msg.get("data-post", "")
        text_el = msg.select_one(".tgme_widget_message_text")
        text = text_el.get_text("\n", strip=True) if text_el else ""
        if not text:
            continue
        time_el = msg.select_one("time[datetime]")
        views = msg.select_one(".tgme_widget_message_views")
        first_line = normalize_ws(text.split("\n", 1)[0])
        item = RawItem(
            url=f"https://t.me/{post}",
            title=first_line[:160] or f"{channel} post",
            content_type="telegram_post",
            source_item_id=post,
            text=text,
            excerpt=text[:600],
            author=channel,
            published_at=parse_datetime(time_el["datetime"]) if time_el else None,
            external_score=safe_int((views.get_text(strip=True) if views else "").replace("K", "000")
                                    .replace("M", "000000").replace(".", "")),
            meta={"channel": channel},
        )
        for a in (text_el.select("a[href]") if text_el else []):
            href = a["href"]
            if href.startswith("http") and "t.me/" not in href:
                item.links.append(href)
        photo = msg.select_one(".tgme_widget_message_photo_wrap")
        if photo and photo.get("style"):
            m = _BG.search(photo["style"])
            if m:
                item.add_image("feed", m.group(1))
        if item.links:
            item.link_url = item.links[0]
        items.append(item)
    return items


class TelegramCollector(Collector):
    kind = "telegram"
    content_type = "telegram_post"

    async def fetch(self, source: SourceSpec) -> FetchResult:
        channel = source.identifier.removeprefix("@").strip("/")
        try:
            resp = await self.http.get(f"https://t.me/s/{channel}", headers={"User-Agent": BROWSER_UA})
        except HttpError as exc:
            raise CollectorError(str(exc), exc.status_code) from exc
        items = parse_channel(resp.text, channel)
        return FetchResult(items=items[-source.limit(20):], status_code=resp.status_code)
