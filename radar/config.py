"""Static configuration (config.toml) plus DB-backed runtime settings with defaults.

- `config.toml` holds deployment facts and secrets: bind address, endpoints, credentials.
  Secrets can be given as `*_command` (e.g. `gh auth token`, `secret-tool lookup ...`) so
  they never have to be written to disk. Secrets are never logged.
- Runtime settings (weights, budgets, intervals, …) live in the `settings` table so the UI
  can edit them. `DEFAULT_SETTINGS` is the source of truth for keys and defaults.
"""
from __future__ import annotations

import copy
import logging
import os
import shlex
import subprocess
import tomllib
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from radar import paths

log = logging.getLogger(__name__)

DEFAULT_PORT = 47400

CONFIG_TEMPLATE = """\
# Nyra Radar configuration. Runtime-tunable settings (weights, intervals, budgets)
# live in the database and are edited from the Settings view.
# Secrets: prefer *_command entries so no secret is stored in this file.

[server]
host = "127.0.0.1"          # use your Tailscale IP (100.x.y.z) or 0.0.0.0 for LAN access
port = {port}
token = "{token}"            # required from non-loopback clients (open /?token=... once on the phone)
allow_unauthenticated_lan = false

[llm]
provider = "auto"           # auto | ollama | openai | none
base_url = ""               # auto: Ollama at http://127.0.0.1:11434 if it answers
model = ""                  # auto: first chat model reported by the endpoint
api_key = ""
api_key_command = ""
num_ctx = 6144
keep_alive = "5m"
timeout_s = 180

[embeddings]
provider = "auto"           # auto | ollama | openai | hash (offline lexical fallback)
base_url = ""
model = "nomic-embed-text"

[github]
token = ""
token_command = "gh auth token"   # uses the GitHub CLI login if present; leave empty to run anonymously

[reddit]
# Optional. Without OAuth credentials Radar uses old.reddit.com JSON with polite pacing.
client_id = ""
client_secret = ""
client_secret_command = ""

[resource]
watch_nyra = true
pause_on_battery = false
min_free_vram_mb = 1200
min_available_ram_mb = 700
interactive_cooldown_s = 90
gpu_busy_util_pct = 70
idle_after_s = 600
"""


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = DEFAULT_PORT
    token: str = ""
    allow_unauthenticated_lan: bool = False


@dataclass
class LLMConfig:
    provider: str = "auto"
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    api_key_command: str = ""
    num_ctx: int = 6144
    keep_alive: str = "5m"
    timeout_s: float = 180.0


@dataclass
class EmbeddingConfig:
    provider: str = "auto"
    base_url: str = ""
    model: str = "nomic-embed-text"


@dataclass
class GitHubConfig:
    token: str = ""
    token_command: str = ""


@dataclass
class RedditConfig:
    client_id: str = ""
    client_secret: str = ""
    client_secret_command: str = ""


@dataclass
class ResourceConfig:
    watch_nyra: bool = True
    pause_on_battery: bool = False
    min_free_vram_mb: int = 1200
    min_available_ram_mb: int = 700
    interactive_cooldown_s: float = 90.0
    gpu_busy_util_pct: int = 70
    idle_after_s: float = 600.0


