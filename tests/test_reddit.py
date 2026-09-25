from __future__ import annotations

import asyncio
import html

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

import radar.net
from radar.api.app import app
from radar.clustering.embeddings import HashEmbedder
from radar.clustering.service import embed_pending
from radar.collectors.base import SourceSpec
from radar.collectors.feedutil import parse_feed
from radar.collectors.reddit import RedditCollector, rss_entry_to_raw
from radar.config import Config
from radar.db.models import CommunityPost, Item, SavedItem, Source
from radar.net import Http
from radar.normalization.models import CommunityInfo, RawItem
from radar.ranking import build_feed, rescore
from radar.services import reddit_account
from radar.services.community import community_feed, normalize_subreddit
from radar.services.ingest import ingest_items
from radar.services.topics import seed_topics
from radar.util import html_to_text, redact_feed_tokens, utcnow


def _entry(pid: str, title: str, content: str, sub: str = "LocalLLaMA", thumb: str | None = None,
           kind: str = "t3") -> str:
    link = f"https://www.reddit.com/r/{sub}/comments/{pid}/slug/"
    media = f'<media:thumbnail url="{thumb}" />' if thumb else ""
    return (f'<entry><author><name>/u/alice</name></author><category term="{sub}" label="r/{sub}"/>'
            f'<content type="html">{html.escape(content)}</content><id>{kind}_{pid}</id>{media}'
            f'<link href="{link}" /><updated>2026-09-25T10:00:00+00:00</updated>'
            f'<published>2026-09-25T10:00:00+00:00</published><title>{html.escape(title)}</title></entry>')


def atom(*entries: str) -> bytes:
    return ('<?xml version="1.0" encoding="UTF-8"?><feed xmlns="http://www.w3.org/2005/Atom" '
            'xmlns:media="http://search.yahoo.com/mrss/"><title>r</title>' + "".join(entries) + "</feed>").encode()


SELF = ('<!-- SC_OFF --><div class="md"><p>First paragraph with <a href="https://github.com/acme/tool">a repo</a>.</p>'
        '<ul><li>one</li><li>two</li></ul></div><!-- SC_ON --> submitted by <a href="https://www.reddit.com/user/alice">'
        ' /u/alice </a> <span><a href="https://www.reddit.com/r/LocalLLaMA/comments/abc111/slug/">[link]</a></span>')
LINK = ('<table><tr><td> submitted by /u/bob <span><a href="https://example.com/post">[link]</a></span> '
        '<span><a href="https://www.reddit.com/r/LocalLLaMA/comments/abc222/slug/">[comments]</a></span></td></tr></table>')
MEDIA = '<span><a href="https://i.redd.it/pic.png">[link]</a></span>'
FEED = atom(
    _entry("pin000", "[Bi-Weekly Megathread] Project Showcase", SELF.replace("abc111", "pin000")),
    _entry("abc111", "A local model beats the API", SELF),
    _entry("abc222", "Blog post about inference", LINK, thumb="https://external-preview.redd.it/x.jpeg?width=640"),
    _entry("abc333", "My rig", MEDIA),
)


@pytest.fixture(autouse=True)
def no_pacing(monkeypatch):
    monkeypatch.setattr(radar.net, "HOST_INTERVALS", {})
    monkeypatch.setattr(radar.net, "DEFAULT_HOST_INTERVAL", 0.0)


def test_rss_entries_parse_self_link_and_media_posts():
    entries = parse_feed(FEED).entries
    self_post, link_post, media_post = (rss_entry_to_raw(e) for e in entries[1:])
    assert self_post.community.post_kind == "self" and self_post.link_url is None
    assert self_post.source_item_id == "t3_abc111" and self_post.author == "alice"
    assert self_post.community.score is None  # Atom has no votes: unknown, not zero
    assert "https://github.com/acme/tool" in self_post.links
    assert "First paragraph" in self_post.text and "• one\n• two" in self_post.text
    assert link_post.community.post_kind == "link" and link_post.link_url == "https://example.com/post"
    assert link_post.image_candidates[0][0] == "reddit_thumb"
    assert media_post.community.post_kind == "media" and media_post.image_candidates[0][1].startswith("https://i.redd.it")
    comment = parse_feed(atom(_entry("c1", "/u/x on post", "<p>hi</p>", kind="t1"))).entries[0]
    assert rss_entry_to_raw(comment) is None  # saved comments are not posts


