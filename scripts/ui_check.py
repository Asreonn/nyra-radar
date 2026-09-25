#!/usr/bin/env python3
"""Headless Chromium responsive smoke test using the built-in DevTools protocol."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path

import httpx
import websockets

SIZES = ((360, 800), (412, 915), (1920, 1080))


ROUTES = ("today", "reddit")


def browser() -> str:
    bundled = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", Path.home() / ".cache/ms-playwright"))
    candidates = [shutil.which("chromium"), shutil.which("google-chrome"),
                  *map(str, sorted(bundled.glob("chromium-*/chrome-linux*/chrome")))]
    for path in candidates:
        if path and Path(path).is_file():
            return path
    raise SystemExit("Chromium not found")


async def command(ws, call_id: int, method: str, params: dict | None = None) -> dict:
    await ws.send(json.dumps({"id": call_id, "method": method, "params": params or {}}))
    while True:
        message = json.loads(await ws.recv())
        if message.get("id") == call_id:
            return message.get("result", {})


async def check_size(ws, width: int, height: int, call_id: int, route: str) -> tuple[dict, int]:
    await command(ws, call_id, "Emulation.setDeviceMetricsOverride",
                  {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": width < 800})
    call_id += 1
    await command(ws, call_id, "Page.navigate", {"url": f"http://127.0.0.1:47400/#/{route}"})
    call_id += 1
    await asyncio.sleep(2.5)
    expression = """JSON.stringify({
      route:location.hash, viewport:[innerWidth,innerHeight], scrollWidth:document.documentElement.scrollWidth,
      overflow:document.documentElement.scrollWidth>innerWidth,
      title:document.querySelector('h1')?.textContent,
      cards:document.querySelectorAll('.entry').length,
      redditCards:[...document.querySelectorAll('.entry .meta .src')].filter(x=>x.textContent.startsWith('r/')).length,
      navVisible:getComputedStyle(document.querySelector('.bottom-nav')).display !== 'none',
      minCoreControl:Math.min(...[...document.querySelectorAll('.bottom-nav a,.entry .act')]
        .filter(x=>getComputedStyle(x).display!=='none' && x.getBoundingClientRect().width>0)
        .map(x=>Math.min(x.getBoundingClientRect().width,x.getBoundingClientRect().height)))
    })"""
    raw = await command(ws, call_id, "Runtime.evaluate", {"expression": expression, "returnByValue": True})
    call_id += 1
    return json.loads(raw["result"]["value"]), call_id


async def main() -> None:
    port = 49321
    env = dict(os.environ)
    env.pop("DBUS_SESSION_BUS_ADDRESS", None)
    proc = await asyncio.create_subprocess_exec(
        browser(), "--headless", "--no-sandbox", "--disable-gpu",
        f"--remote-debugging-port={port}", "about:blank",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, env=env,
    )
    try:
        async with httpx.AsyncClient(timeout=1) as client:
            for _ in range(50):
                try:
                    pages = (await client.get(f"http://127.0.0.1:{port}/json")).json()
                    if pages:
                        break
                except (httpx.HTTPError, ValueError):
                    await asyncio.sleep(0.1)
            else:
                raise RuntimeError("browser debugging port did not start")
        async with websockets.connect(pages[0]["webSocketDebuggerUrl"]) as ws:
            results, call_id = [], 1
            for route in ROUTES:
                for width, height in SIZES:
                    result, call_id = await check_size(ws, width, height, call_id, route)
                    results.append(result)
        print(json.dumps(results, indent=2))
        if any(x["overflow"] or not x["title"] or x["cards"] < 1 or x["redditCards"] < 1 for x in results):
            raise SystemExit(1)
        if any(x["viewport"][0] < 800 and x["minCoreControl"] < 40 for x in results):
            raise SystemExit(1)
    finally:
        proc.terminate()
        await asyncio.wait_for(proc.wait(), timeout=5)


if __name__ == "__main__":
    asyncio.run(main())
