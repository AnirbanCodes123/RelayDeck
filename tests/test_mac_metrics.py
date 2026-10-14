import json
import time
from unittest.mock import patch

import pytest

from app.metrics import HostMonitor, read_mac_snapshot
from scripts.mac_host_monitor import parse_memory, read_apple_gpu


def native_snapshot():
    return {"ts": time.time(), "source": "macos",
            "cpu": {"percent": 25, "cores": 8},
            "memory": {"total_bytes": 8 * 1024**3, "used_bytes": 6 * 1024**3,
                       "percent": 75, "unified": True},
            "gpu": {"available": True, "name": "Apple M2", "percent": 12,
                    "compute_available": False, "unified": True}}


def test_host_monitor_uses_native_totals_and_preserves_vm_edge_metrics(tmp_path, monkeypatch):
    path = tmp_path / "metrics.json"
    path.write_text(json.dumps(native_snapshot()))
    monkeypatch.setenv("HOST_PLATFORM", "macos")
    monkeypatch.setenv("HOST_METRICS_FILE", str(path))
    vm_memory = {"total_bytes": 4 * 1024**3, "used_bytes": 1024**3,
                 "available_bytes": 3 * 1024**3}
    monitor = HostMonitor()
    with patch("app.metrics.read_memory", return_value=vm_memory), \
         patch("app.metrics.read_gpu", return_value={"available": False}), \
         patch.object(monitor, "_sample_edge_agent", wraps=monitor._sample_edge_agent) as edge, \
         patch.object(monitor, "_resolve_edge_pids", return_value=[]):
        result = monitor.sample_once()
    assert result["memory"]["total_bytes"] == 8 * 1024**3
    assert result["cpu"]["percent"] == 25
    assert result["gpu"]["name"] == "Apple M2"
    assert result["gpu"]["compute_available"] is False
    assert result["source"] == "macos"
    assert result["status"] == "live"
    assert edge.call_args.args[0] == vm_memory


@pytest.mark.parametrize("contents", [None, "{", "[]", json.dumps({**native_snapshot(), "ts": 0})])
def test_missing_corrupt_or_stale_host_data_goes_offline(tmp_path, monkeypatch, contents):
    path = tmp_path / "metrics.json"
    if contents is not None:
        path.write_text(contents)
    monkeypatch.setenv("HOST_PLATFORM", "macos")
    monkeypatch.setenv("HOST_METRICS_FILE", str(path))
    result = read_mac_snapshot()
    assert result["status"] == "offline"
    assert result["memory"]["total_bytes"] is None
    assert result["cpu"]["percent"] is None
    assert HostMonitor().snapshot()["status"] == "offline"


def test_vm_stat_counts_compressor_footprint_and_excludes_file_cache():
    text = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages active: 100.
Pages inactive: 50.
Pages speculative: 10.
Pages wired down: 20.
Pages occupied by compressor: 30.
Pages stored in compressor: 900.
File-backed pages: 40.
Pages purgeable: 5.
"""
    result = parse_memory(text, 8 * 1024**3)
    assert result["used_bytes"] == 165 * 16384
    assert result["total_bytes"] == 8 * 1024**3


@pytest.mark.parametrize("stats, expected", [({}, None), ({"Device Utilization %": 7}, 7)])
def test_apple_gpu_does_not_invent_utilization_or_dedicated_vram(stats, expected):
    import plistlib
    raw = plistlib.dumps([{"model": "Apple M2", "PerformanceStatistics": stats}])
    with patch("scripts.mac_host_monitor.subprocess.check_output", return_value=raw):
        result = read_apple_gpu()
    assert result["available"] is True
    assert result["percent"] == expected
    assert result["compute_available"] is False
    assert "memory_total_bytes" not in result
