import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.streamer import (
    StreamManager,
    probe_processing_mode,
    resolve_processing_mode,
)


def probe_result(video_codec: str, audio_codec: str | None = None) -> MagicMock:
    streams = [{"codec_type": "video", "codec_name": video_codec}]
    if audio_codec:
        streams.append({"codec_type": "audio", "codec_name": audio_codec})
    return MagicMock(stdout=json.dumps({"streams": streams}))


@patch("app.streamer.subprocess.run")
def test_probe_selects_copy_for_h264_aac(run: MagicMock, tmp_path: Path) -> None:
    run.return_value = probe_result("h264", "aac")
    assert probe_processing_mode(tmp_path / "video.mp4") == "copy"


@patch("app.streamer.subprocess.run")
def test_probe_selects_transcode_for_other_codecs(
    run: MagicMock, tmp_path: Path
) -> None:
    run.return_value = probe_result("hevc", "aac")
    assert probe_processing_mode(tmp_path / "video.mkv") == "transcode"


@patch("app.streamer.subprocess.run")
def test_probe_rejects_files_without_video(run: MagicMock, tmp_path: Path) -> None:
    run.return_value = MagicMock(
        stdout=json.dumps({"streams": [{"codec_type": "audio", "codec_name": "aac"}]})
    )
    with pytest.raises(RuntimeError, match="does not contain a video"):
        probe_processing_mode(tmp_path / "audio.mp4")


@patch(
    "app.streamer.nvidia_gpu_status",
    return_value={"available": True, "name": "Test GPU"},
)
def test_processing_engine_selection_uses_nvidia_when_available(
    _gpu_status: MagicMock,
) -> None:
    assert resolve_processing_mode("transcode", "auto") == "nvidia"
    assert resolve_processing_mode("transcode", "nvidia") == "nvidia"
    assert resolve_processing_mode("transcode", "cpu") == "cpu"
    assert resolve_processing_mode("copy", "nvidia") == "copy"


@patch(
    "app.streamer.nvidia_gpu_status",
    return_value={"available": False, "name": None},
)
def test_nvidia_selection_falls_back_to_cpu(_gpu_status: MagicMock) -> None:
    assert resolve_processing_mode("transcode", "nvidia") == "cpu"


def test_manager_enforces_endpoint_uniqueness_and_capacity(tmp_path: Path) -> None:
    manager = StreamManager(max_streams=2)
    manager.register("one", tmp_path / "one.mp4", "one.mp4", "camera1", True, "copy")
    with pytest.raises(RuntimeError, match="already in use"):
        manager.register(
            "duplicate", tmp_path / "two.mp4", "two.mp4", "camera1", True, "copy"
        )
    manager.register("two", tmp_path / "two.mp4", "two.mp4", "camera2", True, "copy")
    with pytest.raises(RuntimeError, match="capacity"):
        manager.register(
            "three", tmp_path / "three.mp4", "three.mp4", "camera3", True, "copy"
        )


@patch("app.streamer.threading.Thread.start")
@patch("app.streamer.shutil.which", return_value="/usr/bin/ffmpeg")
@patch("app.streamer.subprocess.Popen")
def test_stream_processes_have_independent_lifecycles(
    popen: MagicMock,
    _which: MagicMock,
    _thread_start: MagicMock,
    tmp_path: Path,
) -> None:
    first = MagicMock()
    second = MagicMock()
    first.poll.return_value = None
    second.poll.return_value = None
    first.wait.return_value = 0
    second.wait.return_value = 0
    popen.side_effect = [first, second]

    manager = StreamManager(max_streams=2)
    manager.register("one", tmp_path / "one.mp4", "one.mp4", "camera1", True, "copy")
    manager.register(
        "two", tmp_path / "two.mov", "two.mov", "camera2", False, "transcode"
    )
    manager.start("one")
    manager.start("two")

    assert manager.aggregate()["starting"] == 2
    assert "-c:v" in popen.call_args_list[0].args[0]
    assert "copy" in popen.call_args_list[0].args[0]
    assert "libx264" in popen.call_args_list[1].args[0]

    manager.stop("one")
    first.terminate.assert_called_once()
    second.terminate.assert_not_called()
    assert manager.get("one")["status"] == "idle"
    assert manager.get("two")["status"] == "starting"


def test_nvidia_stream_builds_nvenc_command(tmp_path: Path) -> None:
    manager = StreamManager(max_streams=1)
    manager.register(
        "gpu", tmp_path / "gpu.mov", "gpu.mov", "camera-gpu", True, "nvidia"
    )
    stream = manager._streams["gpu"]
    command = manager._build_command(stream)
    assert "h264_nvenc" in command
    assert "-nostdin" in command
    assert "-tune" in command
    assert "ll" in command


@patch("app.streamer.shutil.which", side_effect=lambda name: f"/usr/bin/{name}")
@patch("app.streamer.subprocess.run")
def test_nvidia_status_accepts_nvenc_probe(
    run: MagicMock, _which: MagicMock
) -> None:
    from app import streamer

    streamer._nvidia_status_cache = None

    def fake_run(command, **_kwargs):
        result = MagicMock()
        result.returncode = 0
        result.stdout = "NVIDIA L40\n" if command[0] == "nvidia-smi" else ""
        result.stderr = ""
        return result

    run.side_effect = fake_run
    status = streamer.nvidia_gpu_status()
    assert status["available"] is True
    assert status["name"] == "NVIDIA L40"
    probe = next(call.args[0] for call in run.call_args_list if call.args[0][0] == "ffmpeg")
    assert "256x256" in " ".join(probe)
    assert "-nostdin" in probe
    assert probe[probe.index("-frames:v") + 1] == "2"
