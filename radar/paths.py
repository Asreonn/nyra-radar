"""Filesystem locations (XDG conventions). Nothing runtime is written into the repo."""
from __future__ import annotations

import os
from pathlib import Path

HOME = Path.home()


def _xdg(var: str, default: Path) -> Path:
    value = os.environ.get(var)
    return Path(value) if value else default


def data_dir() -> Path:
    return Path(os.environ.get("RADAR_DATA_DIR") or _xdg("XDG_DATA_HOME", HOME / ".local/share") / "nyra-radar")


def config_dir() -> Path:
    return Path(os.environ.get("RADAR_CONFIG_DIR") or _xdg("XDG_CONFIG_HOME", HOME / ".config") / "nyra-radar")


def cache_dir() -> Path:
    return Path(os.environ.get("RADAR_CACHE_DIR") or _xdg("XDG_CACHE_HOME", HOME / ".cache") / "nyra-radar")


def runtime_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return Path(os.environ.get("RADAR_RUNTIME_DIR") or Path(base) / "nyra-radar")


def db_path() -> Path:
    return Path(os.environ.get("RADAR_DB") or data_dir() / "radar.db")


def config_path() -> Path:
    return config_dir() / "config.toml"


def image_cache_dir() -> Path:
    return cache_dir() / "images"


def web_dist_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "apps" / "web" / "dist"


def ensure_dirs() -> None:
    for d in (data_dir(), config_dir(), cache_dir(), image_cache_dir(), runtime_dir()):
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
