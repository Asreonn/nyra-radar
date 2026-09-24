"""Nyra Radar command line entrypoint."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

import uvicorn
from sqlalchemy import func, select

from radar import paths
from radar.config import get_config, write_template
from radar.db import init_engine, migrate, session_scope
from radar.db.models import Item, Repository, Source
from radar.net import Http
from radar.scheduler.daemon import Daemon
from radar.services.collect import collect_due, ensure_default_sources
from radar.services.topics import seed_topics


def bootstrap() -> dict:
    paths.ensure_dirs()
    config_path = write_template()
    init_engine()
    migrate()
    with session_scope() as session:
        sources = ensure_default_sources(session)
        topics = seed_topics(session)
    return {"config": str(config_path), "database": str(paths.db_path()), "sources_added": sources,
            "topics_added": topics}


async def _collect(force: bool) -> dict:
    config = get_config()
    http = Http()
    try:
        with session_scope() as session:
            return await collect_due(session, http, config, force=force, limit=100)
    finally:
        await http.close()


def doctor() -> dict:
    bootstrap()
    config = get_config()
    with session_scope() as session:
        counts = {"sources": session.scalar(select(func.count(Source.id))) or 0,
                  "items": session.scalar(select(func.count(Item.id))) or 0,
                  "projects": session.scalar(select(func.count(Repository.id))) or 0}
    from radar.scheduler.resources import ResourceGate
    state = ResourceGate(config).inspect()
    return {"ok": True, "paths": {"config": str(paths.config_path()), "database": str(paths.db_path()),
                                    "cache": str(paths.cache_dir())}, "counts": counts,
            "resources": state.__dict__, "server": f"http://{config.server.host}:{config.server.port}"}


def main() -> None:
    parser = argparse.ArgumentParser(prog="radar", description="Nyra Radar local intelligence service")
    parser.add_argument("--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    sub.add_parser("doctor")
    collect = sub.add_parser("collect")
    collect.add_argument("--force", action="store_true")
    sub.add_parser("worker")
    serve = sub.add_parser("serve")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    sub.add_parser("run", help="run API and worker in one process")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.command == "init":
        print(json.dumps(bootstrap(), indent=2))
    elif args.command == "doctor":
        print(json.dumps(doctor(), indent=2))
    elif args.command == "collect":
        bootstrap()
        print(json.dumps(asyncio.run(_collect(args.force)), indent=2))
    elif args.command == "worker":
        bootstrap()
        asyncio.run(Daemon(get_config()).run())
    elif args.command == "serve":
        bootstrap()
        cfg = get_config().server
        uvicorn.run("radar.api.app:app", host=args.host or cfg.host, port=args.port or cfg.port)
    elif args.command == "run":
        bootstrap()
        cfg = get_config().server

        async def both():
            worker = asyncio.create_task(Daemon(get_config()).run())
            server = uvicorn.Server(uvicorn.Config("radar.api.app:app", host=cfg.host, port=cfg.port))
            try:
                await server.serve()
            finally:
                worker.cancel()
        asyncio.run(both())
    else:
        sys.exit(2)


if __name__ == "__main__":
    main()
