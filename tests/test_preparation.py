import json
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.streamer import StreamManager


def preparing(tmp_path: Path, mode="nvidia"):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    manager = StreamManager(prepare_videos=True)
    manager.register("one", source, source.name, "one", True, mode)
    stream = manager._streams["one"]
    token = object()
    stream.preparation_token = token
    stream.status = "preparing"
    return manager, stream, token


def test_restored_gpu_source_is_copied_without_nvenc_if_compatible(tmp_path):
    manager, stream, token = preparing(tmp_path)
    with patch("app.streamer.probe_processing_mode", return_value="copy"), \
         patch("app.streamer.subprocess.Popen") as popen, \
         patch.object(manager, "start") as start:
        manager._prepare_video("one", token)
    popen.assert_not_called()
    start.assert_called_once_with("one")
    assert manager.get("one")["processing_mode"] == "copy"
    command = manager._build_command(stream)
    assert "h264_nvenc" not in command
    assert command[command.index("-c:v") + 1] == "copy"


@patch("app.streamer.threading.Thread.start")
@patch("app.streamer.shutil.which", return_value="/usr/bin/ffmpeg")
def test_restored_copy_records_are_rechecked_before_publishing(_which, _thread, tmp_path):
    manager, stream, _ = preparing(tmp_path, mode="copy")
    stream.status = "idle"
    with patch("app.streamer.subprocess.Popen") as popen:
        state = manager.start("one")
    assert state["status"] == "preparing"
    popen.assert_not_called()


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg is required")
@pytest.mark.parametrize("pixel_format", ["yuv444p", "yuv422p", "yuv420p10le"])
def test_h264_preparation_preserves_colours_and_decodable_frames(tmp_path, pixel_format):
    from app.streamer import probe_processing_mode

    source = tmp_path / "source.mp4"
    subprocess.run([
        "ffmpeg", "-v", "error", "-f", "lavfi", "-i",
        "testsrc2=size=160x120:rate=10", "-t", "2.5", "-c:v", "libx264",
        "-pix_fmt", pixel_format, str(source),
    ], capture_output=True, check=True, timeout=15)
    assert probe_processing_mode(source) == "transcode"
    manager = StreamManager(prepare_videos=True)
    manager.register("one", source, source.name, "one", True, "copy")
    stream = manager._streams["one"]
    token = object()
    stream.preparation_token = token
    stream.status = "preparing"
    with patch.object(manager, "start"):
        manager._prepare_video("one", token)
    assert stream.publish_path is not None, stream.error
    assert probe_processing_mode(stream.publish_path) == "copy"
    frames = []
    for path in (source, stream.publish_path):
        decoded = subprocess.run([
            "ffmpeg", "-v", "error", "-xerror", "-i", str(path),
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
        ], capture_output=True, check=True, timeout=15)
        assert decoded.stderr == b""
        assert len(decoded.stdout) == 25 * 160 * 120 * 3
        frames.append(decoded.stdout)
    # A shifted plane/stride or colour channel swap produces a large error.
    colour_error = sum(abs(a - b) for a, b in zip(*frames)) / len(frames[0])
    assert colour_error < 8
    probe = subprocess.run([
        "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_frames",
        "-show_entries", "frame=key_frame,best_effort_timestamp_time,pict_type",
        "-of", "json", str(stream.publish_path),
    ], capture_output=True, text=True, check=True, timeout=15)
    decoded_frames = json.loads(probe.stdout)["frames"]
    assert all(frame["pict_type"] != "B" for frame in decoded_frames)
    assert any(frame["key_frame"] and float(frame["best_effort_timestamp_time"]) == 2
               for frame in decoded_frames)


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg is required")
def test_nvdec_profile_normalizes_restored_copy_and_ignores_old_cache(tmp_path):
    source = tmp_path / "camera.mp4"
    subprocess.run([
        "ffmpeg", "-v", "error", "-f", "lavfi", "-i",
        "smptebars=size=2688x1520:rate=5", "-t", "1.2", "-c:v", "libx264",
        "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-bf", "0", str(source),
    ], capture_output=True, check=True, timeout=20)
    manager = StreamManager(prepare_videos=True, nvdec_compatibility=True)
    manager.register("one", source, source.name, "one", True, "copy")
    stream = manager._streams["one"]
    old_cache = tmp_path / ".prepared" / f"one-v2-{source.stat().st_size}-{source.stat().st_mtime_ns}.mp4"
    old_cache.parent.mkdir()
    old_cache.write_bytes(b"old incompatible cache")
    token = object()
    stream.preparation_token = token
    stream.status = "preparing"
    with patch.object(manager, "start"):
        manager._prepare_video("one", token)
    assert stream.publish_path is not None, stream.error
    assert stream.publish_path != source and stream.publish_path != old_cache
    info = subprocess.run([
        "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_streams",
        "-show_frames", "-of", "json", str(stream.publish_path),
    ], capture_output=True, text=True, check=True, timeout=15)
    data = json.loads(info.stdout)
    video = data["streams"][0]
    assert (video["width"], video["height"]) == (1280, 720)
    assert video["profile"] == "Constrained Baseline"
    assert video["level"] == 31 and video["has_b_frames"] == 0
    assert video["pix_fmt"] == "yuv420p" and video["sample_aspect_ratio"] == "1:1"
    assert video["r_frame_rate"] == "25/1"
    assert len(data["frames"]) == 30
    assert all(frame["pict_type"] != "B" for frame in data["frames"])
    assert any(frame["key_frame"] and float(frame["best_effort_timestamp_time"]) == 1
               for frame in data["frames"])
    colours = []
    for path in (source, stream.publish_path):
        decoded = subprocess.run([
            "ffmpeg", "-v", "error", "-xerror", "-i", str(path), "-frames:v", "1",
            "-vf", "scale=1280:720", "-pix_fmt", "rgb24", "-f", "rawvideo", "-",
        ], capture_output=True, check=True, timeout=15)
        assert decoded.stderr == b""
        colours.append(decoded.stdout)
    assert len(colours[0]) == len(colours[1]) == 1280 * 720 * 3
    assert sum(abs(a - b) for a, b in zip(*colours)) / len(colours[0]) < 8
    # Publishing the normalized file stays encoder-free.
    assert manager._build_command(stream)[manager._build_command(stream).index("-c:v") + 1] == "copy"


