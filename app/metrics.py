from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any


def _find_binary(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    for candidate in (Path("/usr/bin") / name, Path("/usr/local/bin") / name):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _run(command: list[str], timeout: float = 3) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
        stdin=subprocess.DEVNULL,
        env=os.environ,
    )


def read_cpu_times() -> tuple[int, int] | None:
    """Return (idle, total) jiffies from /proc/stat, or None if unavailable."""
    stat = Path("/proc/stat")
    if not stat.is_file():
        return None
    try:
        first = stat.read_text(encoding="utf-8").splitlines()[0]
    except OSError:
        return None
    parts = first.split()
    if not parts or parts[0] != "cpu":
        return None
    values = [int(item) for item in parts[1:] if item.isdigit()]
    if len(values) < 4:
        return None
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    return idle, sum(values)


def cpu_percent_from_delta(
    previous: tuple[int, int] | None, current: tuple[int, int] | None
) -> float | None:
    if previous is None or current is None:
        return None
    idle_delta = current[0] - previous[0]
    total_delta = current[1] - previous[1]
    if total_delta <= 0:
        return None
    busy = max(0.0, min(100.0, (1 - idle_delta / total_delta) * 100))
    return round(busy, 1)


def read_loadavg() -> tuple[float, float, float] | None:
    try:
        return os.getloadavg()
    except OSError:
        return None


def read_memory() -> dict[str, int] | None:
    meminfo = Path("/proc/meminfo")
    if not meminfo.is_file():
        return None
    fields: dict[str, int] = {}
    try:
        for line in meminfo.read_text(encoding="utf-8").splitlines():
            if ":" not in line:
                continue
            key, raw = line.split(":", 1)
            token = raw.strip().split()[0]
            if token.isdigit():
                fields[key] = int(token) * 1024
    except OSError:
        return None
    total = fields.get("MemTotal")
    if not total:
        return None
    available = fields.get("MemAvailable")
    if available is None:
        available = fields.get("MemFree", 0) + fields.get("Buffers", 0) + fields.get(
            "Cached", 0
        )
    used = max(0, total - available)
    return {"total_bytes": total, "used_bytes": used, "available_bytes": available}


def parse_nvidia_smi_csv(stdout: str) -> dict[str, Any] | None:
    line = next((row.strip() for row in stdout.splitlines() if row.strip()), "")
    if not line or line.lower().startswith("failed"):
        return None
    parts = [item.strip() for item in line.split(",")]
    if len(parts) < 6:
        return None

    def number(value: str) -> float | None:
        try:
            return float(value)
        except ValueError:
            return None

    gpu_util = number(parts[1])
    mem_util = number(parts[2])
    mem_used = number(parts[3])
    mem_total = number(parts[4])
    temperature = number(parts[5])
    memory_used_bytes = int(mem_used * 1024 * 1024) if mem_used is not None else None
    memory_total_bytes = int(mem_total * 1024 * 1024) if mem_total is not None else None
    memory_percent = None
    if memory_used_bytes is not None and memory_total_bytes:
        memory_percent = round(memory_used_bytes / memory_total_bytes * 100, 1)
    elif mem_util is not None:
        memory_percent = round(mem_util, 1)
    return {
        "available": True,
        "name": parts[0] or "NVIDIA GPU",
        "percent": None if gpu_util is None else round(gpu_util, 1),
        "memory_percent": memory_percent,
        "memory_used_bytes": memory_used_bytes,
        "memory_total_bytes": memory_total_bytes,
        "temperature_c": None if temperature is None else round(temperature),
    }


def read_gpu() -> dict[str, Any]:
    nvidia_smi = _find_binary("nvidia-smi")
    if not nvidia_smi:
        return {"available": False, "name": None, "percent": None}
    try:
        result = _run(
            [
                nvidia_smi,
                "--query-gpu=name,utilization.gpu,utilization.memory,memory.used,memory.total,temperature.gpu",
                "--format=csv,noheader,nounits",
            ],
            timeout=4,
        )
    except (OSError, subprocess.SubprocessError):
        return {"available": False, "name": None, "percent": None}
    parsed = parse_nvidia_smi_csv(result.stdout)
    if result.returncode != 0 or parsed is None:
        return {"available": False, "name": None, "percent": None}
    return parsed


def empty_snapshot() -> dict[str, Any]:
    cores = os.cpu_count() or 1
    return {
        "ts": time.time(),
        "cpu": {
            "percent": None,
            "cores": cores,
            "load1": None,
            "load5": None,
            "load15": None,
        },
        "memory": {
            "percent": None,
            "used_bytes": None,
            "total_bytes": None,
            "available_bytes": None,
        },
        "gpu": {"available": False, "name": None, "percent": None},
    }


class HostMonitor:
    def __init__(self, interval: float = 1.0) -> None:
        self.interval = interval
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._prev_cpu: tuple[int, int] | None = None
        self._snapshot = empty_snapshot()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="host-monitor"
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "ts": self._snapshot["ts"],
                "cpu": dict(self._snapshot["cpu"]),
                "memory": dict(self._snapshot["memory"]),
                "gpu": dict(self._snapshot["gpu"]),
            }

    def sample_once(self) -> dict[str, Any]:
        current = read_cpu_times()
        percent = cpu_percent_from_delta(self._prev_cpu, current)
        if current is not None:
            self._prev_cpu = current
        load = read_loadavg()
        if percent is None and load:
            cores = os.cpu_count() or 1
            percent = round(min(100.0, max(0.0, load[0] / cores * 100)), 1)
        memory = read_memory()
        memory_percent = None
        if memory and memory["total_bytes"]:
            memory_percent = round(memory["used_bytes"] / memory["total_bytes"] * 100, 1)
        snapshot = {
            "ts": time.time(),
            "cpu": {
                "percent": percent,
                "cores": os.cpu_count() or 1,
                "load1": None if not load else round(load[0], 2),
                "load5": None if not load else round(load[1], 2),
                "load15": None if not load else round(load[2], 2),
            },
            "memory": {
                "percent": memory_percent,
                "used_bytes": None if not memory else memory["used_bytes"],
                "total_bytes": None if not memory else memory["total_bytes"],
                "available_bytes": None if not memory else memory["available_bytes"],
            },
            "gpu": read_gpu(),
        }
        with self._lock:
            self._snapshot = snapshot
        return self.snapshot()

    def _run(self) -> None:
        self.sample_once()
        if self._stop.wait(0.25):
            return
        self.sample_once()
        while not self._stop.wait(self.interval):
            self.sample_once()


monitor = HostMonitor()
