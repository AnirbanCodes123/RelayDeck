from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Iterable

_SS_PID = re.compile(r"pid=(\d+)")
_DEFAULT_EDGE_AGENT_PORT = 9000


def host_proc() -> Path:
    raw = os.getenv("HOST_PROC", "").strip()
    if raw:
        path = Path(raw)
        if path.is_dir():
            return path
    return Path("/proc")


def proc_pid(pid: int) -> Path:
    return host_proc() / str(pid)


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


def _clk_tck() -> int:
    try:
        value = os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError):
        return 100
    return int(value) if value and value > 0 else 100


def _page_size() -> int:
    try:
        value = os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError):
        return 4096
    return int(value) if value and value > 0 else 4096


def listen_inodes_for_port(proc_net_text: str, port: int) -> set[int]:
    inodes: set[int] = set()
    for line in proc_net_text.splitlines():
        parts = line.split()
        if len(parts) < 10 or parts[0] == "sl":
            continue
        local = parts[1]
        state = parts[3]
        if state != "0A" or ":" not in local:
            continue
        try:
            local_port = int(local.rsplit(":", 1)[1], 16)
            inode = int(parts[9])
        except ValueError:
            continue
        if local_port == port and inode > 0:
            inodes.add(inode)
    return inodes


def parse_ss_listen_pids(stdout: str) -> list[int]:
    return sorted({int(match.group(1)) for match in _SS_PID.finditer(stdout)})


def parse_proc_stat_cpu_ticks(text: str) -> int | None:
    rparen = text.rfind(")")
    if rparen < 0:
        return None
    parts = text[rparen + 2 :].split()
    if len(parts) < 13:
        return None
    try:
        return int(parts[11]) + int(parts[12])
    except ValueError:
        return None


def process_cpu_percent_from_delta(
    previous_ticks: int | None,
    current_ticks: int | None,
    elapsed_seconds: float,
    clk_tck: int = 100,
    cores: int = 1,
) -> float | None:
    if previous_ticks is None or current_ticks is None or elapsed_seconds <= 0:
        return None
    delta = current_ticks - previous_ticks
    if delta < 0 or clk_tck <= 0:
        return None
    ncores = cores if cores and cores > 0 else 1
    one_core = delta / elapsed_seconds / clk_tck * 100.0
    return round(max(0.0, min(100.0, one_core / ncores)), 1)


def process_cores_used_from_delta(
    previous_ticks: int | None,
    current_ticks: int | None,
    elapsed_seconds: float,
    clk_tck: int = 100,
) -> float | None:
    if previous_ticks is None or current_ticks is None or elapsed_seconds <= 0:
        return None
    delta = current_ticks - previous_ticks
    if delta < 0 or clk_tck <= 0:
        return None
    return round(max(0.0, delta / elapsed_seconds / clk_tck), 2)


def parse_nvidia_compute_apps(stdout: str) -> list[dict[str, Any]]:
    apps: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped or stripped.lower().startswith("pid"):
            continue
        parts = [item.strip() for item in stripped.split(",")]
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            memory_mib = float(parts[-1])
        except ValueError:
            continue
        apps.append(
            {
                "pid": pid,
                "name": parts[1] if len(parts) > 2 else "",
                "memory_used_bytes": int(memory_mib * 1024 * 1024),
            }
        )
    return apps


