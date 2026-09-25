"""Single-process cooperative scheduler for collection and bounded pipeline work."""
from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import signal
from datetime import timedelta

from sqlalchemy import func, select

from radar import paths
from radar.clustering.embeddings import make_embedder
from radar.clustering.service import cluster_pending, embed_pending
from radar.collectors.github import GitHubAPI
from radar.config import Config
from radar.db import session_scope
from radar.db.models import KV, Item, ModelUsage
from radar.discovery.service import auto_start_trials, mine_candidates, review_trials
from radar.enrichment.comments import prefetch_threads
from radar.enrichment.pipeline import cache_images, extract_pending
from radar.enrichment.service import enrich_candidates
from radar.llm.client import LocalLLM
from radar.net import Http
from radar.ranking import rescore
from radar.scheduler.resources import ResourceGate
from radar.services.collect import collect_due
from radar.services.feedback import fold_events
from radar.services.projects import hydrate_due
from radar.services.settings import get as get_setting
from radar.services.topics import classify_items
from radar.util import iso, parse_datetime, utcnow

log = logging.getLogger(__name__)


class Daemon:
    def __init__(self, config: Config):
        self.config = config
        self.stop = asyncio.Event()
        self.http = Http(max_concurrency=6)
        self.gate = ResourceGate(config)
        self.embedder = make_embedder(config)
        self.llm = LocalLLM(config)

    def _due(self, session, name: str, minutes: int) -> bool:
        row = session.get(KV, f"scheduler.{name}")
        last = parse_datetime((row.value or {}).get("last")) if row and row.value else None
        return last is None or last <= utcnow() - timedelta(minutes=minutes)

    def _mark(self, session, name: str, result: dict | int) -> None:
        key = f"scheduler.{name}"
        row = session.get(KV, key) or KV(key=key)
        session.add(row)
        row.value = {"last": iso(utcnow()), "result": result}

    async def cycle(self) -> dict:
        result: dict = {}
        with session_scope() as session:
            if get_setting(session, "scheduler.paused", False):
                result["paused"] = True
                row = session.get(KV, "worker.heartbeat") or KV(key="worker.heartbeat")
                session.add(row)
                row.value = {"at": iso(utcnow()), "result": result}
                return result
            result["collect"] = await collect_due(session, self.http, self.config, limit=4)
            result["extract"] = await extract_pending(session, self.http, limit=12)
            fresh = session.scalars(select(Item).where(Item.first_seen_at >= utcnow() - timedelta(days=7),
                                                       Item.processing_state == "normalized")
                                    .order_by(Item.first_seen_at.desc()).limit(300)).all()
            result["topics"] = classify_items(session, fresh)
            result["feedback"] = fold_events(session)
            state = self.gate.inspect()
            result["resource"] = state.__dict__
            if state.heavy_allowed:
                result["embed"] = await embed_pending(session, self.embedder, limit=48)
            result["cluster"] = cluster_pending(session, limit=180)
            result["rank"] = rescore(session)
            # Network-only (no model): keep the best Reddit/HN discussions ready to read.
            result["comments"] = await prefetch_threads(
                session, self.http, self.config, limit=int(get_setting(session, "pipeline.comments_per_cycle", 6)))
            if state.heavy_allowed:
                result["images"] = await cache_images(session, self.http, self.config, limit=8)
                result["projects"] = await hydrate_due(session, GitHubAPI(self.http, self.config), limit=3)
            if self._due(session, "discovery", 360):
                found = mine_candidates(session)
                started = auto_start_trials(session)
                reviewed = review_trials(session)
                result["discovery"] = {"found": found, "trials_started": started, **reviewed}
                self._mark(session, "discovery", result["discovery"])
            if state.heavy_allowed:
                # Re-check directly before inference: Nyra may have started after this cycle began.
                daily_budget = int(get_setting(session, "pipeline.llm_daily_budget", 400))
                used_today = session.scalar(select(func.count(ModelUsage.id)).where(
                    ModelUsage.ts >= utcnow() - timedelta(hours=24))) or 0
                llm_limit = min(int(get_setting(session, "pipeline.llm_candidates_per_cycle", 12)),
                                max(0, daily_budget - used_today))
                if (get_setting(session, "pipeline.enable_llm", True) and llm_limit > 0
                        and self.gate.inspect().heavy_allowed):
                    result["llm"] = await enrich_candidates(session, self.llm, limit=llm_limit)
                else:
                    result["llm"] = {"attempted": 0, "done": 0, "failed": 0,
                                     "budget_used": used_today, "budget": daily_budget}
            row = session.get(KV, "worker.heartbeat") or KV(key="worker.heartbeat")
            session.add(row)
            row.value = {"at": iso(utcnow()), "result": result}
        return result

    async def run(self) -> None:
        paths.ensure_dirs()
        lock_file = (paths.runtime_dir() / "worker.lock").open("w")
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock_file.close()
            raise RuntimeError("another Radar worker already holds worker.lock") from None
        lock_file.write(str(os.getpid()))
        lock_file.flush()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.stop.set)
            except NotImplementedError:
                pass
        try:
            while not self.stop.is_set():
                try:
                    await self.cycle()
                except Exception:
                    log.exception("scheduler cycle failed")
                try:
                    await asyncio.wait_for(self.stop.wait(), timeout=120)
                except TimeoutError:
                    pass
        finally:
            await self.http.close()
            fcntl.flock(lock_file, fcntl.LOCK_UN)
            lock_file.close()
