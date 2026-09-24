"""In-process matrix of item embeddings with cheap invalidation.

At personal scale (thousands to low tens of thousands of items) a dense numpy matrix
is faster and simpler than any vector DB: 20k × 768 float32 ≈ 60 MB. Only the current
embedding model's vectors are loaded.
"""
from __future__ import annotations

import threading

import numpy as np
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from radar.db.models import Embedding


class VectorIndex:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sig: tuple | None = None
        self.ids: np.ndarray = np.zeros(0, dtype=np.int64)
        self.matrix: np.ndarray = np.zeros((0, 1), dtype=np.float32)
        self.model: str | None = None
        self._pos: dict[int, int] = {}

    def _signature(self, session: Session, model: str) -> tuple:
        row = session.execute(select(func.count(Embedding.item_id), func.max(Embedding.created_at))
                              .where(Embedding.model == model)).one()
        return (model, row[0], row[1])

    def refresh(self, session: Session, model: str) -> None:
        sig = self._signature(session, model)
        if sig == self._sig:
            return
        with self._lock:
            rows = session.execute(text("SELECT item_id, vector FROM embeddings WHERE model = :m ORDER BY item_id"),
                                   {"m": model}).all()
            if rows:
                ids = np.fromiter((r[0] for r in rows), dtype=np.int64, count=len(rows))
                dim = len(rows[0][1]) // 4
                mat = np.empty((len(rows), dim), dtype=np.float32)
                for i, r in enumerate(rows):
                    if len(r[1]) == dim * 4:
                        mat[i] = np.frombuffer(r[1], dtype=np.float32)
                    else:
                        mat[i] = 0
            else:
                ids, mat = np.zeros(0, dtype=np.int64), np.zeros((0, 1), dtype=np.float32)
            self.ids, self.matrix, self.model, self._sig = ids, mat, model, sig
            self._pos = {int(x): i for i, x in enumerate(ids)}

    def vector(self, item_id: int) -> np.ndarray | None:
        i = self._pos.get(int(item_id))
        return None if i is None else self.matrix[i]

    def vectors(self, item_ids: list[int]) -> tuple[list[int], np.ndarray]:
        found = [(x, self._pos[x]) for x in item_ids if x in self._pos]
        if not found:
            return [], np.zeros((0, self.matrix.shape[1]), dtype=np.float32)
        return [f[0] for f in found], self.matrix[[f[1] for f in found]]

    def search(self, query: np.ndarray, k: int = 50, allowed: set[int] | None = None,
               exclude: set[int] | None = None) -> list[tuple[int, float]]:
        if self.matrix.shape[0] == 0 or query is None or query.shape[-1] != self.matrix.shape[1]:
            return []
        sims = self.matrix @ query.astype(np.float32)
        order = np.argsort(-sims)
        out = []
        for idx in order:
            item_id = int(self.ids[idx])
            if exclude and item_id in exclude:
                continue
            if allowed is not None and item_id not in allowed:
                continue
            out.append((item_id, float(sims[idx])))
            if len(out) >= k:
                break
        return out


_INDEX = VectorIndex()


def get_index(session: Session, model: str) -> VectorIndex:
    _INDEX.refresh(session, model)
    return _INDEX


def reset_index() -> None:
    global _INDEX
    _INDEX = VectorIndex()

