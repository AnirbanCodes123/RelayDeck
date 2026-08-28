from unittest.mock import MagicMock, patch

from app.metrics import (
    cpu_percent_from_delta,
    find_listening_pids,
    gpu_for_pids,
    listen_inodes_for_port,
    parse_nvidia_compute_apps,
    parse_nvidia_pmon,
    parse_nvidia_smi_csv,
    parse_proc_stat_cpu_ticks,
    parse_ss_listen_pids,
    proc_net_table_paths,
    process_cores_used_from_delta,
    process_cpu_percent_from_delta,
    read_memory,
    unique_thread_groups,
)


def test_cpu_percent_from_delta_computes_busy_share() -> None:
    previous = (100, 200)
    current = (110, 300)
    assert cpu_percent_from_delta(previous, current) == 90.0
    assert cpu_percent_from_delta(None, current) is None
    assert cpu_percent_from_delta(current, current) is None


def test_process_cpu_percent_from_delta_is_one_core_units() -> None:
    assert process_cpu_percent_from_delta(0, 100, 1.0, 100) == 100.0
    assert process_cpu_percent_from_delta(10, 30, 0.5, 100) == 40.0
    assert process_cpu_percent_from_delta(0, 100, 1.0, 100, cores=4) == 25.0
    assert process_cpu_percent_from_delta(0, 800, 1.0, 100, cores=8) == 100.0
    assert process_cpu_percent_from_delta(None, 100, 1.0, 100) is None
    assert process_cpu_percent_from_delta(10, 20, 0, 100) is None


def test_process_cores_used_from_delta() -> None:
    assert process_cores_used_from_delta(0, 100, 1.0, 100) == 1.0
    assert process_cores_used_from_delta(0, 250, 1.0, 100) == 2.5


def test_parse_proc_stat_cpu_ticks() -> None:
    line = "10 (python3) S 1 10 10 0 0 0 0 0 0 0 40 10 0 0 20 0 1 0 1"
    assert parse_proc_stat_cpu_ticks(line) == 50
    assert parse_proc_stat_cpu_ticks("broken") is None


def test_listen_inodes_for_port() -> None:
    table = (
        "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
        "   0: 00000000:2328 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 999 1 0000000000000000 100 0 0 10 0\n"
        "   1: 00000000:1F90 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 111 1 0000000000000000 100 0 0 10 0\n"
        "   2: 00000000:2328 0100007F:0016 01 00000000:00000000 00:00000000 00000000     0        0 222 1 0000000000000000 100 0 0 10 0\n"
    )
    assert listen_inodes_for_port(table, 9000) == {999}
    assert listen_inodes_for_port(table, 8080) == {111}


def test_parse_ss_listen_pids() -> None:
    stdout = 'LISTEN 0 2048 0.0.0.0:9000 0.0.0.0:* users:(("python3",pid=3245626,fd=48))\n'
    assert parse_ss_listen_pids(stdout) == [3245626]


def test_proc_net_table_paths_includes_pid1(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOST_PROC", str(tmp_path))
    (tmp_path / "net").mkdir()
    (tmp_path / "1" / "net").mkdir(parents=True)
    (tmp_path / "net" / "tcp").write_text("container-ns\n")
    (tmp_path / "1" / "net" / "tcp").write_text("host-ns\n")
    paths = proc_net_table_paths()
    assert tmp_path / "net" / "tcp" in paths
    assert tmp_path / "1" / "net" / "tcp" in paths


@patch("app.metrics.unique_thread_groups", side_effect=lambda pids: list(pids))
@patch("app.metrics._find_listening_pids_proc", return_value=[3245626])
@patch("app.metrics._find_listening_pids_ss_host_net", return_value=None)
@patch("app.metrics._find_listening_pids_ss", return_value=[])
def test_find_listening_pids_falls_back_when_container_ss_is_empty(
    _ss: MagicMock, _host_ss: MagicMock, _proc: MagicMock, _unique: MagicMock
) -> None:
    assert find_listening_pids(9000) == [3245626]


@patch("app.metrics.unique_thread_groups", side_effect=lambda pids: list(pids))
@patch("app.metrics._edge_pids_from_env", return_value=[77])
@patch("app.metrics._find_listening_pids_ss")
def test_find_listening_pids_uses_pinned_env_pid(
    ss: MagicMock, _env: MagicMock, _unique: MagicMock
) -> None:
    assert find_listening_pids(9000) == [77]
    ss.assert_not_called()


def test_unique_thread_groups_collapses_threads(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOST_PROC", str(tmp_path))
    for pid, tgid in ((10, 10), (11, 10), (12, 10), (20, 20)):
        directory = tmp_path / str(pid)
        directory.mkdir()
        (directory / "status").write_text(f"Name:\tpython\nTgid:\t{tgid}\n")
    assert unique_thread_groups([11, 10, 12, 20]) == [10, 20]


def test_parse_nvidia_smi_csv() -> None:
    parsed = parse_nvidia_smi_csv("NVIDIA L40, 15, 63, 29354, 46068, 44\n")
    assert parsed is not None
    assert parsed["available"] is True
    assert parsed["name"] == "NVIDIA L40"
    assert parsed["percent"] == 15.0
    assert parsed["temperature_c"] == 44
    assert parsed["memory_used_bytes"] == 29354 * 1024 * 1024
    assert parsed["memory_total_bytes"] == 46068 * 1024 * 1024


def test_parse_nvidia_compute_apps() -> None:
    text = "pid, process_name, used_gpu_memory [MiB]\n3245626, python3, 744\n1206329, python3, 3566\n"
    apps = parse_nvidia_compute_apps(text)
    assert apps[0]["pid"] == 3245626
    assert apps[0]["memory_used_bytes"] == 744 * 1024 * 1024
    assert apps[1]["pid"] == 1206329


def test_parse_nvidia_pmon() -> None:
    text = (
        "# gpu         pid   type     sm    mem    enc    dec    jpg    ofa    command \n"
        "# Idx           #    C/G      %      %      %      %      %      %    name \n"
        "    0    3245626     C      12      3      -      -      -      -    python3\n"
        "    0      18956     G      -      -      -      -      -      -    Xorg\n"
    )
    rows = parse_nvidia_pmon(text)
    assert rows[0]["pid"] == 3245626
    assert rows[0]["sm"] == 12.0
    assert rows[1]["sm"] is None


def test_gpu_for_pids_uses_process_sm_and_memory() -> None:
    device = {
        "available": True,
        "name": "NVIDIA L40",
        "percent": 40.0,
        "memory_total_bytes": 46068 * 1024 * 1024,
    }
    apps = [{"pid": 3245626, "memory_used_bytes": 744 * 1024 * 1024}]
    pmon = [{"pid": 3245626, "sm": 12.0}]
    gpu = gpu_for_pids({3245626}, device, apps, pmon)
    assert gpu["available"] is True
    assert gpu["percent"] == 12.0
    assert gpu["memory_used_bytes"] == 744 * 1024 * 1024
    assert gpu["memory_percent"] == round(744 / 46068 * 100, 1)


def test_gpu_for_pids_falls_back_to_vram_when_sm_missing() -> None:
    device = {
        "available": True,
        "name": "NVIDIA L40",
        "memory_total_bytes": 1000 * 1024 * 1024,
    }
    apps = [{"pid": 10, "memory_used_bytes": 250 * 1024 * 1024}]
    gpu = gpu_for_pids({10}, device, apps, [{"pid": 10, "sm": None}])
    assert gpu["percent"] == 25.0


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
