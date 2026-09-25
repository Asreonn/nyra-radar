from __future__ import annotations

import asyncio

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

import radar.net
from radar.api.app import app
from radar.collectors.feedutil import parse_feed
from radar.collectors.reddit import rss_entry_to_raw
from radar.db.models import Item, Source
from radar.enrichment.comments import refresh_post
from radar.ranking import build_feed, rescore
from radar.services import brief as brief_mod
from radar.services.catalog import catalog
from radar.services.community import keep_reading
from radar.services.ingest import ingest_items
from tests.test_reddit import _entry, _reddit_raw, atom

INLINE = ('<!-- SC_OFF --><div class="md"><p>My rack:</p><p><a href="https://preview.redd.it/abc123xyz.png?width=640'
          '&amp;format=png&amp;auto=webp&amp;s=deadbeef">https://preview.redd.it/abc123xyz.png?width=640&amp;s=deadbeef</a>'
          '</p><p>Thoughts?</p></div><!-- SC_ON --> <span><a href="https://www.reddit.com/r/homelab/comments/p1/x/">[link]'
          '</a></span>')


@pytest.fixture(autouse=True)
def no_pacing(monkeypatch):
    monkeypatch.setattr(radar.net, "HOST_INTERVALS", {})
    monkeypatch.setattr(radar.net, "DEFAULT_HOST_INTERVAL", 0.0)


def test_text_post_images_stay_in_place_and_become_candidates():
    raw = rss_entry_to_raw(parse_feed(atom(_entry("p1", "My rack", INLINE, sub="homelab"))).entries[0])
    assert raw.text == "My rack:\n\nhttps://i.redd.it/abc123xyz.png\n\nThoughts?"
    assert raw.meta["inline_images"] == ["https://i.redd.it/abc123xyz.png"]
    assert ("reddit_preview", "https://i.redd.it/abc123xyz.png") in raw.image_candidates


def test_thread_copy_completes_a_short_listing_post(db):
    source = Source(kind="reddit", name="r", identifier="homelab", config={})
    db.add(source); db.flush()
    short = _reddit_raw("p1", "My rack", "homelab", None, rank=0, text="My rack:")
    ingest_items(db, source, [short])
    item = db.query(Item).one()
    item.image_state = "none"
    full = rss_entry_to_raw(parse_feed(atom(_entry("p1", "My rack", INLINE, sub="homelab"))).entries[0])
    assert refresh_post(item, full)
    assert "Thoughts?" in item.raw_text and item.meta["inline_images"]
    assert item.image_state == "pending"  # the inline picture re-opened the image stage
    assert not refresh_post(item, full)  # idempotent


def test_catalog_follow_state_and_feed_exclusions(db):
    db.add(Source(kind="youtube", name="YouTube · Fireship", identifier="UCsBjURrPoezykLs9EqgamOA", config={}))
    db.flush()
    groups = catalog(db)
    fireship = next(i for g in groups for i in g["items"] if i["identifier"] == "UCsBjURrPoezykLs9EqgamOA")
    assert fireship["following"] and len(groups) >= 6
    source = Source(kind="reddit", name="r", identifier="linux", config={})
    db.add(source); db.flush()
    ingest_items(db, source, [_reddit_raw("k1", "Normal linux post here", "linux", 50),
                              _reddit_raw("k2", "Old post found through search", "linux", 50)])
    found = db.query(Item).filter(Item.source_item_id == "t3_k2").one()
    found.meta = {**found.meta, "via_search": True}
    rescore(db)
    assert [i.source_item_id for i in keep_reading(db)] == ["t3_k1"]
    assert found.id not in {e.item.id for e in build_feed(db, limit=10)}


def test_live_search_ingests_but_keeps_results_out_of_feeds(db):
    feed = atom(_entry("s1", "Search hit about local llm", '<!-- SC_OFF --><div class="md"><p>body</p></div><!-- SC_ON -->',
                       sub="LocalLLaMA"))
    with TestClient(app) as client, respx.mock(assert_all_called=False) as mock:
        mock.route(host="127.0.0.1").pass_through()
        mock.get(url__regex=r"https://www\.reddit\.com/search/\.rss.*").mock(
            return_value=httpx.Response(200, content=feed, headers={"content-type": "application/atom+xml"}))
        res = client.get("/api/v1/search/live", params={"q": "local llm", "where": "reddit"}).json()
        assert res["items"][0]["title"] == "Search hit about local llm"
        assert res["items"][0]["community"]["name"] == "LocalLLaMA"
        assert client.get("/media/remote", params={"u": "https://evil.example/x.png"}).status_code == 404
    item = db.query(Item).filter(Item.title == "Search hit about local llm").one()
    db.refresh(item)
    assert item.meta["via_search"] is True


def test_brief_uses_local_model_and_caches(db, monkeypatch):
    source = Source(kind="reddit", name="r", identifier="linux", config={})
    db.add(source); db.flush()
    ingest_items(db, source, [_reddit_raw("b1", "Kernel 7.2 released", "linux", 50)])
    item = db.query(Item).one()
    calls = []

    async def fake_json(self, system, prompt, schema=None):
        calls.append(prompt)
        self.model, self.provider = "fake", "ollama"
        return {"tldr": "Kernel 7.2 is out.", "key_points": ["faster"], "discussion": "", "discussion_points": []}, {}

    class Idle:
        reason = "desktop active"

    monkeypatch.setattr(brief_mod.LocalLLM, "json", fake_json)
    monkeypatch.setattr(brief_mod.ResourceGate, "inspect", lambda self: Idle())
    first = asyncio.run(brief_mod.make_brief(db, brief_mod.Config(), item))
    second = asyncio.run(brief_mod.make_brief(db, brief_mod.Config(), item))
    assert first["tldr"] == "Kernel 7.2 is out." and second["fresh"] and len(calls) == 1

    class Busy:
        reason = "GPU busy, desktop active"

    monkeypatch.setattr(brief_mod.ResourceGate, "inspect", lambda self: Busy())
    with pytest.raises(brief_mod.BriefUnavailable):
        asyncio.run(brief_mod.make_brief(db, brief_mod.Config(), item, refresh=True))
