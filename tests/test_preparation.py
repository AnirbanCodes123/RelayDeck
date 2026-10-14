from pathlib import Path
from unittest.mock import MagicMock, patch

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
