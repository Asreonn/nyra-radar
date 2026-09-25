"""Small shared helpers: time, hashing, text cleanup."""
from __future__ import annotations

import hashlib
import html
import re
import time
from calendar import timegm
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any

_WS = re.compile(r"\s+")
_TAG = re.compile(r"<[^>]+>")


def utcnow() -> datetime:
    """Naive UTC 'now' (SQLite stores naive datetimes; everything in Radar is UTC)."""
    return datetime.now(UTC).replace(tzinfo=None)


def to_naive_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return dt


def parse_datetime(value: Any) -> datetime | None:
    """Parse ISO-8601, RFC-2822, unix timestamps and time.struct_time into naive UTC."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return to_naive_utc(value)
    if isinstance(value, time.struct_time):
        return datetime.fromtimestamp(timegm(value), UTC).replace(tzinfo=None)
    if isinstance(value, (int, float)):
        if value > 1e12:
            value = value / 1000.0
        return datetime.fromtimestamp(float(value), UTC).replace(tzinfo=None)
    text = str(value).strip()
    if not text:
        return None
    try:
        iso = text.replace("Z", "+00:00") if text.endswith("Z") else text
        return to_naive_utc(datetime.fromisoformat(iso))
    except ValueError:
        pass
    try:
        return to_naive_utc(parsedate_to_datetime(text))
    except (TypeError, ValueError, IndexError):
        return None


def clamp_future(dt: datetime | None, now: datetime | None = None) -> datetime | None:
    """Feeds sometimes publish future dates; clamp to now + 1h."""
    if dt is None:
        return None
    now = now or utcnow()
    return min(dt, now + timedelta(hours=1))


def iso(dt: datetime | None) -> str | None:
    return dt.replace(microsecond=0).isoformat() + "Z" if dt else None


def sha1_hex(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()


def strip_html(value: str | None) -> str:
    if not value:
        return ""
    text = _TAG.sub(" ", value)
    return normalize_ws(html.unescape(text))


def html_to_text(value: str | None) -> str:
    """Community HTML (Reddit, HN) to plain text that keeps paragraphs, list items and quotes readable."""
    if not value:
        return ""
    text = re.sub(r"<!--.*?-->", "", value, flags=re.DOTALL)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)<p(\s[^>]*)?>", "\n\n", text)
    text = re.sub(r"(?i)<li[^>]*>", "\n• ", text)
    text = re.sub(r"(?i)<blockquote[^>]*>", "\n> ", text)
    text = re.sub(r"(?i)</li>", "", text)
    text = re.sub(r"(?i)</(p|div|ul|ol|pre|blockquote|h[1-6]|tr|table)>", "\n\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text).replace("\u200b", "")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


_FEED_SECRET = re.compile(r"([?&](?:feed|token)=)[^&#\s;]+")


def redact_feed_tokens(text: str | None) -> str | None:
    """Mask private feed tokens (reddit.com/prefs/feeds) in URLs, identifiers and error text."""
    return _FEED_SECRET.sub(r"\1•••", text) if text else text


def normalize_ws(value: str | None) -> str:
    return _WS.sub(" ", value or "").strip()


def truncate(text: str | None, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut.rstrip(" ,.;:-") + "…"


def hours_between(a: datetime, b: datetime) -> float:
    return (a - b).total_seconds() / 3600.0


def safe_int(value: Any, default: int | None = None) -> int | None:
    try:
        if value is None or value == "":
            return default
        return int(float(str(value).replace(",", "")))
    except (TypeError, ValueError):
        return default
