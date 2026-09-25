"""Lightweight local topic extraction used before optional LLM enrichment."""
from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from radar.db.models import Interest, Item, ItemTopic, Topic

SEEDS: dict[str, tuple[str, ...]] = {
    "local-ai": ("local ai", "llama.cpp", "ollama", "on-device", "inference"),
    "coding-agents": ("coding agent", "codex", "claude code", "cursor", "agentic coding"),
    "open-source": ("open source", "github", "repository", "self-hosted"),
    "linux": ("linux", "arch linux", "wayland", "kernel", "desktop"),
    "machine-learning": ("machine learning", "llm", "transformer", "model", "embedding"),
    "developer-tools": ("developer tool", "cli", "sdk", "api", "database"),
    "security": ("security", "vulnerability", "privacy", "encryption", "cve"),
    "web-development": ("javascript", "typescript", "react", "browser", "web framework"),
    "hardware": ("gpu", "cpu", "nvidia", "intel", "hardware"),
}


NAMES = {"local-ai": "Local AI", "coding-agents": "Coding agents", "open-source": "Open source", "linux": "Linux",
         "machine-learning": "Machine learning", "developer-tools": "Developer tools", "security": "Security",
         "web-development": "Web development", "hardware": "Hardware"}


def seed_topics(session: Session) -> int:
    count = 0
    for slug, aliases in SEEDS.items():
        topic = session.scalar(select(Topic).where(Topic.slug == slug))
        if topic is not None and topic.name == slug.replace("-", " ").title():
            topic.name = NAMES[slug]  # earlier installs used title-cased slugs ("Local Ai")
        if topic is None:
            topic = Topic(slug=slug, name=NAMES[slug], aliases=list(aliases), kind="seed")
            session.add(topic)
            session.flush()
            session.add(Interest(topic_id=topic.id, seed_weight=0.15 if slug in
                                 {"local-ai", "coding-agents", "open-source", "linux"} else 0.0))
            count += 1
    return count


def classify_items(session: Session, items: list[Item]) -> int:
    topics = session.scalars(select(Topic)).all()
    changed = 0
    for item in items:
        text = f"{item.title} {item.excerpt or ''} {item.clean_text or ''}".lower()[:12000]
        for topic in topics:
            terms = [topic.slug.replace("-", " ")] + list(topic.aliases or [])
            hits = sum(len(re.findall(rf"(?<!\w){re.escape(term.lower())}(?!\w)", text)) for term in terms)
            if hits:
                weight = min(1.0, 0.45 + 0.12 * hits)
                session.execute(sqlite_insert(ItemTopic).values(
                    item_id=item.id, topic_id=topic.id, weight=weight, method="keyword"
                ).on_conflict_do_update(index_elements=["item_id", "topic_id"],
                                        set_={"weight": weight, "method": "keyword"}))
                changed += 1
        item.processing_state = "classified"
    return changed
