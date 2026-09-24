from __future__ import annotations

import asyncio

from radar.clustering.embeddings import HashEmbedder
from radar.clustering.service import cluster_pending, embed_pending
from radar.db.models import Item, ItemSighting, Source
from radar.normalization.models import RawItem
from radar.ranking import build_feed, rescore
from radar.services.feedback import fold_events, record
from radar.services.ingest import ingest_items
from radar.services.topics import classify_items, seed_topics


def test_ingest_is_idempotent_and_preserves_sightings(db):
    source = Source(kind="rss", name="Test", identifier="https://example.com/feed", config={})
    db.add(source); db.flush()
    raw = RawItem(url="https://example.com/a?utm_source=test", title="A substantial local AI project launch",
                  content_type="article", text="local AI open source project " * 20)
    first = ingest_items(db, source, [raw])
    second = ingest_items(db, source, [raw])
    assert first.new == 1 and second.new == 0
    assert db.query(Item).count() == 1
    assert db.query(ItemSighting).one().seen_count == 2


def test_dedup_cluster_feed_and_learning(db):
    seed_topics(db)
    sources = [Source(kind="rss", name=f"Source {n}", identifier=f"https://s{n}.test/feed", config={})
               for n in range(3)]
    db.add_all(sources); db.flush()
    raws = [
        RawItem(url="https://a.test/news/agent", title="Acme launches a new local coding agent",
                content_type="article", text="Acme local coding agent runs privately on device. " * 18),
        RawItem(url="https://b.test/story/42", title="New local coding agent launched by Acme",
                content_type="article", text="Acme local coding agent runs privately on device. " * 18),
        RawItem(url="https://news.ycombinator.com/item?id=42", title="Acme launches a new local coding agent",
                content_type="hackernews_post", link_url="https://a.test/news/agent",
                text="Discussion of the newly released local coding agent. " * 10),
    ]
    items=[]
    for source, raw in zip(sources, raws, strict=True):
        stats=ingest_items(db, source, [raw]); items.append(db.get(Item, stats.new_item_ids[0]))
    classify_items(db, items)
    asyncio.run(embed_pending(db, HashEmbedder(), 10))
    assert cluster_pending(db, 10) == 3
    cluster_ids = {i.cluster_id for i in items}
    assert len(cluster_ids) == 1
    assert all(not i.duplicate_of for i in items)
    rescore(db)
    feed = build_feed(db, limit=10)
    assert len(feed) == 1  # one story card, while all raw items remain
    before = feed[0].item.score.interest
    record(db, items[0], "like"); assert fold_events(db) == 1
    rescore(db)
    assert items[0].score.interest >= before