def parse_nvidia_pmon(stdout: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split()
        if len(parts) < 5:
            continue
        try:
            pid = int(parts[1])
        except ValueError:
            continue

        def pct(value: str) -> float | None:
            if value == "-":
                return None
            try:
                return float(value)
            except ValueError:
                return None

        rows.append({"pid": pid, "sm": pct(parts[3]), "mem": pct(parts[4])})
    return rows


def gpu_for_pids(
    pids: set[int],
    device: dict[str, Any],
    apps: list[dict[str, Any]],
    pmon_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    if not device.get("available"):
        return {
            "available": False,
            "name": device.get("name"),
            "percent": None,
            "memory_percent": None,
            "memory_used_bytes": None,
            "memory_total_bytes": device.get("memory_total_bytes"),
        }
    used = sum(
        int(app["memory_used_bytes"])
        for app in apps
        if app.get("pid") in pids and app.get("memory_used_bytes")
    )
    sm_values = [
        row["sm"]
        for row in pmon_rows
        if row.get("pid") in pids and row.get("sm") is not None
    ]
    total = device.get("memory_total_bytes")
    memory_percent = round(used / total * 100, 1) if total else None
    if sm_values:
        percent = round(max(sm_values), 1)
    elif used and memory_percent is not None:
        percent = memory_percent
    else:
        percent = 0.0
    return {
        "available": True,
        "name": device.get("name"),
        "percent": percent,
        "memory_percent": memory_percent if memory_percent is not None else 0.0,
        "memory_used_bytes": used,
        "memory_total_bytes": total,
    }


def proc_net_table_paths(proc: Path | None = None) -> list[Path]:
    root = proc or host_proc()
    candidates = [
        root / "net" / "tcp",
        root / "net" / "tcp6",
        root / "1" / "net" / "tcp",
        root / "1" / "net" / "tcp6",
    ]
    paths: list[Path] = []
    seen: set[tuple[int, int]] = set()
    for path in candidates:
        try:
            stat = path.stat()
        except OSError:
            continue
        key = (stat.st_dev, stat.st_ino)
        if key in seen:
            continue
        seen.add(key)
        paths.append(path)
    return paths


def _edge_pids_from_env() -> list[int]:
    raw = os.getenv("EDGE_AGENT_PID", "").strip()
    if not raw:
        return []
    pids: list[int] = []
    for token in raw.replace(",", " ").split():
        if not token.isdigit():
            continue
        pid = int(token)
        if proc_pid(pid).is_dir():
            pids.append(pid)
    return pids


def _find_listening_pids_ss(port: int) -> list[int] | None:
    ss = _find_binary("ss")
    if not ss:
        return None
    try:
        result = _run([ss, "-H", "-lptn", f"sport = :{port}"], timeout=2)
    except (OSError, subprocess.SubprocessError):
        return None
    pids = parse_ss_listen_pids(result.stdout)
    if pids:
        return pids
    if result.stdout.strip():
        return None
    return []


def _find_listening_pids_ss_host_net(port: int) -> list[int] | None:
    nsenter = _find_binary("nsenter")
    ss = _find_binary("ss")
    netns = host_proc() / "1" / "ns" / "net"
    try:
        netns_ok = nsenter is not None and ss is not None and netns.exists()
    except OSError:
        netns_ok = False
    if not netns_ok:
        return None
    try:
        result = _run(
            [
                nsenter,
                f"--net={netns}",
                "--",
                ss,
                "-H",
                "-lptn",
                f"sport = :{port}",
            ],
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    pids = parse_ss_listen_pids(result.stdout)
    return pids or None


def _pids_holding_inodes(inodes: set[int], proc: Path | None = None) -> list[int]:
    if not inodes:
        return []
    root = proc or host_proc()
    wanted = {f"socket:[{inode}]" for inode in inodes}
    found: set[int] = set()
    try:
        proc_entries = list(root.iterdir())
    except OSError:
        return []
    for entry in proc_entries:
        if not entry.name.isdigit():
            continue
        fd_dir = entry / "fd"
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue
        for fd_name in fds:
            try:
                target = os.readlink(fd_dir / fd_name)
            except OSError:
                continue
            if target in wanted:
                tgid = read_tgid(int(entry.name)) or int(entry.name)
                found.add(tgid)
                break
    return sorted(found)


def _find_listening_pids_proc(port: int) -> list[int]:
    inodes: set[int] = set()
    for path in proc_net_table_paths():
        try:
            inodes |= listen_inodes_for_port(path.read_text(encoding="utf-8"), port)
        except OSError:
            continue
    return _pids_holding_inodes(inodes)


def find_listening_pids(port: int) -> list[int]:
    if not 1 <= port <= 65535:
        return []
    pinned = _edge_pids_from_env()
    if pinned:
        return unique_thread_groups(pinned)
    for finder in (
        _find_listening_pids_ss,
        _find_listening_pids_ss_host_net,
        _find_listening_pids_proc,
    ):
        pids = finder(port)
        if pids:
            return unique_thread_groups(pids)
    return []


def read_tgid(pid: int) -> int | None:
    path = proc_pid(pid) / "status"
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("Tgid:"):
                return int(line.split()[1])
    except (OSError, IndexError, ValueError):
        return None
    return pid


def unique_thread_groups(pids: Iterable[int]) -> list[int]:
    seen: set[int] = set()
    ordered: list[int] = []
    for pid in pids:
        if pid <= 0:
            continue
        tgid = read_tgid(pid) or pid
        if tgid in seen:
            continue
        if not proc_pid(tgid).is_dir():
            continue
        seen.add(tgid)
        ordered.append(tgid)
    return ordered


def _child_pids(pid: int) -> list[int]:
    children: list[int] = []
    task_dir = proc_pid(pid) / "task"
    try:
        tids = list(task_dir.iterdir())
    except OSError:
        return children
    for tid_path in tids:
        try:
            text = (tid_path / "children").read_text(encoding="utf-8")
        except OSError:
            continue
        for token in text.split():
            if token.isdigit():
                children.append(int(token))
    return children


def collect_process_tree(root_pids: list[int]) -> list[int]:
    seen: set[int] = set()
    stack = list(unique_thread_groups(root_pids))
    while stack:
        pid = stack.pop()
        tgid = read_tgid(pid) or pid
        if tgid in seen or tgid <= 0:
            continue
        if not proc_pid(tgid).is_dir():
            continue
        seen.add(tgid)
        stack.extend(unique_thread_groups(_child_pids(tgid)))
    return sorted(seen)


def read_process_cpu_ticks(pid: int) -> int | None:
    path = proc_pid(pid) / "stat"
    try:
        return parse_proc_stat_cpu_ticks(path.read_text(encoding="utf-8"))
    except OSError:
        return None


def read_process_rss_bytes(pid: int) -> int | None:
    path = proc_pid(pid) / "statm"
    try:
        resident_pages = int(path.read_text(encoding="utf-8").split()[1])
    except (OSError, IndexError, ValueError):
        return None
    return resident_pages * _page_size()


def read_comm(pid: int) -> str | None:
    path = proc_pid(pid) / "comm"
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def read_cmdline(pid: int) -> str | None:
    path = proc_pid(pid) / "cmdline"
    try:
        raw = path.read_bytes().replace(b"\x00", b" ").strip()
    except OSError:
        return None
    text = raw.decode("utf-8", "replace")
    return text[:120] or None


def read_threads(pid: int) -> int | None:
    path = proc_pid(pid) / "status"
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("Threads:"):
                return int(line.split()[1])
    except (OSError, IndexError, ValueError):
        return None
    return None


def read_gpu_compute_apps() -> list[dict[str, Any]]:
    nvidia_smi = _find_binary("nvidia-smi")
    if not nvidia_smi:
        return []
    try:
        result = _run(
            [
                nvidia_smi,
                "--query-compute-apps=pid,process_name,used_gpu_memory",
                "--format=csv,noheader,nounits",
            ],
            timeout=4,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    return parse_nvidia_compute_apps(result.stdout)


def read_gpu_pmon() -> list[dict[str, Any]]:
    nvidia_smi = _find_binary("nvidia-smi")
    if not nvidia_smi:
        return []
    try:
        result = _run([nvidia_smi, "pmon", "-c", "1"], timeout=4)
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    return parse_nvidia_pmon(result.stdout)


def empty_edge_agent(port: int) -> dict[str, Any]:
    return {
        "available": False,
        "port": port,
        "pid": None,
        "pids": [],
        "name": None,
        "cmdline": None,
        "threads": None,
        "cpu": {"percent": None, "cores_used": None, "cores": None},
        "memory": {"percent": None, "used_bytes": None, "total_bytes": None},
        "gpu": {
            "available": False,
            "name": None,
            "percent": None,
            "memory_percent": None,
            "memory_used_bytes": None,
            "memory_total_bytes": None,
        },
    }


def _clone_edge_agent(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        **payload,
        "pids": list(payload.get("pids") or []),
        "cpu": dict(payload.get("cpu") or {}),
        "memory": dict(payload.get("memory") or {}),
        "gpu": dict(payload.get("gpu") or {}),
    }


def empty_snapshot(edge_agent_port: int = _DEFAULT_EDGE_AGENT_PORT) -> dict[str, Any]:
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
        "edge_agent": empty_edge_agent(edge_agent_port),
    }


class HostMonitor:
    def __init__(
        self,
        interval: float = 1.0,
        edge_agent_port: int | None = None,
    ) -> None:
        self.interval = interval
        self.edge_agent_port = (
            _DEFAULT_EDGE_AGENT_PORT if edge_agent_port is None else edge_agent_port
        )
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._prev_cpu: tuple[int, int] | None = None
        self._prev_edge_ticks: int | None = None
        self._prev_edge_ts: float | None = None
        self._edge_pids: list[int] = []
        self._snapshot = empty_snapshot(self.edge_agent_port)

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
                "edge_agent": _clone_edge_agent(self._snapshot["edge_agent"]),
            }

    def _resolve_edge_pids(self) -> list[int]:
        pids = unique_thread_groups(find_listening_pids(self.edge_agent_port))
        if pids != self._edge_pids:
            self._prev_edge_ticks = None
            self._prev_edge_ts = None
        self._edge_pids = pids
        return pids

    def _sample_edge_agent(
        self, memory: dict[str, int] | None, gpu: dict[str, Any]
    ) -> dict[str, Any]:
        now = time.time()
        pids = self._resolve_edge_pids()
        if not pids:
            self._prev_edge_ticks = None
            self._prev_edge_ts = None
            return empty_edge_agent(self.edge_agent_port)

        tree = collect_process_tree(pids)
        ticks = 0
        rss = 0
        for pid in tree:
            cpu = read_process_cpu_ticks(pid)
            if cpu is not None:
                ticks += cpu
            proc_rss = read_process_rss_bytes(pid)
            if proc_rss:
                rss += proc_rss

        elapsed = 0.0 if self._prev_edge_ts is None else now - self._prev_edge_ts
        cores = os.cpu_count() or 1
        clk = _clk_tck()
        percent = process_cpu_percent_from_delta(
            self._prev_edge_ticks, ticks, elapsed, clk, cores
        )
        cores_used = process_cores_used_from_delta(
            self._prev_edge_ticks, ticks, elapsed, clk
        )
        self._prev_edge_ticks = ticks
        self._prev_edge_ts = now

        total_ram = None if not memory else memory.get("total_bytes")
        ram_percent = None
        if total_ram:
            ram_percent = round(min(100.0, max(0.0, rss / total_ram * 100)), 1)
        primary = pids[0]
        apps: list[dict[str, Any]] = []
        pmon_rows: list[dict[str, Any]] = []
        if gpu.get("available"):
            apps = read_gpu_compute_apps()
            pmon_rows = read_gpu_pmon()
        return {
            "available": True,
            "port": self.edge_agent_port,
            "pid": primary,
            "pids": tree,
            "name": read_comm(primary),
            "cmdline": read_cmdline(primary),
            "threads": read_threads(primary),
            "cpu": {
                "percent": percent,
                "cores_used": cores_used,
                "cores": cores,
            },
            "memory": {
                "percent": ram_percent,
                "used_bytes": rss,
                "total_bytes": total_ram,
            },
            "gpu": gpu_for_pids(set(tree), gpu, apps, pmon_rows),
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
        gpu = read_gpu()
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
            "gpu": gpu,
            "edge_agent": self._sample_edge_agent(memory, gpu),
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


def _edge_agent_port_from_env() -> int:
    raw = os.getenv("EDGE_AGENT_PORT", str(_DEFAULT_EDGE_AGENT_PORT))
    try:
        port = int(raw)
    except ValueError:
        return _DEFAULT_EDGE_AGENT_PORT
    return port if 1 <= port <= 65535 else _DEFAULT_EDGE_AGENT_PORT


monitor = HostMonitor(edge_agent_port=_edge_agent_port_from_env())
