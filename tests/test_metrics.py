from unittest.mock import MagicMock, patch

from app.metrics import (
    cpu_percent_from_delta,
    parse_nvidia_smi_csv,
    read_memory,
)


def test_cpu_percent_from_delta_computes_busy_share() -> None:
    previous = (100, 200)
    current = (110, 300)
    assert cpu_percent_from_delta(previous, current) == 90.0
    assert cpu_percent_from_delta(None, current) is None
    assert cpu_percent_from_delta(current, current) is None


def test_parse_nvidia_smi_csv() -> None:
    parsed = parse_nvidia_smi_csv("NVIDIA L40, 15, 63, 29354, 46068, 44\n")
    assert parsed is not None
    assert parsed["available"] is True
    assert parsed["name"] == "NVIDIA L40"
    assert parsed["percent"] == 15.0
    assert parsed["temperature_c"] == 44
    assert parsed["memory_used_bytes"] == 29354 * 1024 * 1024
    assert parsed["memory_total_bytes"] == 46068 * 1024 * 1024


@patch("app.metrics.Path")
def test_read_memory_from_proc(path_cls: MagicMock) -> None:
    meminfo = MagicMock()
    meminfo.is_file.return_value = True
    meminfo.read_text.return_value = (
        "MemTotal:       32768000 kB\n"
        "MemAvailable:   16384000 kB\n"
        "MemFree:         8192000 kB\n"
    )
    path_cls.return_value = meminfo
    memory = read_memory()
    assert memory is not None
    assert memory["total_bytes"] == 32768000 * 1024
    assert memory["available_bytes"] == 16384000 * 1024
    assert memory["used_bytes"] == (32768000 - 16384000) * 1024
