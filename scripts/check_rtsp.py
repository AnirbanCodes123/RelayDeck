"""Read RelayDeck with OpenCV over TCP, without application image processing."""
import argparse
import json
import os
import sys
from pathlib import Path


def open_capture(url: str):
    # Set before importing OpenCV; constrain this diagnostic reader only.
    os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp"
    import cv2

    return cv2.VideoCapture(url, cv2.CAP_FFMPEG, [
        cv2.CAP_PROP_HW_ACCELERATION, cv2.VIDEO_ACCELERATION_NONE,
        cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 10000,
        cv2.CAP_PROP_READ_TIMEOUT_MSEC, 10000,
    ])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="Affected RTSP URL")
    parser.add_argument("--frames", type=int, default=120)
    parser.add_argument("--snapshot", type=Path, default=Path("/tmp/relaydeck-frame.png"))
    args = parser.parse_args()
    if args.frames < 1:
        parser.error("--frames must be positive")
    try:
        capture = open_capture(args.url)
    except ImportError:
        print("OpenCV is missing. Run this script in your application's Python environment.", file=sys.stderr)
        return 1
    import cv2

    try:
        if not capture.isOpened():
            print("Could not open stream with the FFmpeg backend over TCP.", file=sys.stderr)
            return 1
        shape = None
        for number in range(args.frames):
            ok, frame = capture.read()
            if not ok or frame is None or frame.size == 0:
                print(f"Frame read failed after {number} frames.", file=sys.stderr)
                return 1
            if frame.ndim != 3 or frame.shape[2] != 3 or str(frame.dtype) != "uint8":
                print(f"Unexpected frame format: {frame.shape}, {frame.dtype}", file=sys.stderr)
                return 1
            if shape is not None and frame.shape != shape:
                print(f"Frame dimensions changed: {shape} -> {frame.shape}", file=sys.stderr)
                return 1
            shape = frame.shape
            # Both read() and imwrite() use BGR: save exactly what was decoded.
            if number == args.frames - 1:
                if not cv2.imwrite(str(args.snapshot), frame):
                    print(f"Could not write snapshot: {args.snapshot}", file=sys.stderr)
                    return 1
        print(json.dumps({"opencv": cv2.__version__, "backend": capture.getBackendName(),
                          "transport": "tcp", "decoder": "software", "frames": args.frames,
                          "shape": shape, "snapshot": str(args.snapshot)}))
        return 0
    finally:
        capture.release()


if __name__ == "__main__":
    raise SystemExit(main())