@dataclass
class Config:
    server: ServerConfig = field(default_factory=ServerConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    embeddings: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    github: GitHubConfig = field(default_factory=GitHubConfig)
    reddit: RedditConfig = field(default_factory=RedditConfig)
    resource: ResourceConfig = field(default_factory=ResourceConfig)
    path: Path | None = None

    def secret(self, value: str, command: str) -> str:
        """Resolve a secret from a literal value or a command. Never logs the value."""
        if value:
            return value
        if not command:
            return ""
        return _run_secret_command(command)


@lru_cache(maxsize=16)
def _run_secret_command(command: str) -> str:
    try:
        out = subprocess.run(shlex.split(command), capture_output=True, text=True, timeout=10, check=False)
        if out.returncode == 0:
            return out.stdout.strip()
        log.warning("secret command failed (exit %s): %s", out.returncode, shlex.split(command)[0])
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("secret command unavailable: %s", type(exc).__name__)
    return ""


def _merge(dc: Any, data: dict[str, Any]) -> Any:
    for key, value in data.items():
        if hasattr(dc, key):
            current = getattr(dc, key)
            if isinstance(current, bool):
                value = bool(value)
            elif isinstance(current, int) and not isinstance(current, bool):
                value = int(value)
            elif isinstance(current, float):
                value = float(value)
            setattr(dc, key, value)
    return dc


def load_config(path: Path | None = None) -> Config:
    path = path or paths.config_path()
    cfg = Config(path=path)
    if path.exists():
        data = tomllib.loads(path.read_text())
        for section in ("server", "llm", "embeddings", "github", "reddit", "resource"):
            if isinstance(data.get(section), dict):
                _merge(getattr(cfg, section), data[section])
    # Environment overrides (handy for tests / systemd drop-ins).
    if os.environ.get("RADAR_HOST"):
        cfg.server.host = os.environ["RADAR_HOST"]
    if os.environ.get("RADAR_PORT"):
        cfg.server.port = int(os.environ["RADAR_PORT"])
    if os.environ.get("RADAR_LLM_PROVIDER"):
        cfg.llm.provider = os.environ["RADAR_LLM_PROVIDER"]
    if os.environ.get("RADAR_EMBED_PROVIDER"):
        cfg.embeddings.provider = os.environ["RADAR_EMBED_PROVIDER"]
    if os.environ.get("GITHUB_TOKEN") and not cfg.github.token:
        cfg.github.token = os.environ["GITHUB_TOKEN"]
    return cfg


_CONFIG: Config | None = None


def get_config() -> Config:
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = load_config()
    return _CONFIG


def set_config(cfg: Config) -> None:
    global _CONFIG
    _CONFIG = cfg


def write_template(path: Path | None = None) -> Path:
    import secrets

    path = path or paths.config_path()
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(CONFIG_TEMPLATE.format(port=DEFAULT_PORT, token=secrets.token_urlsafe(24)))
    path.chmod(0o600)
    return path


# ---------------------------------------------------------------------------
# Runtime settings (DB-backed). Keys are dotted; values are JSON-serialisable.
# ---------------------------------------------------------------------------
DEFAULT_SETTINGS: dict[str, Any] = {
    # Ranking weights. Every component is normalised to [0, 1] before weighting.
    "ranking.weights": {
        "personal_interest": 1.6,
        "semantic_preference": 1.8,
        "freshness": 1.0,
        "novelty": 0.7,
        "source_quality": 0.5,
        "source_affinity": 1.0,
        "content_type_preference": 0.8,
        "community_signal": 0.9,
        "project_momentum": 0.9,
        "coverage": 0.6,
        "llm_quality": 0.4,
        "duplicate_penalty": 2.0,
        "spam_penalty": 2.5,
        "ignored_penalty": 0.4,
    },
    # Prior per content type (0..1) for the `content_type_preference` component. Reddit is the
    # primary community source; Hacker News stays a strong secondary one.
    "ranking.content_type_preference": {"reddit_post": 0.8, "hackernews_post": 0.65, "github_repository": 0.6,
                                        "github_release": 0.55, "default": 0.5},
    # Feed budget for the Today/mixed digest. Normalised if it does not sum to 1. `reddit` is a
    # dedicated Reddit section shown first on Today.
    "feed.budget": {"reddit": 0.30, "for_you": 0.35, "important": 0.12, "adjacent": 0.10, "oss": 0.08,
                    "wildcard": 0.05},
    # At most this share of the feed may come from one subreddit (self posts otherwise all
    # share the reddit.com domain and would be capped together).
    "feed.max_per_community": 0.08,
    "feed.size": 40,
    "feed.freshness_half_life_hours": {"default": 30, "github_repository": 96, "github_release": 72,
                                       "youtube_video": 72, "website_change": 72},
    "pipeline.max_age_days": 10,
    "pipeline.enable_llm": True,
    "pipeline.candidate_pool": 200,
    "pipeline.llm_candidates_per_cycle": 12,
    "pipeline.llm_daily_budget": 400,
    "pipeline.extraction_batch": 30,
    "pipeline.embedding_batch": 64,
    "pipeline.comments_per_cycle": 6,
    "clustering.story_threshold": 0.84,
    "clustering.dup_threshold": 0.955,
    "clustering.window_hours": 96,
    "scheduler.paused": False,
    "scheduler.jitter_pct": 0.15,
    "scheduler.intervals_min": {
        "rss": 30, "reddit": 20, "hackernews": 15, "github_search": 90, "github_trending": 120,
        "ossinsight": 120, "github_releases": 180, "google_news": 60, "youtube": 120,
        "website_watch": 360, "telegram": 60, "hn_search": 120,
    },
    "scheduler.jobs_min": {
        "pipeline": 2, "repo_hydrate": 30, "discovery": 360, "trial_review": 120,
        "digest": 360, "cleanup": 1440, "interest_refresh": 10, "story_summaries": 20,
    },
    "images.cache_enabled": True,
    "images.max_width": 1280,
    "privacy.record_impressions": True,
    "privacy.history_retention_days": 180,
    "storage.item_retention_days": 45,
    "storage.image_cache_max_mb": 700,
    "discovery.enabled": True,
    "discovery.max_active_trials": 6,
    "discovery.trial_days": 3,
    "discovery.trial_min_items": 8,
    "discovery.auto_trial": True,
    "discovery.auto_promote": True,
    "discovery.use_llm": True,
    "interests.decay_half_life_days": 30,
    "interests.propagation": 0.35,
}


def default_setting(key: str) -> Any:
    return copy.deepcopy(DEFAULT_SETTINGS.get(key))
