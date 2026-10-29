import json
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.streamer import (
    StreamManager,
    probe_processing_mode,
    resolve_processing_mode,
)


def probe_result(video_codec: str, audio_codec: str | None = None, **metadata) -> MagicMock:
    streams = [{"codec_type": "video", "codec_name": video_codec,
                "pix_fmt": "yuv420p", "width": 320, "height": 240,
                "has_b_frames": 0, **metadata}]
    if audio_codec:
        streams.append({"codec_type": "audio", "codec_name": audio_codec})
    return MagicMock(stdout=json.dumps({"streams": streams}))


@patch("app.streamer.subprocess.run")
def test_probe_selects_copy_for_h264_aac(run: MagicMock, tmp_path: Path) -> None:
    run.return_value = probe_result("h264", "aac")
    assert probe_processing_mode(tmp_path / "video.mp4") == "copy"


@pytest.mark.parametrize("metadata", [
    {"pix_fmt": "yuv444p"}, {"pix_fmt": "yuv422p"},
    {"pix_fmt": "yuv420p10le"}, {"pix_fmt": None},
    {"width": 321}, {"height": 241}, {"width": 0},
    {"has_b_frames": 2}, {"has_b_frames": None},
])
@patch("app.streamer.subprocess.run")
def test_incompatible_h264_is_normalized(run, tmp_path, metadata):
    run.return_value = probe_result("h264", "aac", **metadata)
    assert probe_processing_mode(tmp_path / "video.mp4") == "transcode"


@patch("app.streamer.subprocess.run")
def test_h264_avi_is_transcoded_for_reliable_timestamps(run: MagicMock, tmp_path: Path) -> None:
    run.return_value = probe_result("h264", "aac")
    assert probe_processing_mode(tmp_path / "video.AVI") == "transcode"


def test_restored_avi_copy_mode_is_repaired(tmp_path: Path) -> None:
    manager = StreamManager()
    state = manager.register("avi", tmp_path / "old.avi", "old.avi", "avi", True, "copy")
    assert state["processing_mode"] == "cpu"
    assert "libx264" in manager._build_command(manager._streams["avi"])


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
    assert resolve_processing_mode("copy", "auto") == "copy"
    assert resolve_processing_mode("copy", "cpu") == "copy"


@patch(
    "app.streamer.nvidia_gpu_status",
    return_value={"available": False, "name": None},
)
def test_nvidia_selection_falls_back_to_cpu(_gpu_status: MagicMock) -> None:
    assert resolve_processing_mode("transcode", "nvidia") == "cpu"
    assert resolve_processing_mode("copy", "nvidia") == "copy"


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


def test_unregister_all_clears_the_registry(tmp_path: Path) -> None:
    manager = StreamManager(max_streams=2)
    manager.register("one", tmp_path / "one.mp4", "one.mp4", "camera1", True, "copy")
    manager.register("two", tmp_path / "two.mp4", "two.mp4", "camera2", True, "nvidia")
    removed = manager.unregister_all()
    assert {stream.id for stream in removed} == {"one", "two"}
    assert manager.list() == []
    assert manager.aggregate()["total"] == 0


@patch("app.streamer.threading.Thread.start")
@patch("app.streamer.probe_processing_mode", return_value="copy")
@patch("app.streamer.shutil.which", return_value="/usr/bin/ffmpeg")
@patch("app.streamer.subprocess.Popen")
def test_stream_processes_have_independent_lifecycles(
    popen: MagicMock,
    _which: MagicMock,
    _probe: MagicMock,
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
    with patch("app.streamer.usable_audio_indices", return_value=()):
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


@patch("app.streamer.threading.Thread.start")
@patch("app.streamer.probe_processing_mode", return_value="transcode")
@patch("app.streamer.shutil.which", return_value="/usr/bin/ffmpeg")
@patch("app.streamer.subprocess.Popen")
def test_live_transcoding_repairs_saved_incompatible_copy_mode(popen, _which, _probe, _thread, tmp_path):
    popen.return_value.poll.return_value = None
    manager = StreamManager(prepare_videos=False)
    manager.register("one", tmp_path / "high444.mp4", "high444.mp4", "one", True, "copy")
    with patch("app.streamer.usable_audio_indices", return_value=()):
        manager.start("one")
    command = popen.call_args.args[0]
    assert command[command.index("-c:v") + 1] == "libx264"
    assert command[command.index("-pix_fmt") + 1] == "yuv420p"


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


@pytest.mark.parametrize("mode", ["cpu", "nvidia"])
def test_transcoding_pads_frames_for_yuv420p(mode: str, tmp_path: Path) -> None:
    manager = StreamManager()
    manager.register("avi", tmp_path / "video.avi", "video.avi", "avi", True, mode)
    command = manager._build_command(manager._streams["avi"])
    assert command[command.index("-vf") + 1] == "pad=ceil(iw/2)*2:ceil(ih/2)*2"


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg is required")
def test_odd_sized_avi_can_be_transcoded(tmp_path: Path) -> None:
    video = tmp_path / "odd.avi"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=size=321x241:rate=25",
            "-t", "0.2", "-c:v", "mjpeg", str(video),
        ],
        capture_output=True, text=True, check=True, timeout=15,
    )
    manager = StreamManager()
    mode = probe_processing_mode(video)
    assert mode == "transcode"
    manager.register("avi", video, video.name, "avi", False, mode)
    command = manager._build_command(manager._streams["avi"])
    # Exercise the actual input/encoder path without needing an RTSP server.
    command = command[:command.index("-avoid_negative_ts")] + ["-f", "null", "-"]
    result = subprocess.run(command, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


def test_publisher_failure_preserves_encoder_error(tmp_path: Path, caplog) -> None:
    manager = StreamManager()
    manager.register("avi", tmp_path / "video.avi", "video.avi", "avi", False, "cpu")
    process = MagicMock()
    process.stderr = iter([
        "width not divisible by 2 (321x241)\n",
        "Nothing was written into output file\n",
    ])
    process.wait.return_value = 1
    manager._streams["avi"].process = process
    manager._capture_output("avi", process)
    state = manager.get("avi")
    assert state["status"] == "error"
    assert "width not divisible" in state["error"]
    assert "width not divisible" in caplog.text


@patch("app.streamer.shutil.which", side_effect=lambda name: f"/usr/bin/{name}")
@patch("app.streamer.subprocess.run")
def test_nvidia_status_uses_smi_and_encoder_list(
    run: MagicMock, _which: MagicMock
) -> None:
    from app import streamer

    streamer._nvidia_status_cache = None

    def fake_run(command, **_kwargs):
        result = MagicMock()
        result.returncode = 0
        result.stderr = ""
        if command[0].endswith("nvidia-smi"):
            result.stdout = "NVIDIA L40\n"
        else:
            result.stdout = " V....D h264_nvenc           NVIDIA NVENC H.264 encoder\n"
        return result

    run.side_effect = fake_run
    status = streamer.nvidia_gpu_status()
    assert status["available"] is True
    assert status["name"] == "NVIDIA L40"
