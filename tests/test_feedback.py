from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from radar.api.app import app
from radar.clustering.embeddings import HashEmbedder
from radar.clustering.service import embed_pending
from radar.db.models import Interest, Item, SourcePreference, Topic
from radar.normalization.models import CommunityInfo, RawItem
from radar.ranking import rescore
from radar.services.feedback import fold_events, record, undo_last
from radar.services.ingest import ingest_items
from radar.services.taste import profile, tune_candidates, why
from radar.services.topics import classify_items, seed_topics
from radar.util import utcnow


def _posts(db, source, specs):
    raws = []
    for pid, title, sub, text in specs:
        url = f"https://www.reddit.com/r/{sub}/comments/{pid}/x/"
        raws.append(RawItem(url=url, title=title, content_type="reddit_post", source_item_id=f"t3_{pid}",
                            text=text, published_at=utcnow(), meta={"subreddit": sub},
                            community=CommunityInfo(platform="reddit", post_id=pid, discussion_url=url,
                                                    community=sub, score=100, num_comments=10)))
    stats = ingest_items(db, source, raws)
    items = [db.get(Item, i) for i in stats.new_item_ids]
    classify_items(db, items)
    db.flush()
    return items


def _source(db, sub):
    from radar.db.models import Source
    source = Source(kind="reddit", name=f"Reddit · {sub}", identifier=sub, config={})
    db.add(source)
    db.flush()
    return source


def test_dislike_reasons_teach_different_things(db):
    seed_topics(db)
    llama, gossip = _source(db, "LocalLLaMA"), _source(db, "gossip")
    a, b = _posts(db, llama, [("a1", "Linux kernel release notes", "LocalLLaMA", "linux kernel " * 30),
                              ("a2", "Another linux kernel thread", "LocalLLaMA", "linux kernel " * 30)])
    (c,) = _posts(db, gossip, [("c1", "Celebrity drama thread", "gossip", "drama " * 30)])
    linux = db.query(Interest).join(Topic).filter(Topic.slug == "linux").one()
    before = linux.learned

    record(db, a, "dislike", {"reason": "source"})  # less from r/LocalLLaMA: not a topic judgement
    fold_events(db)
    assert db.get(Interest, linux.id).learned == before
    assert db.get(SourcePreference, llama.id).affinity < 1.0
    assert a.is_hidden and a.user_vote == 0  # hidden, but not a negative taste example

    record(db, b, "dislike", {"reason": "topic"})  # not interested in this kind of thing
    fold_events(db)
    assert db.get(Interest, linux.id).learned < before and b.user_vote == -1

    record(db, c, "dislike", {"reason": "seen"})  # already seen: hide, learn nothing
    fold_events(db)
    assert c.is_hidden and c.user_vote == 0 and db.get(SourcePreference, gossip.id) is None


def test_undo_reverses_state_and_learning(db):
    seed_topics(db)
    source = _source(db, "linux")
    (item,) = _posts(db, source, [("u1", "Linux desktop news", "linux", "linux desktop " * 30)])
    linux = db.query(Interest).join(Topic).filter(Topic.slug == "linux").one()
    before = linux.learned
    record(db, item, "like")
    fold_events(db)
    assert db.get(Interest, linux.id).learned > before and item.user_vote == 1
    undo_last(db, item)
    fold_events(db)
    assert abs(db.get(Interest, linux.id).learned - before) < 1e-9 and item.user_vote == 0
    assert db.get(SourcePreference, source.id).likes == 0
    record(db, item, "skip")
    assert item.is_hidden
    undo_last(db, item)
    assert not item.is_hidden
    assert undo_last(db, item) is None or item.user_vote == 0


def test_disliked_posts_push_similar_ones_down(db):
    seed_topics(db)
    source = _source(db, "mixed")
    rated = _posts(db, source, [
        (f"d{n}", f"Crypto token pump number {n}", "mixed", "crypto token pump coin price moon " * 8)
        for n in range(3)])
    fresh = _posts(db, source, [
        ("f1", "New crypto token pump", "mixed", "crypto token pump coin price moon " * 8),
        ("f2", "Rust compiler internals", "mixed", "rust compiler borrow checker lifetimes " * 8),
    ])
    for item in rated:
        record(db, item, "dislike", {"reason": "topic"})
    fold_events(db)
    asyncio.run(embed_pending(db, HashEmbedder(), 50))
    rescore(db)
    crypto, rust = (x.score.components["semantic_preference"] for x in fresh)
    assert crypto < 0.5 < rust or crypto < rust
    assert "Similar to things you asked to see less of" in why(db, fresh[0])


def test_tune_is_diverse_and_profile_is_readable(db):
    seed_topics(db)
    subs = [_source(db, name) for name in ("one", "two", "three")]
    for n, source in enumerate(subs):
        _posts(db, source, [(f"{source.identifier}{k}", f"Post {k} in {source.identifier} about things",
                             source.identifier, f"body {n} {k} " * 20) for k in range(5)])
    rescore(db)
    picked = tune_candidates(db, limit=3)
    assert len({p.source_id for p in picked}) == 3
    record(db, picked[0], "like")
    record(db, picked[1], "dislike", {"reason": "source"})
    fold_events(db)
    summary = profile(db)
    assert summary["counts"]["likes"] == 1 and summary["counts"]["ratings"] == 2
    assert summary["less_from"] and summary["less_from"][0].startswith("r/")


def test_feedback_api_undo_and_refine(db):
    seed_topics(db)
    source = _source(db, "linux")
    (item,) = _posts(db, source, [("r1", "Linux kernel thing", "linux", "linux kernel " * 30)])
    rescore(db)
    db.commit()
    with TestClient(app) as client:
        url = f"/api/v1/items/{item.id}/feedback"
        assert client.post(url, json={"action": "dislike"}).json()["user"]["hidden"] is True
        refined = client.post(url, json={"action": "dislike", "context": {"reason": "seen", "refine": True}}).json()
        assert refined["user"]["vote"] == 0 and refined["user"]["hidden"] is True
        undone = client.post(url, json={"action": "undo"}).json()
        assert undone["user"]["hidden"] is False
        assert client.post(url, json={"action": "dislike", "context": {"reason": "nope"}}).status_code == 422
        detail = client.get(f"/api/v1/items/{item.id}").json()["item"]
        assert isinstance(detail["why"], list) and detail["user"]["saved"] is False
        assert client.get("/api/v1/tune").json()["items"]
        assert "counts" in client.get("/api/v1/profile").json()