def test_gpu_preparation_failure_falls_back_once_and_cache_survives_restart(tmp_path):
    manager, stream, token = preparing(tmp_path)
    commands = []

    def spawn(command, **kwargs):
        commands.append(command)
        process = MagicMock()
        process.stderr = iter(["OpenEncodeSessionEx failed: out of memory (10)\n"])
        if "h264_nvenc" in command:
            process.wait.return_value = 1
        else:
            Path(command[-1]).write_bytes(b"prepared")
            process.wait.return_value = 0
        return process

    with patch("app.streamer.probe_processing_mode", return_value="transcode"), \
         patch("app.streamer.subprocess.Popen", side_effect=spawn), \
         patch.object(manager, "start"):
        manager._prepare_video("one", token)
    assert len(commands) == 2
    assert "h264_nvenc" in commands[0]
    assert "libx264" in commands[1]
    assert all("-re" not in command and "-stream_loop" not in command for command in commands)
    assert stream.publish_path.read_bytes() == b"prepared"
    assert manager.get("one")["preparation_engine"] == "cpu"
    assert manager.get("one")["processing_mode"] == "copy"

    restored = StreamManager(prepare_videos=True)
    restored.register("one", stream.video_path, "source.mp4", "one", True, "nvidia")
    restored_stream = restored._streams["one"]
    token = object()
    restored_stream.preparation_token = token
    restored_stream.status = "preparing"
    with patch("app.streamer.probe_processing_mode", return_value="transcode"), \
         patch("app.streamer.subprocess.Popen") as popen, \
         patch.object(restored, "start"):
        restored._prepare_video("one", token)
    popen.assert_not_called()
    assert restored.get("one")["preparation_engine"] == "cached"
    restored.unregister("one")
    assert not stream.publish_path.exists()
    assert stream.video_path.exists()


def test_stopping_queued_preparation_cancels_start(tmp_path):
    manager, stream, token = preparing(tmp_path)
    manager.stop("one")
    with patch("app.streamer.probe_processing_mode") as probe:
        manager._prepare_video("one", token)
    probe.assert_not_called()
    assert stream.status == "idle"
    assert manager.aggregate()["preparing"] == 0


def test_preparation_errors_remain_visible(tmp_path):
    manager, stream, token = preparing(tmp_path)
    with patch("app.streamer.probe_processing_mode", side_effect=RuntimeError("invalid source")):
        manager._prepare_video("one", token)
    assert stream.status == "error"
    assert stream.error == "invalid source"


def test_stop_all_cancels_queued_preparations(tmp_path):
    manager, stream, token = preparing(tmp_path)
    manager.stop_all()
    assert stream.status == "idle"
    assert stream.preparation_token is None


def test_preparation_does_not_use_a_realtime_loop(tmp_path):
    manager, stream, _ = preparing(tmp_path)
    command = manager._build_prepare_command(stream, "nvidia", tmp_path / "ready.mp4")
    assert "h264_nvenc" in command
    assert "-re" not in command
    assert "-stream_loop" not in command
    assert "rtsp" not in command
    assert "-movflags" in command


def test_stop_terminates_running_preparation_and_removes_partial_file(tmp_path):
    import threading
    manager, stream, token = preparing(tmp_path, mode="cpu")
    entered = threading.Event()
    released = threading.Event()
    process = MagicMock()
    process.poll.return_value = None
    process.wait.return_value = 1
    process.terminate.side_effect = released.set

    def output():
        entered.set()
        released.wait(3)
        yield "conversion cancelled\n"

    def spawn(command, **kwargs):
        Path(command[-1]).write_bytes(b"partial")
        process.stderr = output()
        return process

    with patch("app.streamer.probe_processing_mode", return_value="transcode"), \
         patch("app.streamer.subprocess.Popen", side_effect=spawn):
        worker = threading.Thread(target=manager._prepare_video, args=("one", token))
        worker.start()
        try:
            assert entered.wait(3)
            manager.stop("one")
        finally:
            released.set()
            worker.join(3)
    assert not worker.is_alive()
    process.terminate.assert_called_once()
    assert stream.status == "idle"
    assert stream.publish_path is None
    assert not list((tmp_path / ".prepared").glob("*.mp4"))