def test_blocked_json_falls_back_to_www_rss_and_is_remembered():
    async def run():
        http = Http()
        collector = RedditCollector(http, Config())
        spec = SourceSpec(id=1, kind="reddit", identifier="r/LocalLLaMA", name="x", config={"sorts": ["hot"]})
        with respx.mock(assert_all_called=False) as mock:
            json_route = mock.get(url__regex=r"https://old\.reddit\.com/r/LocalLLaMA/hot\.json.*").mock(
                return_value=httpx.Response(302, headers={"Location": "https://old.reddit.com/login/?dest=x"}))
            mock.get(url__startswith="https://old.reddit.com/login").mock(
                return_value=httpx.Response(200, text="<html>login</html>"))
            rss_route = mock.get(url__regex=r"https://www\.reddit\.com/r/LocalLLaMA/hot/\.rss.*").mock(
                return_value=httpx.Response(200, content=FEED, headers={"content-type": "application/atom+xml"}))
            first = await collector.fetch(spec)
            spec.state = first.state
            second = await collector.fetch(spec)
            await http.close()
        return first, second, json_route.call_count, rss_route.call_count, rss_route.calls[0].request

    first, second, json_calls, rss_calls, request = asyncio.run(run())
    assert [r.title for r in first.items] == ["A local model beats the API", "Blog post about inference", "My rig"]
    assert [r.meta["reddit_rank"] for r in first.items] == [0, 1, 2]  # pinned megathread dropped
    assert first.state["last_access"] == "rss" and first.state["json_blocked_until"]
    assert json_calls == 1 and rss_calls == 2  # the second fetch skipped the blocked JSON endpoint
    assert request.headers["user-agent"].startswith("linux:nyra-radar")
    assert len(second.items) == 3


def _reddit_raw(pid: str, title: str, sub: str, score: int | None, rank: int | None = None,
                text: str = "") -> RawItem:
    url = f"https://www.reddit.com/r/{sub}/comments/{pid}/x/"
    meta = {"subreddit": sub, "via": "json" if score is not None else "rss"}
    if rank is not None:
        meta |= {"reddit_sort": "hot", "reddit_rank": rank, "reddit_listing_size": 25}
    return RawItem(url=url, title=title, content_type="reddit_post", source_item_id=f"t3_{pid}",
                   text=text or f"{title} discussion body " * 5, published_at=utcnow(),
                   external_score=score, meta=meta,
                   community=CommunityInfo(platform="reddit", post_id=pid, discussion_url=url, community=sub,
                                           score=score, num_comments=None if score is None else 12,
                                           is_self=True, post_kind="self"))


def test_rss_sighting_keeps_known_votes(db):
    source = Source(kind="reddit", name="r", identifier="LocalLLaMA", config={})
    db.add(source); db.flush()
    ingest_items(db, source, [_reddit_raw("p1", "Post with votes from the API", "LocalLLaMA", 250)])
    ingest_items(db, source, [_reddit_raw("p1", "Post with votes from the API", "LocalLLaMA", None, rank=0)])
    post = db.query(CommunityPost).one()
    item = db.query(Item).one()
    assert post.score == 250 and post.num_comments == 12
    assert item.meta["reddit_rank"] == 0


