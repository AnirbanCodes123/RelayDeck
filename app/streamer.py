from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path
from typing import Any


ACTIVE_STATUSES = {"preparing", "starting", "live", "stopping"}
logger = logging.getLogger(__name__)

_nvidia_status_lock = threading.Lock()
_nvidia_status_cache: dict[str, Any] | None = None


@lru_cache(maxsize=1)
def cpu_status() -> dict[str, Any]:
    name = platform.processor().strip()
    try:
        cpu_info = Path("/proc/cpuinfo").read_text(encoding="utf-8")
        for key in ("model name", "Hardware", "Processor"):
            for line in cpu_info.splitlines():
                if line.startswith(key) and ":" in line:
                    name = line.split(":", 1)[1].strip()
                    break
            if name:
                break
    except OSError:
        pass
    return {"available": True, "name": name or platform.machine() or "Unknown CPU"}


def _find_binary(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    for candidate in (Path("/usr/bin") / name, Path("/usr/local/bin") / name):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _run_command(command: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
        stdin=subprocess.DEVNULL,
        env=os.environ,
    )


def _nvidia_smi_name() -> str | None:
    nvidia_smi = _find_binary("nvidia-smi")
    if not nvidia_smi:
        return None
    result = _run_command(
        [nvidia_smi, "--query-gpu=name", "--format=csv,noheader"],
        timeout=5,
    )
    if result.returncode != 0 or not result.stdout.strip():
        logger.warning("nvidia-smi GPU query failed: %s", result.stderr.strip() or result.stdout)
        return None
    return result.stdout.splitlines()[0].strip()


def _ffmpeg_has_nvenc() -> bool:
    ffmpeg = _find_binary("ffmpeg")
    if not ffmpeg:
        return False
    result = _run_command([ffmpeg, "-hide_banner", "-encoders"], timeout=10)
    output = f"{result.stdout}\n{result.stderr}"
    return "h264_nvenc" in output


def _detect_nvidia_gpu() -> dict[str, Any]:
    """Treat a visible GPU plus NVENC encoder as available.

    A synthetic lavfi encode to stdout (`-f null -`) fails inside subprocess
    pipes even when the same command succeeds in an interactive shell.
    """
    try:
        name = _nvidia_smi_name()
        has_nvenc = _ffmpeg_has_nvenc()
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("NVIDIA detection failed: %s", exc)
        return {"available": False, "name": None, "error": str(exc)}
    if name and has_nvenc:
        return {"available": True, "name": name, "error": None}
    if name:
        return {
            "available": False,
            "name": name,
            "error": "FFmpeg has no h264_nvenc encoder",
        }
    if has_nvenc:
        return {
            "available": False,
            "name": None,
            "error": "nvidia-smi did not report a GPU",
        }
    return {
        "available": False,
        "name": None,
        "error": "NVIDIA GPU was not detected",
    }


def nvidia_gpu_status() -> dict[str, Any]:
    """Return cached NVENC status. Successful probes are reused; failures retry."""
    global _nvidia_status_cache
    with _nvidia_status_lock:
        if _nvidia_status_cache is not None and _nvidia_status_cache.get("available"):
            return dict(_nvidia_status_cache)
        detected = _detect_nvidia_gpu()
        if detected["available"]:
            _nvidia_status_cache = detected
        return dict(detected)


def resolve_processing_mode(media_mode: str, requested_engine: str) -> str:
    if media_mode == "copy":
        return "copy"
    gpu_available = nvidia_gpu_status()["available"]
    if requested_engine == "nvidia":
        if gpu_available:
            return "nvidia"
        return "copy" if media_mode == "copy" else "cpu"
    if media_mode == "copy":
        return "copy"
    if requested_engine != "cpu" and gpu_available:
        return "nvidia"
    return "cpu"


def probe_processing_mode(video_path: Path) -> str:
    """Copy only 8-bit 4:2:0 H.264/AAC that common RTSP decoders support."""
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "stream=codec_type,codec_name,pix_fmt,width,height,has_b_frames",
                "-of",
                "json",
                str(video_path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
            stdin=subprocess.DEVNULL,
        )
        streams = json.loads(result.stdout).get("streams", [])
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not inspect the uploaded video: {exc}") from exc

    video_streams = [
        stream
        for stream in streams
        if stream.get("codec_type") == "video"
    ]
    audio_codecs = [
        stream.get("codec_name")
        for stream in streams
        if stream.get("codec_type") == "audio"
    ]
    if not video_streams:
        raise RuntimeError("The selected file does not contain a video stream.")
    # H.264 in AVI can lack reliable presentation timestamps, especially with
    # B-frames. Decode/re-encode to create timestamps that remain valid on loops.
    if video_path.suffix.lower() == ".avi":
        return "transcode"
    video = video_streams[0]
    # H.264 alone does not imply player compatibility: High 10/4:2:2/4:4:4
    # sources can produce coloured blocks or fail in hardware RTSP decoders.
    compatible_video = (
        video.get("codec_name") == "h264"
        and video.get("pix_fmt") in {"yuv420p", "yuvj420p"}
        # Reordered B-frames may refer to pictures preceding a reader's join
        # point or a loop boundary. Normalize these files to closed, B-free GOPs.
        and video.get("has_b_frames") == 0
        and isinstance(video.get("width"), int)
        and isinstance(video.get("height"), int)
        and video["width"] > 0 and video["width"] % 2 == 0
        and video["height"] > 0 and video["height"] % 2 == 0
    )
    return (
        "copy"
        if compatible_video and all(codec == "aac" for codec in audio_codecs)
        else "transcode"
    )


@dataclass
class ManagedStream:
    id: str
    video_path: Path
    original_filename: str
    stream_name: str
    loop: bool
    processing_mode: str
    status: str = "idle"
    started_at: float | None = None
    error: str | None = None
    process: subprocess.Popen[str] | None = field(default=None, repr=False)
    logs: deque[str] = field(default_factory=lambda: deque(maxlen=100), repr=False)
    publish_path: Path | None = field(default=None, repr=False)
    preparation_engine: str | None = None
    preparation_token: object | None = field(default=None, repr=False)


class StreamManager:
    """Supervises independent FFmpeg publishers for a single portal instance."""

    def __init__(
        self,
        mediamtx_host: str = "127.0.0.1",
        mediamtx_port: int = 8554,
        public_host: str = "localhost",
        max_streams: int = 110,
        prepare_videos: bool = False,
        nvdec_compatibility: bool = False,
    ) -> None:
        self.mediamtx_host = mediamtx_host
        self.mediamtx_port = mediamtx_port
        self.public_host = public_host
        self.max_streams = max_streams
        self.prepare_videos = prepare_videos
        self.nvdec_compatibility = nvdec_compatibility
        self._prepare_lock = threading.Lock()
        self._streams: dict[str, ManagedStream] = {}
        self._names: dict[str, str] = {}
        self._lock = threading.RLock()

    def register(
        self,
        stream_id: str,
        video_path: Path,
        original_filename: str,
        stream_name: str,
        loop: bool,
        processing_mode: str,
    ) -> dict[str, Any]:
        with self._lock:
            if stream_id in self._streams:
                return self._serialize(self._streams[stream_id])
            if len(self._streams) >= self.max_streams:
                raise RuntimeError(f"Stream capacity reached ({self.max_streams}).")
            if stream_name in self._names:
                raise RuntimeError(f"RTSP endpoint '{stream_name}' is already in use.")
            # Also repair copy mode in AVI records restored from older versions.
            if processing_mode == "copy" and video_path.suffix.lower() == ".avi":
                processing_mode = "cpu"
            stream = ManagedStream(
                id=stream_id,
                video_path=video_path,
                original_filename=original_filename,
                stream_name=stream_name,
                loop=loop,
                processing_mode=processing_mode,
            )
            self._streams[stream_id] = stream
            self._names[stream_name] = stream_id
            return self._serialize(stream)

    def unregister(self, stream_id: str) -> ManagedStream:
        self.stop(stream_id)
        with self._lock:
            stream = self._require(stream_id)
            self._names.pop(stream.stream_name, None)
            removed = self._streams.pop(stream_id)
        self._remove_prepared_files(removed)
        return removed

    def unregister_all(self) -> list[ManagedStream]:
        self.stop_all()
        with self._lock:
            streams = list(self._streams.values())
            self._streams.clear()
            self._names.clear()
        for stream in streams:
            self._remove_prepared_files(stream)
        return streams

    @staticmethod
    def _remove_prepared_files(stream: ManagedStream) -> None:
        for path in (stream.video_path.parent / ".prepared").glob(f"{stream.id}-*.mp4"):
            path.unlink(missing_ok=True)

    def start(self, stream_id: str) -> dict[str, Any]:
        if not shutil.which("ffmpeg"):
            raise RuntimeError("FFmpeg is not installed or is not available on PATH.")
        with self._lock:
            stream = self._require(stream_id)
            if stream.status == "preparing":
                return self._serialize(stream)
            if stream.process and stream.process.poll() is None:
                return self._serialize(stream)
            active_count = sum(
                item.status in ACTIVE_STATUSES for item in self._streams.values()
            )
            if active_count >= self.max_streams:
                raise RuntimeError(f"Active stream capacity reached ({self.max_streams}).")
            # Re-probe saved copy records too; older versions checked only the
            # codec name and could persist incompatible H.264 pixel formats.
            if self.prepare_videos and not stream.publish_path:
                token = object()
                stream.preparation_token = token
                stream.status = "preparing"
                stream.error = None
                stream.started_at = None
                stream.logs.clear()
                threading.Thread(target=self._prepare_video, args=(stream_id, token),
                                 daemon=True, name=f"prepare-{stream_id[:8]}").start()
                return self._serialize(stream)
            if not self.prepare_videos and stream.processing_mode == "copy":
                if self.nvdec_compatibility or probe_processing_mode(stream.video_path) != "copy":
                    stream.processing_mode = "cpu"
            command = self._build_command(stream)
            stream.logs.clear()
            stream.error = None
            try:
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1,
                )
            except OSError as exc:
                raise RuntimeError(f"Could not start FFmpeg: {exc}") from exc
            stream.process = process
            stream.status = "starting"
            stream.started_at = time.time()

        threading.Thread(
            target=self._capture_output,
            args=(stream_id, process),
            daemon=True,
            name=f"ffmpeg-log-{stream_id[:8]}",
        ).start()
        threading.Thread(
            target=self._confirm_started,
            args=(stream_id, process),
            daemon=True,
            name=f"ffmpeg-ready-{stream_id[:8]}",
        ).start()
        return self.get(stream_id)

    def _prepare_video(self, stream_id: str, token: object) -> None:
        """Convert each source once, serially, then publish without an encoder."""
        temporary: Path | None = None
        try:
            # A single conversion avoids NVENC session/VRAM exhaustion and CPU storms.
            with self._prepare_lock:
                with self._lock:
                    stream = self._streams.get(stream_id)
                    if not stream or stream.preparation_token is not token:
                        return
                # Repair previously saved GPU records for already compatible inputs.
                if not self.nvdec_compatibility and probe_processing_mode(stream.video_path) == "copy":
                    ready = stream.video_path
                    engine = "copy"
                else:
                    stat = stream.video_path.stat()
                    directory = stream.video_path.parent / ".prepared"
                    directory.mkdir(parents=True, exist_ok=True)
                    profile = "nvdec720-v1" if self.nvdec_compatibility else "v2"
                    ready = directory / f"{stream.id}-{profile}-{stat.st_size}-{stat.st_mtime_ns}.mp4"
                    engine = "cached"
                    if not ready.is_file():
                        temporary = ready.with_name(f"{ready.stem}-{id(token)}.tmp.mp4")
                        engines = ["nvidia", "cpu"] if stream.processing_mode == "nvidia" else ["cpu"]
                        for engine in engines:
                            command = self._build_prepare_command(stream, engine, temporary)
                            with self._lock:
                                if stream.preparation_token is not token:
                                    return
                                process = subprocess.Popen(
                                    command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.PIPE, text=True, bufsize=1)
                                stream.process = process
                            if process.stderr:
                                for line in process.stderr:
                                    with self._lock:
                                        if stream.preparation_token is token and line.strip():
                                            stream.logs.append(line.strip())
                            return_code = process.wait()
                            with self._lock:
                                if stream.preparation_token is not token:
                                    return
                                stream.process = None
                            if return_code == 0:
                                temporary.replace(ready)
                                break
                            temporary.unlink(missing_ok=True)
                            if engine == "nvidia":
                                logger.warning("GPU preparation failed for %s; retrying once on CPU", stream.stream_name)
                            else:
                                raise RuntimeError("\n".join(list(stream.logs)[-20:]) or "Video preparation failed.")
                with self._lock:
                    if stream.preparation_token is not token:
                        return
                    stream.publish_path = ready
                    stream.preparation_engine = engine
                    stream.status = "idle"
                    self.start(stream_id)
                    stream.preparation_token = None
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            with self._lock:
                stream = self._streams.get(stream_id)
                if stream and stream.preparation_token is token:
                    stream.process = None
                    stream.preparation_token = None
                    stream.status = "error"
                    stream.error = str(exc)
                    logger.error("Video preparation failed for %s: %s", stream.stream_name, exc)
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)

    def _build_prepare_command(self, stream: ManagedStream, engine: str, destination: Path) -> list[str]:
        source = replace(stream, processing_mode=engine, publish_path=None, loop=False)
        command = self._build_command(source)
        command = command[:command.index("-avoid_negative_ts")]
        command.remove("-re")
        return command + ["-y", "-movflags", "+faststart", str(destination)]

    def _build_command(self, stream: ManagedStream) -> list[str]:
        publish_url = (
            f"rtsp://{self.mediamtx_host}:{self.mediamtx_port}/{stream.stream_name}"
        )
        command = [
            "ffmpeg",
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "warning",
            "-filter_threads",
            "1",
            "-threads",
            "1",
            "-fflags",
            "+genpts",
            "-re",
        ]
        if stream.loop:
            command += ["-stream_loop", "-1"]
        command += [
            "-i",
            str(stream.publish_path or stream.video_path),
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
        ]
        mode = "copy" if stream.publish_path else stream.processing_mode
        if mode != "copy":
            # AVI codecs such as MJPEG can carry odd-sized frames. Both H.264
            # encoders require even dimensions for yuv420p; pad rather than crop.
            if self.nvdec_compatibility:
                # Fix display geometry before publishing: rawvideo consumers
                # allocate frames using dimensions probed on another connection.
                # Use DAR to preserve even non-square-pixel source proportions.
                filters = (
                    "scale=w='max(2,trunc(min(1280,720*dar)/2)*2)':"
                    "h='max(2,trunc(min(720,1280/dar)/2)*2)',"
                    "setsar=1,pad=1280:720:(ow-iw)/2:(oh-ih)/2,"
                    "fps=25,format=yuv420p"
                )
            else:
                filters = "pad=ceil(iw/2)*2:ceil(ih/2)*2"
            command += ["-vf", filters]
        if mode == "copy":
            command += [
                "-c:v",
                "copy",
                "-bsf:v",
                "h264_mp4toannexb",
                "-c:a",
                "copy",
            ]
        elif mode == "nvidia":
            command += [
                "-c:v",
                "h264_nvenc",
                "-preset",
                "p4",
                "-tune",
                "ll",
                "-rc",
                "vbr",
                "-cq",
                "23",
                "-b:v",
                "0",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-ar",
                "44100",
            ]
        else:
            command += [
                "-c:v",
                "libx264",
                "-threads",
                "2",
                "-preset",
                "veryfast",
                "-tune",
                "zerolatency",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-ar",
                "44100",
            ]
        if mode != "copy":
            # Closed GOPs and regular IDRs let late/reconnecting readers recover
            # without depending on reference frames from before they connected.
            command += [
                "-profile:v", "baseline" if self.nvdec_compatibility else "main",
                "-g", "25" if self.nvdec_compatibility else "50",
                "-bf", "0",
                "-flags", "+cgop",
                "-force_key_frames", ("expr:gte(t,n_forced)" if self.nvdec_compatibility
                                      else "expr:gte(t,n_forced*2)"),
            ]
            if self.nvdec_compatibility:
                command += ["-level:v", "3.1", "-refs:v", "1",
                            "-maxrate:v", "4M", "-bufsize:v", "4M"]
            if mode == "nvidia":
                command += ["-forced-idr", "1"]
            else:
                command += ["-x264-params", "repeat-headers=1:open-gop=0"]
        command += [
            "-avoid_negative_ts",
            "make_zero",
            "-muxdelay",
            "0.1",
            "-f",
            "rtsp",
            "-rtsp_transport",
            "tcp",
            # FFmpeg defaults to 1472 bytes, exceeding MediaMTX's 1452-byte
            # packet limit (1440-byte RTP payload). Fragment at the publisher
            # so the server can forward H.264 without repacketizing every feed.
            "-pkt_size",
            "1200",
            publish_url,
        ]
        return command

    def _confirm_started(
        self, stream_id: str, process: subprocess.Popen[str]
    ) -> None:
        time.sleep(1.2)
        with self._lock:
            stream = self._streams.get(stream_id)
            if stream and stream.process is process and process.poll() is None:
                stream.status = "live"

    def _capture_output(
        self, stream_id: str, process: subprocess.Popen[str]
    ) -> None:
        if process.stderr:
            for line in process.stderr:
                clean_line = line.strip()
                if clean_line:
                    with self._lock:
                        stream = self._streams.get(stream_id)
                        if stream and stream.process is process:
                            stream.logs.append(clean_line)
        return_code = process.wait()
        with self._lock:
            stream = self._streams.get(stream_id)
            if not stream or stream.process is not process:
                return
            stream.process = None
            if stream.status == "stopping":
                stream.status = "idle"
                stream.started_at = None
            elif return_code == 0:
                stream.status = "ended"
            else:
                stream.status = "error"
                stream.error = (
                    "\n".join(list(stream.logs)[-20:])
                    if stream.logs
                    else "FFmpeg exited unexpectedly."
                )
                logger.error(
                    "FFmpeg publisher %s (%s) exited with code %s:\n%s",
                    stream.stream_name,
                    stream.original_filename,
                    return_code,
                    stream.error,
                )

    def stop(self, stream_id: str) -> dict[str, Any]:
        with self._lock:
            stream = self._require(stream_id)
            stream.preparation_token = None
            process = stream.process
            if not process or process.poll() is not None:
                stream.process = None
                stream.status = "idle"
                stream.started_at = None
                stream.error = None
                return self._serialize(stream)
            stream.status = "stopping"
            process.terminate()
        try:
            process.wait(timeout=4)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
        with self._lock:
            stream = self._require(stream_id)
            if stream.process is process:
                stream.process = None
            stream.status = "idle"
            stream.started_at = None
            stream.error = None
            return self._serialize(stream)

    def start_all(self) -> list[dict[str, Any]]:
        results = []
        for stream_id in self.ids():
            try:
                results.append(self.start(stream_id))
            except RuntimeError:
                results.append(self.get(stream_id))
        return results

    def stop_all(self) -> list[dict[str, Any]]:
        with self._lock:
            for stream in self._streams.values():
                stream.preparation_token = None
                if stream.status == "preparing" and not stream.process:
                    stream.status = "idle"
                    stream.error = None
            active = [
                (stream, stream.process)
                for stream in self._streams.values()
                if stream.process and stream.process.poll() is None
            ]
            for stream, process in active:
                stream.status = "stopping"
                process.terminate()
        deadline = time.monotonic() + 6
        for _, process in active:
            timeout = max(0.1, deadline - time.monotonic())
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
        with self._lock:
            for stream, process in active:
                if stream.process is process:
                    stream.process = None
                stream.status = "idle"
                stream.started_at = None
                stream.error = None
            return [self._serialize(item) for item in self._streams.values()]

    def get(self, stream_id: str, include_logs: bool = True) -> dict[str, Any]:
        with self._lock:
            return self._serialize(self._require(stream_id), include_logs)

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                self._serialize(stream, include_logs=False)
                for stream in sorted(
                    self._streams.values(),
                    key=lambda item: item.original_filename.lower(),
                )
            ]

    def ids(self) -> list[str]:
        with self._lock:
            return list(self._streams)

    def aggregate(self) -> dict[str, int]:
        with self._lock:
            statuses = [stream.status for stream in self._streams.values()]
            return {
                "total": len(statuses),
                "live": statuses.count("live"),
                "starting": statuses.count("starting"),
                "preparing": statuses.count("preparing"),
                "error": statuses.count("error"),
                "idle": statuses.count("idle"),
                "capacity": self.max_streams,
            }

    def _require(self, stream_id: str) -> ManagedStream:
        stream = self._streams.get(stream_id)
        if not stream:
            raise KeyError(stream_id)
        return stream

    def _serialize(
        self, stream: ManagedStream, include_logs: bool = False
    ) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": stream.id,
            "filename": stream.original_filename,
            "stream_name": stream.stream_name,
            "rtsp_url": (
                f"rtsp://{self.public_host}:{self.mediamtx_port}/{stream.stream_name}"
            ),
            "loop": stream.loop,
            "processing_mode": "copy" if stream.publish_path else stream.processing_mode,
            "preparation_engine": stream.preparation_engine,
            "output_profile": "nvdec-720p25" if self.nvdec_compatibility else "source",
            "status": stream.status,
            "started_at": stream.started_at,
            "uptime_seconds": (
                max(0, int(time.time() - stream.started_at))
                if stream.started_at
                else 0
            ),
            "error": stream.error,
        }
        if include_logs:
            data["logs"] = list(stream.logs)[-20:]
        return data

    def shutdown(self) -> None:
        self.stop_all()
