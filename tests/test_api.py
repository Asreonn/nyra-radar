from __future__ import annotations

from fastapi.testclient import TestClient

from radar.api.app import app
from radar.db.models import Item, Repository, Source
from radar.extraction.canonical_url import url_hash
from radar.ranking import rescore
from radar.services.search import index_items
from radar.util import utcnow


def seed(db):
    source=Source(kind="rss",name="Fixture",identifier="https://fixture.test/feed",config={})
    db.add(source); db.flush()
    item=Item(canonical_url="https://fixture.test/local-agent",url_hash=url_hash("https://fixture.test/local-agent"),
              source_id=source.id,content_type="github_repository",title="fixture/local-agent",
              clean_text="A local coding agent for private development",excerpt="Local coding agent",
              first_seen_at=utcnow(),published_at=utcnow(),domain="github.com")
    db.add(item); db.flush()
    repo=Repository(full_name="fixture/local-agent",display_name="fixture/local-agent",owner="fixture",
                    name="local-agent",item_id=item.id,description="A local coding agent",stars=120,
                    topics=["agents"],readme_images=[],readme_links=[],external_stars_delta={})
    db.add(repo); db.flush(); item.repository_id=repo.id
    rescore(db); index_items(db, [item.id]); db.commit()
    return item,repo


def test_nyra_api_contract(db):
    item,repo=seed(db)
    with TestClient(app) as client:
        assert client.get("/api/v1/health").json()["status"] == "ok"
        digest=client.get("/api/v1/digest",params={"period":"24h"}).json()
        assert {"digest_id","sections","count"} <= digest.keys()
        search=client.get("/api/v1/search",params={"q":"local coding agent"}).json()
        assert search["count"] == 1
        project=client.get(f"/api/v1/projects/{repo.id}").json()["project"]
        assert project["repository"]["full_name"] == "fixture/local-agent"
        related=client.get(f"/api/v1/items/{item.id}/related").json()
        assert "related" in related
        feedback=client.post(f"/api/v1/items/{item.id}/feedback",json={"action":"like"}).json()
        assert feedback["ok"] and feedback["user"]["vote"] == 1
