"""Opt-in decoded-frame checks against an isolated MediaMTX server."""
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.streamer import StreamManager


@pytest.mark.skipif(not os.getenv("MEDIAMTX_TEST_HOST"), reason="Isolated MediaMTX required")
@pytest.mark.parametrize("pixel_format,b_frames", [
    ("yuv420p", 0), ("yuv420p", 2), ("yuv444p", 2),
    ("yuv422p", 2), ("yuv420p10le", 2),
])
def test_late_reader_decodes_complete_coloured_frames_across_loops(tmp_path, pixel_format, b_frames):
    source = tmp_path / "bars.mp4"
    subprocess.run([
        "ffmpeg", "-v", "error", "-f", "lavfi", "-i",
        "smptebars=size=160x120:rate=10", "-t", "1", "-c:v", "libx264",
        "-pix_fmt", pixel_format, "-bf", str(b_frames), str(source),
    ], capture_output=True, check=True, timeout=15)
    reference = subprocess.run([
        "ffmpeg", "-v", "error", "-i", str(source), "-frames:v", "1",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
    ], capture_output=True, check=True, timeout=15).stdout
    host = os.environ["MEDIAMTX_TEST_HOST"]
    port = int(os.getenv("MEDIAMTX_TEST_RTSP_PORT", "8554"))
    manager = StreamManager(mediamtx_host=host, mediamtx_port=port, prepare_videos=True)
    try:
        # Simulate a copy record persisted by an older codec-only probe.
        manager.register("bars", source, source.name, "playback-bars", True, "copy")
        manager.start("bars")
        deadline = time.monotonic() + 15
        while manager.get("bars")["status"] in {"preparing", "starting"} and time.monotonic() < deadline:
            time.sleep(.1)
        assert manager.get("bars")["status"] == "live", manager.get("bars")
        result = subprocess.run([
            "ffmpeg", "-v", "error", "-xerror", "-rtsp_transport", "tcp",
            "-i", f"rtsp://{host}:{port}/playback-bars", "-frames:v", "30",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
        ], capture_output=True, timeout=20)
        assert result.returncode == 0, result.stderr.decode()
        assert not result.stderr, result.stderr.decode()
        assert len(result.stdout) == 30 * len(reference)
        colour_error = sum(abs(a - b) for a, b in zip(result.stdout, reference * 30)) / len(result.stdout)
        assert colour_error < 8
    finally:
        manager.shutdown()


@pytest.mark.skipif(not os.getenv("MEDIAMTX_TEST_HOST"), reason="Isolated MediaMTX required")
@pytest.mark.parametrize("pixel_format", ["yuv420p", "yuv444p"])
def test_opencv_reader_preserves_colours_across_loops(tmp_path, pixel_format):
    cv2 = pytest.importorskip("cv2")
    import numpy as np

    source = tmp_path / "bars.mp4"
    subprocess.run([
        "ffmpeg", "-v", "error", "-f", "lavfi", "-i",
        "smptebars=size=640x360:rate=10", "-t", "1", "-c:v", "libx264",
        "-pix_fmt", pixel_format, "-bf", "0", str(source),
    ], capture_output=True, check=True, timeout=15)
    reference = subprocess.run([
        "ffmpeg", "-v", "error", "-i", str(source), "-frames:v", "1",
        "-f", "rawvideo", "-pix_fmt", "bgr24", "-",
    ], capture_output=True, check=True, timeout=15).stdout
    host = os.environ["MEDIAMTX_TEST_HOST"]
    port = int(os.getenv("MEDIAMTX_TEST_RTSP_PORT", "8554"))
    manager = StreamManager(mediamtx_host=host, mediamtx_port=port, prepare_videos=True)
    try:
        manager.register("bars", source, source.name, "opencv-bars", True, "copy")
        manager.start("bars")
        deadline = time.monotonic() + 15
        while manager.get("bars")["status"] in {"preparing", "starting"} and time.monotonic() < deadline:
            time.sleep(.1)
        assert manager.get("bars")["status"] == "live", manager.get("bars")
        snapshot = tmp_path / "decoded.png"
        result = subprocess.run([
            sys.executable, str(Path(__file__).resolve().parents[1] / "scripts/check_rtsp.py"),
            f"rtsp://{host}:{port}/opencv-bars", "--frames", "30", "--snapshot", str(snapshot),
        ], capture_output=True, text=True, timeout=25)
        assert result.returncode == 0, result.stderr
        assert not result.stderr, result.stderr
        frame = cv2.imread(str(snapshot))
        assert frame is not None and frame.shape == (360, 640, 3)
        expected = np.frombuffer(reference, dtype=np.uint8).reshape(frame.shape)
        assert np.abs(frame.astype(float) - expected.astype(float)).mean() < 8
    finally:
        manager.shutdown()
