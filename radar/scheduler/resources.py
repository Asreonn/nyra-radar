"""Conservative resource gate: heavy work runs only while the laptop is idle enough."""
from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from radar.config import Config


@dataclass
class ResourceState:
    heavy_allowed: bool
    reason: str
    available_ram_mb: int
    free_vram_mb: int | None
    gpu_util_pct: int | None
    on_battery: bool
    desktop_idle: bool | None


def _ram_available_mb() -> int:
    try:
        values = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, value = line.split(":", 1)
            values[key] = int(value.strip().split()[0])
        return values.get("MemAvailable", 0) // 1024
    except (OSError, ValueError):
        return 0


def _gpu() -> tuple[int | None, int | None]:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free,utilization.gpu", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=2, check=False,
        )
        if out.returncode == 0:
            free, util = out.stdout.splitlines()[0].split(",")
            return int(free.strip()), int(util.strip())
    except (OSError, subprocess.TimeoutExpired, ValueError, IndexError):
        pass
    return None, None


def _on_battery() -> bool:
    for path in Path("/sys/class/power_supply").glob("AC*/online"):
        try:
            if path.read_text().strip() == "0":
                return True
        except OSError:
            pass
    return False


def _nyra_llama_active() -> bool:
    """Nyra owns an on-demand llama-server; its presence always wins over Radar heavy work."""
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
    pid_file = runtime / "nyra" / "llama.pid"
    try:
        pid = int(pid_file.read_text().strip())
        cmdline = (Path("/proc") / str(pid) / "cmdline").read_bytes().replace(b"\0", b" ").decode()
        return "llama-server" in cmdline
    except (OSError, ValueError, UnicodeDecodeError):
        return False


def _desktop_idle() -> bool | None:
    """Ask logind, which receives idle hints from the Wayland desktop/locker."""
    try:
        result = subprocess.run(["loginctl", "show-user", str(os.getuid()), "-p", "IdleHint", "--value"],
                                capture_output=True, text=True, timeout=2, check=False)
        if result.returncode == 0 and result.stdout.strip() in ("yes", "no"):
            return result.stdout.strip() == "yes"
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


class ResourceGate:
    def __init__(self, config: Config):
        self.config = config
        self._last_interactive = 0.0

    def note_interactive_llm(self) -> None:
        self._last_interactive = time.monotonic()

    def inspect(self) -> ResourceState:
        cfg = self.config.resource
        ram = _ram_available_mb()
        vram, util = _gpu()
        battery = _on_battery()
        desktop_idle = _desktop_idle() if cfg.idle_after_s > 0 else None
        reasons = []
        if ram < cfg.min_available_ram_mb:
            reasons.append("low available RAM")
        if vram is not None and vram < cfg.min_free_vram_mb:
            reasons.append("low free VRAM")
        if util is not None and util >= cfg.gpu_busy_util_pct:
            reasons.append("GPU busy")
        if cfg.pause_on_battery and battery:
            reasons.append("on battery")
        if cfg.watch_nyra and _nyra_llama_active():
            reasons.append("Nyra llama-server active")
        if desktop_idle is False:
            reasons.append("desktop active")
        if time.monotonic() - self._last_interactive < cfg.interactive_cooldown_s:
            reasons.append("interactive LLM cooldown")
        return ResourceState(not reasons, ", ".join(reasons) or "resources available", ram, vram, util, battery,
                             desktop_idle)
