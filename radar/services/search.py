"""Local search: SQLite FTS5 (BM25) + semantic similarity, fused with reciprocal rank fusion."""
from __future__ import annotations

import re
from typing import Any

import numpy as np
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from radar.db.models import Item, ItemTopic, Repository, Topic

_FTS_TOKEN = re.compile(r"[\w][\w+#.\-]*", re.UNICODE)


def index_items(session: Session, ids: list[int]) -> None:
    """(Re)index items in FTS. Idempotent: delete + insert per rowid."""
    ids = sorted({int(i) for i in ids if i})
    if not ids:
        return
    for chunk_start in range(0, len(ids), 400):
        chunk = ids[chunk_start:chunk_start + 400]
        items = session.scalars(select(Item).where(Item.id.in_(chunk))).all()
        topic_rows = session.execute(
            select(ItemTopic.item_id, Topic.name).join(Topic, Topic.id == ItemTopic.topic_id)
            .where(ItemTopic.item_id.in_(chunk))).all()
        topics: dict[int, list[str]] = {}
        for item_id, name in topic_rows:
            topics.setdefault(item_id, []).append(name)
        repo_ids = [it.repository_id for it in items if it.repository_id]
        repos = {r.id: r for r in session.scalars(select(Repository).where(Repository.id.in_(repo_ids)))} \
            if repo_ids else {}
        session.execute(text(f"DELETE FROM item_fts WHERE rowid IN ({','.join(map(str, chunk))})"))
        rows = []
        for it in items:
            meta = it.meta or {}
            src = " ".join(filter(None, [it.source.name if it.source else None, it.domain,
                                         meta.get("subreddit"), meta.get("channel"), meta.get("publisher")]))
            extra_parts = [it.author or ""]
            repo = repos.get(it.repository_id) if it.repository_id else None
            if repo:
                extra_parts += [repo.display_name, repo.description or "", repo.language or "",
                                " ".join(repo.topics or []), (repo.readme_excerpt or "")[:3000]]
            rows.append({"rowid": it.id, "title": it.title or "", "body": (it.clean_text or "")[:30000],
                         "summary": " ".join(filter(None, [it.summary, it.excerpt])),
                         "topics": " ".join(topics.get(it.id, [])), "source": src,
                         "extra": " ".join(p for p in extra_parts if p)})
        if rows:
            session.execute(text("INSERT INTO item_fts(rowid, title, body, summary, topics, source, extra) "
                                 "VALUES (:rowid, :title, :body, :summary, :topics, :source, :extra)"), rows)


def fts_query(q: str, mode: str = "and") -> str | None:
    toks = [t for t in _FTS_TOKEN.findall(q or "") if len(t) > 1 or t.isdigit()]
    if not toks:
        return None
    quoted = [f'"{t.replace(chr(34), "")}"' for t in toks[:12]]
    quoted[-1] = quoted[-1] + "*"
    return (" OR " if mode == "or" else " ").join(quoted)


def lexical(session: Session, q: str, limit: int = 100) -> list[tuple[int, float]]:
    out: list[tuple[int, float]] = []
    for mode in ("and", "or"):
        expr = fts_query(q, mode)
        if not expr:
            return []
        rows = session.execute(text(
            "SELECT rowid, bm25(item_fts, 8.0, 1.0, 3.0, 4.0, 2.0, 2.0) AS r FROM item_fts "
            "WHERE item_fts MATCH :q ORDER BY r LIMIT :lim"), {"q": expr, "lim": limit}).all()
        out = [(int(r[0]), float(-r[1])) for r in rows]
        if len(out) >= 5:
            break
    return out


def hybrid(session: Session, q: str, query_vec: np.ndarray | None, index: Any | None, limit: int = 50,
           allowed: set[int] | None = None, k_rrf: int = 60) -> list[dict[str, Any]]:
    """Return [{item_id, score, lexical_rank, semantic_rank, semantic_sim}] best first."""
    lex = lexical(session, q, limit=200)
    sem: list[tuple[int, float]] = []
    if query_vec is not None and index is not None:
        sem = [(i, s) for i, s in index.search(query_vec, k=200, allowed=allowed) if s > 0.35]
    scores: dict[int, dict[str, Any]] = {}
    for rank, (item_id, _score) in enumerate(lex):
        if allowed is not None and item_id not in allowed:
            continue
        d = scores.setdefault(item_id, {"item_id": item_id, "score": 0.0})
        d["score"] += 1.0 / (k_rrf + rank + 1)
        d["lexical_rank"] = rank + 1
    for rank, (item_id, sim) in enumerate(sem):
        d = scores.setdefault(item_id, {"item_id": item_id, "score": 0.0})
        d["score"] += 1.0 / (k_rrf + rank + 1)
        d["semantic_rank"] = rank + 1
        d["semantic_sim"] = round(sim, 4)
    return sorted(scores.values(), key=lambda d: -d["score"])[:limit]
