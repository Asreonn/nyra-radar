"""On-demand briefs: a TL;DR of a post or article plus what its discussion says, from the local
model. Cached per item (`enrichments.kind = "brief"`) and keyed by an input hash, so a brief is only
recomputed when the post, the linked text or the loaded comments change.

A brief is something the user asked for, so only hardware limits block it (RAM/VRAM, busy GPU,
Nyra's own llama-server); "desktop active" and the interactive cooldown do not.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from radar.config import Config
from radar.db.models import CommentSummary, Enrichment, Item, ModelUsage
from radar.llm.client import LLMUnavailable, LocalLLM
from radar.scheduler.resources import ResourceGate
from radar.util import iso, sha1_hex, truncate, utcnow

SYSTEM = """You summarise posts, articles and their discussions for a busy technical reader.
Be factual and compact. Use only the provided text; never invent facts, numbers or quotes.
If there are no comments, return an empty discussion and empty discussion_points.
Write in the language of the post."""

SCHEMA = {"type": "object", "properties": {
    "tldr": {"type": "string"},
    "key_points": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
    "discussion": {"type": "string"},
    "discussion_points": {"type": "array", "items": {"type": "string"}, "maxItems": 4},
}, "required": ["tldr", "key_points", "discussion", "discussion_points"]}

NON_BLOCKING = {"desktop active", "interactive LLM cooldown", "resources available"}


class BriefUnavailable(RuntimeError):
    pass


def _inputs(session: Session, item: Item) -> str:
    thread = session.scalar(select(CommentSummary).where(CommentSummary.item_id == item.id))
    comments = "\n".join(f"- {c.get('author') or 'someone'}: {truncate(c.get('text') or '', 400)}"
                         for c in (thread.comments if thread else [])[:30])
    return (f"TITLE: {item.title}\nSOURCE: {item.source.name if item.source else item.domain}\n\n"
            f"TEXT:\n{truncate(item.clean_text or item.excerpt or '', 7000)}\n\nCOMMENTS:\n{comments or '(none)'}")


def cached_brief(session: Session, item: Item) -> dict | None:
    row = session.scalar(select(Enrichment).where(Enrichment.item_id == item.id, Enrichment.kind == "brief"))
    if row is None:
        return None
    return {**row.data, "model": row.model, "created_at": iso(row.created_at),
            "fresh": row.input_hash == sha1_hex(_inputs(session, item))}


async def make_brief(session: Session, config: Config, item: Item, *, refresh: bool = False) -> dict:
    prompt = _inputs(session, item)
    digest = sha1_hex(prompt)
    row = session.scalar(select(Enrichment).where(Enrichment.item_id == item.id, Enrichment.kind == "brief"))
    if row is not None and row.input_hash == digest and not refresh:
        return {**row.data, "model": row.model, "created_at": iso(row.created_at), "fresh": True}
    state = ResourceGate(config).inspect()
    blocking = [r for r in state.reason.split(", ") if r not in NON_BLOCKING]
    if blocking:
        raise BriefUnavailable(f"The local model is busy ({', '.join(blocking)}). Try again in a minute.")
    llm = LocalLLM(config)
    try:
        data, usage = await llm.json(SYSTEM, prompt, SCHEMA)
    except LLMUnavailable as exc:
        session.add(ModelUsage(provider=llm.provider, model=llm.model or "unknown", purpose="brief", ok=False,
                               error=str(exc)[:1000]))
        raise BriefUnavailable(f"No local model available ({exc}).") from exc
    data = {"tldr": str(data.get("tldr") or ""), "key_points": [str(x) for x in data.get("key_points") or []][:5],
            "discussion": str(data.get("discussion") or ""),
            "discussion_points": [str(x) for x in data.get("discussion_points") or []][:4]}
    if row is None:
        row = Enrichment(item_id=item.id, kind="brief", input_hash=digest)
        session.add(row)
    row.data, row.model, row.input_hash, row.created_at = data, llm.model, digest, utcnow()
    session.add(ModelUsage(provider=llm.provider, model=llm.model, purpose="brief", ok=True, **usage))
    return {**data, "model": llm.model, "created_at": iso(row.created_at), "fresh": True}
