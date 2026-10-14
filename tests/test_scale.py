"""Opt-in RTSP integration check against an isolated MediaMTX server."""
import json
import os
import shutil
import subprocess
import time
import urllib.request

import pytest

from app.streamer import StreamManager


@pytest.mark.skipif(not os.getenv("MEDIAMTX_TEST_HOST"), reason="Isolated MediaMTX required")
def test_forty_sources_publish_without_forty_encoder_sessions(tmp_path):
    host = os.environ["MEDIAMTX_TEST_HOST"]
    compatible = tmp_path / "compatible.mp4"
    incompatible = tmp_path / "incompatible.avi"
    for target, codec in [(compatible, "libx264"), (incompatible, "mjpeg")]:
        subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
            "-i", "testsrc2=size=64x64:rate=5", "-t", "2", "-c:v", codec,
            "-threads", "1", str(target),
        ], check=True, capture_output=True, timeout=15)

    manager = StreamManager(mediamtx_host=host, prepare_videos=True)
    try:
        for number in range(40):
            template = compatible if number < 36 else incompatible
            source = tmp_path / f"source-{number}{template.suffix}"
            shutil.copyfile(template, source)
            # Simulate the user's existing registry entries with NVIDIA selected.
            manager.register(str(number), source, source.name, f"camera-{number}", True, "nvidia")
        manager.start_all()
        deadline = time.monotonic() + 60
        ready = []
        while time.monotonic() < deadline:
            with urllib.request.urlopen(f"http://{host}:9997/v3/paths/list?itemsPerPage=100", timeout=3) as response:
                ready = [item for item in json.load(response)["items"] if item["ready"]]
            if len(ready) == 40 and manager.aggregate()["live"] == 40:
                break
            time.sleep(.2)
        assert len(ready) == 40, [manager.get(s["id"]) for s in manager.list() if s["status"] == "error"]
        assert manager.aggregate()["live"] == 40
        assert manager.aggregate()["error"] == 0
        assert all(s["processing_mode"] == "copy" for s in manager.list())
        for number in (0, 39):
            result = subprocess.run([
                "ffprobe", "-v", "error", "-rtsp_transport", "tcp",
                "-show_entries", "stream=codec_name", "-of", "json",
                f"rtsp://{host}:8554/camera-{number}",
            ], capture_output=True, text=True, timeout=15, check=True)
            assert json.loads(result.stdout)["streams"][0]["codec_name"] == "h264"
    finally:
        manager.shutdown()