def test_feed_leads_with_reddit_and_caps_one_subreddit(db):
    seed_topics(db)
    reddit = Source(kind="reddit", name="r", identifier="LocalLLaMA", config={})
    other = Source(kind="rss", name="News", identifier="https://news.test/feed", config={})
    db.add_all([reddit, other]); db.flush()
    raws = [_reddit_raw(f"a{n}", f"Big LocalLLaMA thread number {n} about models", "LocalLLaMA", None, rank=n)
            for n in range(10)]
    raws += [_reddit_raw(f"b{n}", f"Selfhosted thread number {n} about servers", "selfhosted", None, rank=n)
             for n in range(4)]
    ingest_items(db, reddit, raws)
    ingest_items(db, other, [RawItem(url=f"https://news.test/{n}", title=f"Regular news story number {n} here",
                                     content_type="rss_entry", text="news " * 40) for n in range(10)])
    rescore(db)
    top = db.query(Item).filter(Item.source_item_id == "t3_a0").one().score.components["community_signal"]
    tail = db.query(Item).filter(Item.source_item_id == "t3_a9").one().score.components["community_signal"]
    assert top > tail  # listing position is the engagement signal for Atom posts
    feed = build_feed(db, limit=20)
    reddit_section = [e for e in feed if e.section == "reddit"]
    assert reddit_section and feed[0].item.content_type == "reddit_post"
    ranked = [e for e in feed if e.reason != "fresh discovery"]  # the final top-up ignores caps by design
    subs = [db.get(CommunityPost, e.item.id).community for e in ranked if e.item.content_type == "reddit_post"]
    assert subs.count("LocalLLaMA") <= 2 and "selfhosted" in subs  # per-subreddit cap = max(2, 8% of 20)


def test_community_feed_sorts_and_filters(db):
    source = Source(kind="reddit", name="r", identifier="rust", config={})
    db.add(source); db.flush()
    ingest_items(db, source, [_reddit_raw("r1", "Rust post with many votes", "rust", 900),
                              _reddit_raw("r2", "Rust post with few votes", "rust", 20),
                              _reddit_raw("g1", "Golang post with votes", "golang", 400)])
    rescore(db)
    top = community_feed(db, community="rust", sort="top")
    assert [i.source_item_id for i in top] == ["t3_r1", "t3_r2"]
    assert len(community_feed(db, community="rust,golang", sort="new")) == 3
    source.status = "muted"; db.flush()  # a muted source's collected posts leave the feeds too
    assert community_feed(db, community="rust,golang", sort="new") == []
    assert not [e for e in build_feed(db, limit=20) if e.item.source_id == source.id]
    source.status = "active"; db.flush()
    assert normalize_subreddit("https://www.reddit.com/r/linux/top/") == "linux"
    assert normalize_subreddit("r/rust+golang") == "rust+golang"
    assert normalize_subreddit("not a sub!") is None
    assert normalize_subreddit("https://www.reddit.com/.rss?feed=abc&user=me").startswith("https://www.reddit.com/.rss")


def test_reddit_api_surface(db):
    source = Source(kind="reddit", name="Reddit · rust", identifier="rust", config={})
    db.add(source); db.flush()
    ingest_items(db, source, [_reddit_raw("r1", "Rust post with many votes", "rust", 900)])
    rescore(db); db.commit()
    thread = atom(_entry("r1", "Rust post", "<p>post</p>", sub="rust"),
                  _entry("c1", "/u/bob on Rust post", '<div class="md"><p>Great <b>post</b></p></div>', sub="rust",
                         kind="t1"))
    with TestClient(app) as client, respx.mock(assert_all_called=False) as mock:
        mock.route(host="127.0.0.1").pass_through()
        mock.get(url__startswith="https://old.reddit.com").mock(return_value=httpx.Response(403))
        mock.get(url__regex=r"https://www\.reddit\.com/r/rust/comments/r1/\.rss.*").mock(
            return_value=httpx.Response(200, content=thread, headers={"content-type": "application/atom+xml"}))
        subs = client.get("/api/v1/communities").json()["communities"]
        assert subs[0]["name"] == "rust" and subs[0]["items"] == 1
        feed = client.get("/api/v1/community", params={"community": "rust", "sort": "hot"}).json()
        item = feed["items"][0]
        assert item["community"]["name"] == "rust" and item["community"]["score"] == 900
        comments = client.get(f"/api/v1/items/{item['id']}/comments").json()
        assert comments["via"] == "rss" and comments["comments"][0]["text"] == "Great post"
        added = client.post("/api/v1/sources", json={"kind": "reddit", "identifier": "https://reddit.com/r/Golang/"})
        assert added.status_code == 200
        dup = client.post("/api/v1/sources", json={"kind": "reddit", "identifier": "golang"})
        assert dup.status_code == 409
        bad = client.post("/api/v1/reddit/account", json={"feed_url": "https://example.com/feed"})
        assert bad.status_code == 422


