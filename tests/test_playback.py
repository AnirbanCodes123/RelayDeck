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


@pytest.mark.skipif(not os.getenv("MEDIAMTX_TEST_HOST"), reason="Isolated MediaMTX required")
@pytest.mark.parametrize("cuda", [False, True])
def test_compatibility_profile_with_edge_agent_rawvideo_options(tmp_path, cuda):
    if cuda and not os.getenv("NVDEC_TEST"):
        pytest.skip("NVIDIA GPU and CUDA-enabled FFmpeg required")
    source = tmp_path / "camera.mp4"
    subprocess.run([
        "ffmpeg", "-v", "error", "-f", "lavfi", "-i",
        "smptebars=size=2688x1520:rate=5", "-t", "1.2", "-c:v", "libx264",
        "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(source),
    ], capture_output=True, check=True, timeout=20)
    host = os.environ["MEDIAMTX_TEST_HOST"]
    port = int(os.getenv("MEDIAMTX_TEST_RTSP_PORT", "8554"))
    manager = StreamManager(mediamtx_host=host, mediamtx_port=port,
                            prepare_videos=True, nvdec_compatibility=True)
    try:
        manager.register("compat", source, source.name, "compat-bars", True, "copy")
        manager.start("compat")
        deadline = time.monotonic() + 20
        while manager.get("compat")["status"] in {"preparing", "starting"} and time.monotonic() < deadline:
            time.sleep(.1)
        assert manager.get("compat")["status"] == "live", manager.get("compat")
        command = ["ffmpeg", "-v", "error", "-rtsp_transport", "tcp",
                   "-fflags", "+genpts+nobuffer+discardcorrupt", "-flags", "low_delay"]
        if cuda:
            command += ["-hwaccel", "cuda"]
        command += ["-i", f"rtsp://{host}:{port}/compat-bars", "-an", "-sn", "-dn",
                    "-vsync", "0", "-frames:v", "65", "-pix_fmt", "bgr24", "-f", "rawvideo", "-"]
        raw_frames = tmp_path / "frames.bgr"
        with raw_frames.open("wb") as output:
            result = subprocess.run(command, stdout=output, stderr=subprocess.PIPE, timeout=30)
        assert result.returncode == 0, result.stderr.decode()
        assert not result.stderr, result.stderr.decode()
        size = 2688 * 1520 * 3
        assert raw_frames.stat().st_size == size * 65
        reference = subprocess.run([
            "ffmpeg", "-v", "error", "-i", str(manager._streams["compat"].publish_path),
            "-frames:v", "1", "-pix_fmt", "bgr24", "-f", "rawvideo", "-",
        ], capture_output=True, check=True, timeout=15).stdout
        # Compare frames before and after multiple loops for plane/stride drift.
        with raw_frames.open("rb") as decoded:
            for number in (0, 32, 64):
                decoded.seek(number * size)
                frame = decoded.read(size)
                assert sum(abs(a - b) for a, b in zip(frame, reference)) / size < 2
    finally:
        manager.shutdown()
