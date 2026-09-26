"""Nothing the reader does waits on Reddit's rate limit, the worker, or a picture download."""
from __future__ import annotations

import asyncio
import io
import sys
from datetime import timedelta

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from PIL import Image as PILImage

import radar.net
from radar.api.app import app
from radar.api.serializers import item_dict
from radar.collectors.reddit import RedditRateLimited, cooldown_remaining, reddit_rss, start_cooldown
from radar.config import get_config
from radar.db.models import CommentSummary, Item, Source
from radar.net import Http
from radar.services.collect import fetch_source
from radar.services.ingest import ingest_items
from radar.util import utcnow
from tests.test_reddit import _reddit_raw


@pytest.fixture(autouse=True)
def no_pacing(monkeypatch):
    monkeypatch.setattr(radar.net, "HOST_INTERVALS", {})
    monkeypatch.setattr(radar.net, "DEFAULT_HOST_INTERVAL", 0.0)


def _source(db, sub="linux"):
    source = Source(kind="reddit", name=f"Reddit · {sub}", identifier=sub, config={})
    db.add(source)
    db.flush()
    return source


def test_429_starts_a_shared_cooldown_and_the_source_defers_instead_of_failing(db):
    source = _source(db)
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(url__regex=r"https://(old|www)\.reddit\.com/.*").mock(
            return_value=httpx.Response(429, headers={"x-ratelimit-reset": "42"}))
        asyncio.run(fetch_source(db, source, Http(), get_config()))
        assert 30 < cooldown_remaining() <= 42
        assert source.consecutive_failures == 0 and source.last_error is None
        assert source.next_fetch_at > utcnow() + timedelta(seconds=30)
        calls = route.call_count
        with pytest.raises(RedditRateLimited):
            asyncio.run(reddit_rss(Http(), "https://www.reddit.com/r/linux/hot/.rss"))
        assert route.call_count == calls  # nothing is sent while the cooldown runs


def test_reader_gets_saved_comments_and_a_retry_time_while_reddit_cools_down(db):
    ingest_items(db, _source(db), [_reddit_raw("c1", "A post worth discussing", "linux", None, rank=0)])
    item = db.query(Item).one()
    db.add(CommentSummary(item_id=item.id, platform="reddit", sampled=1, state="fetched", summary={"via": "rss"},
                          fetched_at=utcnow() - timedelta(hours=2),
                          comments=[{"id": "x", "author": "a", "text": "hi", "depth": 0, "score": None,
                                     "created_at": None}]))
    db.commit()
    start_cooldown(90)
    with TestClient(app) as client:
        cached = client.get(f"/api/v1/items/{item.id}/comments", params={"cached": "true"}).json()
        live = client.get(f"/api/v1/items/{item.id}/comments").json()
    assert cached["comments"] and cached["stale"]
    assert live["retry_in"] >= 60 and live["comments"][0]["text"] == "hi" and live["error"] is None


def test_videos_and_pictures_show_before_the_worker_caches_anything(db):
    talk = _reddit_raw("v1", "Great talk on category theory", "programming", None, rank=0, text=" ")
    talk.link_url = "https://www.youtube.com/watch?v=Xr1gcdb_IvA"
    demo = _reddit_raw("v2", "My browser demo", "LocalLLaMA", None, rank=1)
    demo.meta |= {"media_url": "https://v.redd.it/25kx9y2448dh1", "media_type": "video"}
    demo.add_image("reddit_thumb", "https://external-preview.redd.it/abc.png?width=640&s=x")
    ingest_items(db, _source(db), [talk, demo])
    a = db.query(Item).filter(Item.source_item_id == "t3_v1").one()
    b = db.query(Item).filter(Item.source_item_id == "t3_v2").one()
    shown_a, shown_b = item_dict(db, a), item_dict(db, b)
    assert shown_a["media"] == {"kind": "youtube", "id": "Xr1gcdb_IvA"} and "i.ytimg.com" in shown_a["image_url"]
    assert a.extraction_state == "skipped"  # a watch page has nothing to extract
    assert ["youtube_thumb", "https://i.ytimg.com/vi/Xr1gcdb_IvA/maxresdefault.jpg"] in a.meta["image_candidates"]
    assert shown_b["media"]["kind"] == "reddit_video" and shown_b["media"]["hls"].endswith("/HLSPlaylist.m3u8")
    assert shown_b["image_url"] == f"/media/item/{b.id}"  # fetched on first view


def test_item_picture_is_downloaded_on_first_view_and_cached(db):
    raw = _reddit_raw("p1", "Look at my rack", "homelab", None, rank=0)
    raw.add_image("reddit_preview", "https://i.redd.it/rack.png")
    ingest_items(db, _source(db, "homelab"), [raw])
    item = db.query(Item).one()
    db.commit()
    png = io.BytesIO()
    PILImage.new("RGB", (800, 600), (120, 80, 200)).save(png, format="PNG")
    with TestClient(app) as client, respx.mock(assert_all_called=False) as mock:
        mock.route(host="testserver").pass_through()
        mock.get("https://i.redd.it/rack.png").mock(
            return_value=httpx.Response(200, content=png.getvalue(), headers={"content-type": "image/png"}))
        res = client.get(f"/media/item/{item.id}")
        again = client.get(f"/media/item/{item.id}")
    assert res.status_code == 200 and res.headers["content-type"] == "image/webp" and again.status_code == 200
    db.expire_all()
    item = db.get(Item, item.id)
    assert item.image_state == "done" and item_dict(db, item)["image"]["width"] == 800


def test_open_pages_learn_about_new_posts(db):
    ingest_items(db, _source(db), [_reddit_raw("n1", "New post one", "linux", None, rank=0),
                                   _reddit_raw("n2", "New post two", "linux", None, rank=1)])
    db.commit()
    before, after = utcnow() - timedelta(minutes=5), utcnow() + timedelta(minutes=1)
    with TestClient(app) as client:
        assert client.get("/api/v1/updates", params={"since": before.isoformat() + "Z", "scope": "reddit"}).json()["new"] == 2
        assert client.get("/api/v1/updates", params={"since": after.isoformat() + "Z", "scope": "reddit"}).json()["new"] == 0


def test_today_scores_new_arrivals_now_and_reranks_in_the_background(db, monkeypatch):
    requested = []
    api = sys.modules["radar.api.app"]  # the package re-exports `app`, which shadows the module name
    monkeypatch.setattr(api, "request_rescore", lambda *a, **k: requested.append((a, k)))
    ingest_items(db, _source(db), [_reddit_raw(f"t{i}", f"Fresh post number {i}", "linux", None, rank=i)
                                   for i in range(3)])
    db.commit()
    with TestClient(app) as client:
        digest = client.get("/api/v1/digest", params={"period": "7d"}).json()
        item_id = digest["sections"][0]["items"][0]["id"]
        assert digest["count"] == 3  # unscored arrivals are scored on the spot
        assert requested  # no full rescore has run yet: it is requested, not awaited
        requested.clear()
        client.post(f"/api/v1/items/{item_id}/feedback", json={"action": "like"})
        assert requested  # taste changed: every rank follows, in the background
