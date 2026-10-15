import hashlib
import itertools
import shutil
import subprocess
import time
import os
import threading
from collections import deque
from array import array

import av
import pytest

from app.playback import Playback
from app.streamer import StreamManager


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.02)
    assert predicate(), "Playback condition timed out"


@pytest.fixture
def prepared_source(tmp_path):
    if not shutil.which("ffmpeg"):
        pytest.skip("FFmpeg required")
    path = tmp_path / "prepared.mp4"
    subprocess.run([
        "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=160x96:rate=25",
        "-f", "lavfi", "-i", "sine=frequency=500:sample_rate=44100", "-t", "20",
        "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
        "-g", "25", "-keyint_min", "25", "-sc_threshold", "0", "-bf", "0",
        "-pix_fmt", "yuv420p", "-c:a", "aac", str(path),
    ], capture_output=True, check=True, timeout=20)
    return path


def frame_hash(frame):
    rgb = frame.reformat(format="rgb24")
    plane = rgb.planes[0]
    data = bytes(plane)
    visible = b"".join(data[row * plane.line_size:row * plane.line_size + rgb.width * 3]
                       for row in range(rgb.height))
    return hashlib.sha256(visible).hexdigest()


def test_pause_forward_resume_keep_frames_and_timestamps_valid(prepared_source, tmp_path):
    destination = tmp_path / "controlled.ts"
    playback = Playback(prepared_source, True, destination.open("wb"))
    playback.start()
    try:
        wait_for(lambda: playback.snapshot()["position_seconds"] > .1)
        playback.pause(True)
        wait_for(lambda: playback.snapshot()["paused"])
        paused_at = playback.snapshot()["position_seconds"]
        time.sleep(1.1)
        assert playback.snapshot()["position_seconds"] == paused_at
        playback.forward(10)
        wait_for(lambda: playback.snapshot()["position_seconds"] >= paused_at + 9.9)
        assert playback.snapshot()["paused"]
        forwarded_at = playback.snapshot()["position_seconds"]
        time.sleep(.25)
        assert playback.snapshot()["position_seconds"] == forwarded_at
        playback.pause(False)
        wait_for(lambda: playback.snapshot()["position_seconds"] > forwarded_at + .3)
    finally:
        playback.stop()
        playback.join()
    assert playback.error is None
    with av.open(str(destination)) as container:
        frames = list(container.decode(video=0))
    assert frames and all((frame.width, frame.height) == (160, 96) for frame in frames)
    assert all(b.pts > a.pts for a, b in zip(frames, frames[1:]))
    hashes = [frame_hash(frame) for frame in frames]
    assert max(len(list(group)) for _, group in itertools.groupby(hashes)) >= 2
    assert any(a == b and float((second.pts - first.pts) * first.time_base) >= .9
               for a, b, first, second in zip(hashes, hashes[1:], frames, frames[1:]))
    assert len(set(hashes[-8:])) > 1
    with av.open(str(destination)) as container:
        audio = list(container.decode(audio=0))
    assert audio and all(b.pts > a.pts for a, b in zip(audio, audio[1:]))
    peaks = [max(abs(value) for value in array("f", bytes(frame.planes[0])[:frame.samples * 4]))
             for frame in audio]
    assert max(peaks) > .02
    assert sum(peak < .001 for peak in peaks) >= 25


def test_forward_wraps_and_stays_paused(prepared_source, tmp_path):
    playback = Playback(prepared_source, True, (tmp_path / "wrap.ts").open("wb"))
    playback.pause(True)
    playback.start()
    try:
        wait_for(lambda: playback.snapshot()["paused"])
        playback.forward(19)
        wait_for(lambda: playback.snapshot()["position_seconds"] >= 18)
        playback.forward(10)
        wait_for(lambda: playback.snapshot()["position_seconds"] < 10)
        assert playback.snapshot()["paused"]
    finally:
        playback.stop()
        playback.join()
    assert playback.error is None


@pytest.mark.skipif(not os.getenv("MEDIAMTX_TEST_HOST"), reason="Isolated MediaMTX required")
def test_two_existing_rtsp_readers_follow_shared_controls_without_reconnecting(prepared_source):
    host = os.environ["MEDIAMTX_TEST_HOST"]
    port = int(os.getenv("MEDIAMTX_TEST_RTSP_PORT", "8554"))
    manager = StreamManager(mediamtx_host=host, mediamtx_port=port, playback_controls=True)
    stop = threading.Event()
    frames = [deque(maxlen=1000), deque(maxlen=1000)]
    failures = []

    def read(index):
        try:
            with av.open(f"rtsp://{host}:{port}/shared", options={"rtsp_transport": "tcp",
                         "probesize": "32768", "analyzeduration": "0"}, timeout=(5, 5)) as container:
                for frame in container.decode(video=0):
                    frames[index].append((time.monotonic(), frame.pts, frame_hash(frame)))
                    if stop.is_set():
                        break
        except Exception as exc:
            failures.append(str(exc))

    readers = []
    try:
        manager.register("shared", prepared_source, prepared_source.name, "shared", True, "cpu")
        manager.start("shared")
        wait_for(lambda: manager.get("shared")["status"] == "live", timeout=10)
        publisher = manager._streams["shared"].process
        for index in range(2):
            thread = threading.Thread(target=read, args=(index,), daemon=True)
            readers.append(thread)
            thread.start()
        wait_for(lambda: all(len(values) >= 10 for values in frames), timeout=10)
        manager.playback_action("shared", "pause")
        wait_for(lambda: manager.get("shared")["playback"]["paused"])
        position = manager.get("shared")["playback"]["position_seconds"]
        # Exceed MediaMTX's default 10-second publisher read timeout.
        time.sleep(11)
        assert manager.get("shared")["playback"]["position_seconds"] == position
        now = time.monotonic()
        paused = [[item for item in values if item[0] > now - 2.2] for values in frames]
        assert all(len(values) >= 2 for values in paused), failures
        assert all(len({item[2] for item in values}) == 1 for values in paused)
        assert paused[0][-1][2] == paused[1][-1][2]
        original_hash = paused[0][-1][2]
        manager.playback_action("shared", "forward")
        wait_for(lambda: manager.get("shared")["playback"]["position_seconds"] >= position + 9.9)
        wait_for(lambda: all(values[-1][2] != original_hash for values in frames))
        assert manager.get("shared")["playback"]["paused"]
        manager.playback_action("shared", "resume")
        wait_for(lambda: not manager.get("shared")["playback"]["paused"])
        time.sleep(.6)
        now = time.monotonic()
        assert all(len({item[2] for item in values if item[0] > now - .5}) > 3 for values in frames)
        assert manager._streams["shared"].process is publisher and publisher.poll() is None
        assert not failures
        for values in frames:
            timestamps = [item[1] for item in list(values) if item[1] is not None]
            assert len(timestamps) > 15
            assert all(b > a for a, b in zip(timestamps, timestamps[1:]))
    finally:
        stop.set()
        for reader in readers:
            reader.join(timeout=2)
        manager.shutdown()
