"""Pace prepared H.264 packets while keeping a single RTSP publisher alive."""
from __future__ import annotations

from collections import deque
from fractions import Fraction
import threading
import time

import av


class Playback:
    """Stream-copy playback. Pauses hold an IDR; seeking starts at an IDR.

    Prepared inputs have closed one-second GOPs, no B-frames, and 25 fps.
    Repeating an independent IDR preserves decoder state for the next P-frame;
    repeating a P-frame would corrupt its reference pictures.
    """

    def __init__(self, path, loop, destination):
        self.path = path
        self.loop = loop
        self.destination = destination
        self.error = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._pause_requested = False
        self._paused = False
        self._seek = None
        self._position = 0.0
        self._duration = 0.0
        self._thread = threading.Thread(target=self._run, daemon=True, name="playback")

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    def join(self):
        self._thread.join(timeout=2)

    def snapshot(self):
        with self._lock:
            return {"paused": self._paused, "pause_pending": self._pause_requested and not self._paused,
                    "position_seconds": round(self._position, 2),
                    "duration_seconds": round(self._duration, 2), "seek_pending": self._seek is not None}

    def pause(self, paused):
        with self._lock:
            self._pause_requested = paused

    def forward(self, seconds=10):
        with self._lock:
            target = (self._seek if self._seek is not None else self._position) + seconds
            if self._duration:
                target = target % self._duration if self.loop else min(target, max(0, self._duration - 1))
            self._seek = target

    @staticmethod
    def _packet(data, stream, timestamp, time_base, duration, key=False):
        packet = av.Packet(data)
        packet.stream = stream
        packet.pts = packet.dts = timestamp
        packet.time_base = time_base
        packet.duration = duration
        packet.is_keyframe = key
        return packet

    @staticmethod
    def _silence(stream):
        context = av.CodecContext.create("aac", "w")
        context.sample_rate = stream.codec_context.sample_rate
        context.layout = stream.codec_context.layout.name
        context.format = "fltp"
        context.bit_rate = 128000
        for index in range(4):
            frame = av.AudioFrame(format="fltp", layout=context.layout, samples=1024)
            frame.sample_rate = context.sample_rate
            frame.pts = index * 1024
            for plane in frame.planes:
                plane.update(bytes(plane.buffer_size))
            packets = context.encode(frame)
            if packets:
                return bytes(packets[-1])
        raise RuntimeError("Could not generate paused-stream audio silence.")

    def _run(self):
        source = output = None
        try:
            source = av.open(str(self.path))
            video = source.streams.video[0]
            if video.codec_context.has_b_frames:
                raise RuntimeError("Playback controls require a prepared video without B-frames.")
            with self._lock:
                self._duration = float(video.duration * video.time_base) if video.duration else (source.duration or 0) / av.time_base
            output = av.open(self.destination, "w", format="mpegts", buffer_size=188,
                             options={"mpegts_flags": "resend_headers", "flush_packets": "1",
                                      "max_interleave_delta": "40000"})
            out_video = output.add_stream_from_template(video)
            tracks = {}
            for audio in source.streams.audio:
                tracks[audio.index] = {"stream": output.add_stream_from_template(audio),
                                       "rate": audio.codec_context.sample_rate, "samples": 0,
                                       "queue": deque(), "silence": self._silence(audio)}
            selected = [video, *source.streams.audio]
            packets = iter(source.demux(selected))

            def seek(target):
                nonlocal packets
                source.seek(int(target / video.time_base), stream=video, backward=True)
                packets = iter(source.demux(selected))
                for track in tracks.values():
                    track["queue"].clear()

            def next_frame():
                nonlocal packets
                for attempt in range(2):
                    for packet in packets:
                        if not packet.size:
                            continue
                        if packet.stream.index == video.index:
                            position = float((packet.pts or 0) * video.time_base)
                            return bytes(packet), packet.is_keyframe, position
                        track = tracks.get(packet.stream.index)
                        if track is not None:
                            track["queue"].append(bytes(packet))
                    if not self.loop:
                        return None
                    seek(0)
                return None

            held = None
            frame_number = 0
            last_video_tick = -25
            deadline = time.monotonic()
            while not self._stop.is_set():
                with self._lock:
                    target, self._seek = self._seek, None
                    pause_requested = self._pause_requested
                if target is not None:
                    seek(target)
                    held = None
                if not (pause_requested and held is not None):
                    current = next_frame()
                    if current is None:
                        break
                    data, key, position = current
                    # A seek must land on a keyframe, including when paused.
                    if target is not None and not key:
                        raise RuntimeError("Seek did not produce an independent keyframe.")
                    held = current if pause_requested and key else None
                else:
                    data, key, position = held
                paused = pause_requested and held is not None
                with self._lock:
                    pause_started = paused and not self._paused
                    self._position, self._paused = position, paused
                # Timestamps belong to the continuous output clock, not to the
                # source playhead. Neither pause nor seek moves RTP time back.
                # One independent frame per second keeps paused readers alive
                # without sending a large IDR 25 times per second. Audio still
                # advances every tick, and resume immediately returns to 25 fps.
                if not paused or pause_started or target is not None or frame_number - last_video_tick >= 25:
                    output.mux(self._packet(data, out_video, frame_number * 3600,
                                            Fraction(1, 90000), 90000 if paused else 3600, key))
                    last_video_tick = frame_number
                for track in tracks.values():
                    while track["samples"] * 25 < (frame_number + 1) * track["rate"]:
                        audio_data = (track["queue"].popleft() if not paused and track["queue"] else track["silence"])
                        output.mux(self._packet(audio_data, track["stream"], track["samples"],
                                                Fraction(1, track["rate"]), 1024))
                        track["samples"] += 1024
                frame_number += 1
                deadline += .04
                self._stop.wait(max(0, deadline - time.monotonic()))
                # Avoid bursting old packets after a slow disk read or seek.
                deadline = max(deadline, time.monotonic() - .04)
        except Exception as exc:
            if not self._stop.is_set():
                self.error = str(exc)
        finally:
            if source is not None:
                source.close()
            if output is not None:
                try:
                    output.close()
                except (OSError, ValueError, av.FFmpegError):
                    pass
            # EOF terminates the publisher on a natural end or feeder failure.
            try:
                self.destination.close()
            except (OSError, ValueError):
                pass
