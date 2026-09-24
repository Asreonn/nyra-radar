"""Candidate-only summarization with input hashes and graceful local fallback."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from radar.db.models import Enrichment, Item, ItemScore, ModelUsage, Repository
from radar.llm.client import LLMUnavailable, LocalLLM
from radar.util import sha1_hex, truncate

SYSTEM = """You are the analysis layer for a personal technology intelligence radar.
Return factual compact JSON. Never invent facts, metrics, licenses, or capabilities. Distinguish evidence
from inference. The reader is a technical expert. Avoid hype and generic praise."""

SCHEMA = {"type": "object", "properties": {
    "summary": {"type": "string"}, "why_it_matters": {"type": "string"},
    "key_points": {"type": "array", "items": {"type": "string"}, "maxItems": 4},
    "risks": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
    "quality": {"type": "number"}}, "required": ["summary", "why_it_matters", "key_points", "risks"]}


async def enrich_candidates(session: Session, llm: LocalLLM, limit: int = 12) -> dict[str, int]:
    rows = session.execute(
        select(Item, ItemScore).join(ItemScore, ItemScore.item_id == Item.id)
        .where(ItemScore.is_candidate.is_(True), Item.summary_state.in_(("none", "stale", "failed")))
        .order_by(ItemScore.final_score.desc()).limit(limit)
    ).all()
    done = 0
    failed = 0
    for item, score in rows:
        repo = session.get(Repository, item.repository_id) if item.repository_id else None
        body = truncate(item.clean_text or item.excerpt, 7000)
        facts = {"title": item.title, "url": item.canonical_url, "type": item.content_type,
                 "source": item.source.name if item.source else None, "published": str(item.published_at or ""),
                 "community_score": item.external_score, "comments": item.external_comment_count}
        if repo:
            facts["repository"] = {"name": repo.display_name, "description": repo.description,
                                   "stars": repo.stars, "forks": repo.forks, "language": repo.language,
                                   "license": repo.license, "topics": repo.topics,
                                   "stars_delta_7d": repo.stars_delta_7d, "mentions_7d": repo.mentions_7d}
            body = truncate(f"{body}\nREADME:\n{repo.readme_excerpt or ''}", 9000)
        digest = sha1_hex(str(facts) + body)
        existing = session.scalar(select(Enrichment).where(Enrichment.item_id == item.id,
                                                            Enrichment.kind == "analysis"))
        if existing and existing.input_hash == digest:
            item.summary_state = "done"
            continue
        try:
            data, usage = await llm.json(SYSTEM, f"FACTS:\n{facts}\n\nCONTENT:\n{body}", SCHEMA)
            if existing is None:
                existing = Enrichment(item_id=item.id, kind="analysis", input_hash=digest)
                session.add(existing)
            existing.data = data
            existing.model = llm.model
            existing.input_hash = digest
            item.summary = truncate(data.get("summary"), 1000)
            item.meta = dict(item.meta or {}) | {"why_it_matters": data.get("why_it_matters"),
                                                 "llm_quality": max(0.0, min(1.0, float(data.get("quality", .5))))}
            item.summary_state = "done"
            session.add(ModelUsage(provider=llm.provider, model=llm.model, purpose="item_analysis", ok=True,
                                   **usage))
            done += 1
        except LLMUnavailable as exc:
            item.summary_state = "failed"
            session.add(ModelUsage(provider=llm.provider, model=llm.model or "unknown",
                                   purpose="item_analysis", ok=False, error=str(exc)[:1000]))
            failed += 1
            break  # endpoint failure should not hammer the local server for every row
        session.commit()
    return {"attempted": done + failed, "done": done, "failed": failed}
