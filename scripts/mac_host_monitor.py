"""Publish native macOS telemetry for the Docker portal, using only the stdlib."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import platform
import plistlib
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_DIR = Path(__file__).resolve().parents[1] / ".mac-monitor"


def command(*args: str) -> str:
    return subprocess.check_output(args, text=True, timeout=8)


def parse_memory(text: str, total: int) -> dict:
    page_size = int(re.search(r"page size of (\d+) bytes", text).group(1))
    pages = {name.strip(): int(value) for name, value in
             re.findall(r"^([^:\n]+):\s+(\d+)\.", text, re.M)}
    # Physical resident pages, excluding reclaimable file cache and purgeable pages.
    # Compressed pages count by their physical footprint, not uncompressed size.
    resident = sum(pages[name] for name in (
        "Pages active", "Pages inactive", "Pages speculative",
        "Pages wired down", "Pages occupied by compressor",
    ))
    reclaimable = pages.get("File-backed pages", 0) + pages.get("Pages purgeable", 0)
    used = max(0, min(total, (resident - reclaimable) * page_size))
    return {"percent": round(used / total * 100, 1), "used_bytes": used,
            "total_bytes": total, "available_bytes": total - used,
            "unified": platform.machine() == "arm64"}


def read_apple_gpu() -> dict:
    gpu = {"available": False, "name": None, "percent": None,
           "unified": platform.machine() == "arm64", "compute_available": False,
           "note": "Metal/MPS is host-only; Docker uses CPU"}
    try:
        raw = subprocess.check_output(
            ["/usr/sbin/ioreg", "-r", "-c", "AGXAccelerator", "-a"], timeout=4)
        devices = plistlib.loads(raw)
        if devices:
            device = devices[0]
            name = device.get("model", "Apple GPU")
            if isinstance(name, bytes):
                name = name.decode(errors="replace").strip("\x00")
            percent = device.get("PerformanceStatistics", {}).get("Device Utilization %")
            if isinstance(percent, (int, float)) and 0 <= percent <= 100:
                gpu["percent"] = round(percent, 1)
            gpu.update(available=True, name=str(name))
    except (OSError, ValueError, plistlib.InvalidFileException, subprocess.SubprocessError):
        pass
    return gpu


def sample() -> dict:
    total = int(command("/usr/sbin/sysctl", "-n", "hw.memsize").strip())
    cores = int(command("/usr/sbin/sysctl", "-n", "hw.logicalcpu").strip())
    # The second top sample measures the interval, unlike the boot-average first sample.
    top = command("/usr/bin/top", "-l", "2", "-s", "1", "-n", "0")
    idle = re.findall(r"CPU usage:.*?([\d.]+)% idle", top)
    if not idle:
        raise ValueError("macOS top did not return CPU utilization")
    load = os.getloadavg()
    return {"ts": time.time(), "source": "macos", "status": "live",
            "cpu": {"percent": round(100 - float(idle[-1]), 1), "cores": cores,
                    "load1": round(load[0], 2), "load5": round(load[1], 2),
                    "load15": round(load[2], 2)},
            "memory": parse_memory(command("/usr/bin/vm_stat"), total),
            "gpu": read_apple_gpu()}


def running(directory: Path) -> bool:
    with (directory / "monitor.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return False
        except BlockingIOError:
            return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=DEFAULT_DIR)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--background", action="store_true")
    mode.add_argument("--stop", action="store_true")
    args = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("Run this monitor on the macOS host, outside Docker.")
    if args.once:
        print(json.dumps(sample()))
        return
    directory = args.directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    if args.stop:
        if running(directory):
            pid = int((directory / "monitor.lock").read_text().strip())
            process = command("/bin/ps", "-p", str(pid), "-o", "command=")
            if str(Path(__file__).resolve()) not in process:
                raise RuntimeError("Monitor PID does not belong to this script")
            os.kill(pid, signal.SIGTERM)
        print("Mac host monitor stopped.")
        return
    if args.background:
        if running(directory):
            print("Mac host monitor is already running.")
            return
        started = time.time()
        with (directory / "monitor.log").open("a") as log:
            child = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "--directory", str(directory)],
                stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        for _ in range(100):
            try:
                payload = json.loads((directory / "metrics.json").read_text())
                if payload["ts"] >= started:
                    print("Mac host monitor started.")
                    return
            except (OSError, ValueError, KeyError):
                pass
            if child.poll() is not None:
                break
            time.sleep(0.1)
        raise RuntimeError(f"Monitor did not start; check {directory / 'monitor.log'}")
    with (directory / "monitor.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        lock.seek(0)
        lock.truncate()
        lock.write(str(os.getpid()))
        lock.flush()
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        while True:
            try:
                payload = sample()
                temporary = directory / "metrics.tmp"
                temporary.write_text(json.dumps(payload), encoding="utf-8")
                temporary.replace(directory / "metrics.json")
            except (OSError, ValueError, KeyError, AttributeError, subprocess.SubprocessError) as error:
                print(f"Sampling failed: {error}", file=sys.stderr, flush=True)
            time.sleep(1)


if __name__ == "__main__":
    main()
