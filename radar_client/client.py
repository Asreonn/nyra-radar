"""Dependency-light Python client used by the Nyra assistant tool adapter."""
from __future__ import annotations

from typing import Any

import httpx


class RadarError(RuntimeError):
    pass


class RadarClient:
    def __init__(self, base_url: str = "http://127.0.0.1:47400", token: str = "", timeout: float = 20):
        self.base_url = base_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.timeout = timeout

    def _call(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        try:
            response = httpx.request(method, self.base_url + path, headers=self.headers,
                                     timeout=self.timeout, **kwargs)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise RadarError(str(exc)) from exc

    def radar_digest(self, period: str = "24h", mode: str = "mixed", limit: int = 40) -> dict:
        return self._call("GET", "/api/v1/digest", params={"period": period, "mode": mode, "limit": limit,
                                                           "requested_by": "nyra"})

    def radar_search(self, query: str, limit: int = 30) -> dict:
        return self._call("GET", "/api/v1/search", params={"q": query, "limit": limit})

    def radar_project(self, project_id: int) -> dict:
        return self._call("GET", f"/api/v1/projects/{project_id}")

    def radar_related(self, item_id: int, limit: int = 10) -> dict:
        return self._call("GET", f"/api/v1/items/{item_id}/related", params={"limit": limit})

    def radar_feedback(self, item_id: int, action: str, context: dict | None = None) -> dict:
        return self._call("POST", f"/api/v1/items/{item_id}/feedback",
                          json={"action": action, "context": context or {"client": "nyra"}})
