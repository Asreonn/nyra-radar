"""Connect a Reddit account without a password or an API app.

Every Reddit account has a private, read-only feed token (https://www.reddit.com/prefs/feeds).
The "your front page" link there looks like `https://www.reddit.com/.rss?feed=<token>&user=<name>`,
and the same token opens that user's upvoted and saved listings. Connecting creates three
ordinary `reddit` sources from it:

- front page  -> collects exactly the subreddits the user subscribes to;
- upvoted     -> every post is recorded as a `like`;
- saved       -> every post is saved in Radar and recorded as a `save`.

Likes and saves feed the topic interest model, per-source affinity and the embedding taste
profile (`semantic_preference`). Posts that only reached Radar because the user already
upvoted/saved them are marked `meta.reddit_account` and kept out of the feeds. The token only
lives in the source identifiers and is masked in every API response; disconnecting deletes it.
"""
from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from radar.db.models import Item, SavedItem, Source
from radar.services.feedback import fold_events, record
from radar.services.topics import classify_items
from radar.util import iso, redact_feed_tokens

# role -> (source name, path template, feedback signal, minutes between syncs)
ACCOUNT_FEEDS: dict[str, tuple[str, str, str | None, int]] = {
    "front": ("Reddit · Front page", "/.rss", None, 20),
    "upvoted": ("Reddit · Your upvotes", "/user/{user}/upvoted/.rss", "like", 60),
    "saved": ("Reddit · Your saved", "/user/{user}/saved/.rss", "save", 60),
}


def parse_feed_url(url: str) -> tuple[str, str]:
    """Return (username, token) from any private Reddit feed URL."""
    parts = urlsplit(url.strip())
    if not (parts.hostname or "").endswith("reddit.com"):
        raise ValueError("expected a reddit.com feed URL from reddit.com/prefs/feeds")
    query = dict(parse_qsl(parts.query))
    user, token = query.get("user", "").strip(), query.get("feed", "").strip()
    if not user or not token:
        raise ValueError("the URL needs both ?feed=… and &user=… (copy a private feed link from reddit.com/prefs/feeds)")
    return user, token


def feed_url(role: str, user: str, token: str) -> str:
    path = ACCOUNT_FEEDS[role][1].format(user=user)
    return f"https://www.reddit.com{path}?{urlencode({'feed': token, 'user': user})}"


def account_sources(session: Session) -> list[Source]:
    rows = session.scalars(select(Source).where(Source.kind == "reddit")).all()
    return [s for s in rows if (s.config or {}).get("account") in ACCOUNT_FEEDS]


def connect(session: Session, url: str) -> list[Source]:
    """Create (or re-point) the account sources. Returns them in ACCOUNT_FEEDS order."""
    user, token = parse_feed_url(url)
    existing = {(s.config or {}).get("account"): s for s in account_sources(session)}
    if "front" not in existing:
        # A private front-page feed added earlier by hand becomes the account's front page.
        manual = [s for s in session.scalars(select(Source).where(Source.kind == "reddit"))
                  if "feed=" in s.identifier and not (s.config or {}).get("account")]
        if manual:
            existing["front"] = manual[0]
    out: list[Source] = []
    for role, (name, _, signal, minutes) in ACCOUNT_FEEDS.items():
        identifier = feed_url(role, user, token)
        source = existing.get(role)
        if source is None:
            source = Source(kind="reddit", identifier=identifier, name=name, origin="manual")
            session.add(source)
        source.identifier = identifier
        source.name = name
        source.url = "https://www.reddit.com" if role == "front" else f"https://www.reddit.com/user/{user}/{role}"
        source.status = "active"
        source.interval_minutes = minutes
        source.next_fetch_at = None
        source.config = {"account": role, "account_user": user, "min_score": 0, "max_items": 100,
                         **({"account_signal": signal} if signal else {})}
        out.append(source)
    session.flush()
    return out


def disconnect(session: Session) -> int:
    rows = account_sources(session)
    for source in rows:
        session.delete(source)
    return len(rows)


def apply_account_signals(session: Session, source: Source, item_ids: list[int]) -> int:
    """Turn posts from the upvoted/saved listings into like/save feedback (idempotent)."""
    config = source.config or {}
    signal, role = config.get("account_signal"), config.get("account")
    if signal not in ("like", "save") or not item_ids:
        return 0
    items = session.scalars(select(Item).where(Item.id.in_(item_ids))).all()
    classify_items(session, items)  # topics must exist before the event snapshots them
    session.flush()
    saved = set(session.scalars(select(SavedItem.item_id).where(SavedItem.item_id.in_(item_ids))))
    applied = 0
    for item in items:
        meta = dict(item.meta or {})
        roles = set(meta.get("reddit_account") or [])
        if role in roles:
            continue
        item.meta = meta | {"reddit_account": sorted(roles | {role})}
        if signal == "like" and item.user_vote != 1:
            record(session, item, "like", {"via": "reddit_account", "listing": role})
            applied += 1
        elif signal == "save" and item.id not in saved:
            session.add(SavedItem(item_id=item.id, note="Saved on Reddit"))
            record(session, item, "save", {"via": "reddit_account", "listing": role})
            applied += 1
    if applied:
        fold_events(session)
    return applied


def status(session: Session) -> dict:
    rows = account_sources(session)
    if not rows:
        return {"connected": False, "user": None, "feeds": []}
    feeds = []
    for source in sorted(rows, key=lambda s: list(ACCOUNT_FEEDS).index(s.config["account"])):
        feeds.append({"role": source.config["account"], "name": source.name, "source_id": source.id,
                      "status": source.status, "items": source.item_count,
                      "last_success_at": iso(source.last_success_at),
                      "last_error": redact_feed_tokens(source.last_error),
                      "access": (source.state or {}).get("last_access")})
    marker = func.json_extract(Item.meta, "$.reddit_account")
    counts = {role: session.scalar(select(func.count(Item.id)).where(marker.like(f'%"{role}"%'))) or 0
              for role in ("upvoted", "saved")}
    return {"connected": True, "user": rows[0].config.get("account_user"), "feeds": feeds, "learned": counts}
