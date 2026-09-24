"""Small structured-output client for local Ollama/OpenAI-compatible endpoints."""
from __future__ import annotations

import json
import time
from typing import Any

import httpx

from radar.config import Config


class LLMUnavailable(RuntimeError):
    pass


class LocalLLM:
    def __init__(self, config: Config):
        self.config = config
        self.provider = config.llm.provider
        self.base_url = config.llm.base_url.rstrip("/")
        self.model = config.llm.model

    def resolve(self) -> None:
        if self.provider == "none":
            raise LLMUnavailable("LLM disabled")
        if self.provider in ("auto", "ollama"):
            self.provider = "ollama"
            self.base_url = self.base_url or "http://127.0.0.1:11434"
            if not self.model:
                try:
                    models = httpx.get(f"{self.base_url}/api/tags", timeout=3).json().get("models", [])
                    candidates = [m["name"] for m in models if "embed" not in m["name"].lower()]
                    self.model = candidates[0] if candidates else ""
                except (httpx.HTTPError, ValueError, KeyError):
                    pass
        elif self.provider == "openai":
            self.base_url = self.base_url or "http://127.0.0.1:8080/v1"
        if not self.model:
            raise LLMUnavailable("no local chat model detected")

    async def json(self, system: str, prompt: str, schema: dict[str, Any] | None = None) -> tuple[dict, dict]:
        self.resolve()
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=self.config.llm.timeout_s) as client:
                if self.provider == "ollama":
                    response = await client.post(f"{self.base_url}/api/chat", json={
                        "model": self.model,
                        "messages": [{"role": "system", "content": system},
                                     {"role": "user", "content": prompt}],
                        "stream": False, "format": schema or "json", "think": False,
                        "keep_alive": self.config.llm.keep_alive,
                        "options": {"num_ctx": self.config.llm.num_ctx, "temperature": 0.15},
                    })
                    response.raise_for_status()
                    raw = response.json()
                    content = raw["message"]["content"]
                    usage = {"prompt_tokens": raw.get("prompt_eval_count", 0),
                             "completion_tokens": raw.get("eval_count", 0)}
                else:
                    key = self.config.secret(self.config.llm.api_key, self.config.llm.api_key_command)
                    headers = {"Authorization": f"Bearer {key}"} if key else {}
                    response = await client.post(f"{self.base_url}/chat/completions", headers=headers, json={
                        "model": self.model,
                        "messages": [{"role": "system", "content": system},
                                     {"role": "user", "content": prompt}],
                        "temperature": 0.15,
                        "response_format": {"type": "json_object"},
                    })
                    response.raise_for_status()
                    raw = response.json()
                    content = raw["choices"][0]["message"]["content"]
                    usage = {"prompt_tokens": raw.get("usage", {}).get("prompt_tokens", 0),
                             "completion_tokens": raw.get("usage", {}).get("completion_tokens", 0)}
            usage["duration_ms"] = round((time.monotonic() - started) * 1000)
            return json.loads(content), usage
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise LLMUnavailable(str(exc)) from exc