def test_account_connect_signals_and_taste_profile(db):
    seed_topics(db)
    front = "https://www.reddit.com/.rss?feed=SECRETTOKEN&user=alice"
    sources = reddit_account.connect(db, front)
    assert [s.config["account"] for s in sources] == ["front", "upvoted", "saved", "downvoted", "hidden"]
    assert "SECRETTOKEN" in sources[1].identifier and "/user/alice/upvoted/" in sources[1].identifier
    assert redact_feed_tokens(sources[1].identifier).count("SECRETTOKEN") == 0
    subscriptions = Source(kind="reddit", name="Reddit · LocalLLaMA", identifier="LocalLLaMA", config={})
    db.add(subscriptions); db.flush()
    liked_text = "local llama inference quantization gguf model benchmark "
    upvoted = [_reddit_raw(f"u{n}", f"Quantized local model benchmark {n}", "LocalLLaMA", 50,
                           text=liked_text * 6) for n in range(4)]
    stats = ingest_items(db, sources[1], upvoted)
    assert reddit_account.apply_account_signals(db, sources[1], stats.touched_item_ids) == 4
    assert reddit_account.apply_account_signals(db, sources[1], stats.touched_item_ids) == 0  # idempotent
    saved = ingest_items(db, sources[2], [_reddit_raw("s1", "Saved homelab rack build", "homelab", 10)])
    reddit_account.apply_account_signals(db, sources[2], saved.touched_item_ids)
    assert db.query(SavedItem).count() == 1
    down = ingest_items(db, sources[3], [_reddit_raw("d1", "Crypto pump signals group", "CryptoMoonShots", 5)])
    assert reddit_account.apply_account_signals(db, sources[3], down.touched_item_ids) == 1
    assert db.get(Item, down.touched_item_ids[0]).user_vote == -1
    hid = ingest_items(db, sources[4], [_reddit_raw("h1", "Celebrity gossip thread", "popculture", 5)])
    assert reddit_account.apply_account_signals(db, sources[4], hid.touched_item_ids) == 1
    assert db.get(Item, hid.touched_item_ids[0]).is_hidden
    fresh = ingest_items(db, subscriptions, [
        _reddit_raw("n1", "New quantized local model benchmark", "LocalLLaMA", 40, text=liked_text * 6),
        _reddit_raw("n2", "Gardening tips for tomatoes", "LocalLLaMA", 40, text="soil water tomato garden " * 12),
    ])
    asyncio.run(embed_pending(db, HashEmbedder(), 50))
    rescore(db)
    similar, unrelated = (db.get(Item, i).score.components["semantic_preference"] for i in fresh.new_item_ids)
    assert similar > unrelated and similar > 0.5
    shown = {e.item.id for e in build_feed(db, limit=20)}
    assert shown.isdisjoint(stats.touched_item_ids)  # already upvoted on Reddit: not shown again
    status = reddit_account.status(db)
    assert status["connected"] and status["user"] == "alice" and status["learned"] == {"upvoted": 4, "saved": 1, "downvoted": 1, "hidden": 1}
    assert reddit_account.disconnect(db) == 5


def test_html_to_text_keeps_structure():
    text = html_to_text("<p>Hello &amp; welcome</p><p>Second<br>line</p><ul><li>a</li><li>b</li></ul>")
    assert text == "Hello & welcome\n\nSecond\nline\n\n• a\n• b"


def test_feed_token_survives_extra_params_and_is_redacted_in_logs():
    import logging
    from radar.cli import _redact_log_record
    from radar.net import merge_query
    url, params = merge_query("https://www.reddit.com/user/a/saved/.rss?feed=SECRETTOKEN&user=a", {"limit": 100})
    assert params is None and "feed=SECRETTOKEN" in url and "user=a" in url and "limit=100" in url
    assert merge_query("https://www.reddit.com/r/rust/hot/.rss", {"limit": 5}) == ("https://www.reddit.com/r/rust/hot/.rss", {"limit": 5})
    record = logging.LogRecord("httpx", logging.INFO, "", 0, "HTTP Request: GET %s", (url,), None)
    _redact_log_record(record)
    assert "SECRETTOKEN" not in record.getMessage()
